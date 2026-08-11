from pathlib import Path
import copy
import json

import pytest

from video_vault.analyzer.multi_frame import (
    MultiFrameUnsupported,
    MultiFrameValidationError,
    _partition_lengths,
    build_frame_windows,
    multi_frame_publish_errors,
    normalize_window_result,
    parse_window_response,
    plan_frame_windows,
    provider_capability,
    validate_window,
    window_cache_key,
    write_window_evidence,
)
from video_vault.analyzer import vision_pipeline
from video_vault.analyzer.local_provider import LocalProvider
from video_vault.capability_registry import (
    persist_probe_capability,
    resolve_verified_probe_capability,
    validate_local_endpoint_scope,
)
from video_vault.config import load_config
from video_vault.database import connect, init_db, project_videos, upsert_video
from video_vault.project import create_project
import video_vault.project_perception as project_perception
from video_vault.project_perception import run_project_perception
import video_vault.analyzer.multi_frame as multi_frame


def _manifest(tmp_path: Path, count: int = 6) -> list[dict]:
    rows = []
    for index in range(count):
        frame = tmp_path / f"frame_{index:02}.jpg"
        frame.write_bytes(f"frame-{index}".encode())
        rows.append({
            "frame_path": str(frame),
            "timestamp_seconds": float(index * 2),
            "sample_reasons": ["baseline"],
        })
    return rows


def test_window_partition_never_leaves_short_tail():
    assert _partition_lengths(6) == [3, 3]
    assert _partition_lengths(11) == [5, 3, 3]
    for count in range(3, 31):
        assert sum(_partition_lengths(count)) == count
        assert all(3 <= length <= 5 for length in _partition_lengths(count))


def test_window_manifest_and_cache_key_include_fingerprints_and_timestamps(tmp_path):
    windows = build_frame_windows(_manifest(tmp_path), 20)
    assert [len(window["frames"]) for window in windows] == [3, 3]
    assert all(len(frame["fingerprint"]) == 64 for window in windows for frame in window["frames"])
    first = window_cache_key(windows[0], provider="mock", model="rules")
    windows[0]["frames"][0]["timestamp_seconds"] += 0.1
    assert window_cache_key(windows[0], provider="mock", model="rules") != first


def test_scene_boundary_and_large_gap_never_share_a_window(tmp_path):
    frames = _manifest(tmp_path, 6)
    frames[3]["timestamp_seconds"] = 30
    frames[4]["timestamp_seconds"] = 32
    frames[5]["timestamp_seconds"] = 34
    frames[3]["sample_reasons"] = ["scene", "boundary"]
    windows = build_frame_windows(frames, 40)
    assert [len(window["frames"]) for window in windows] == [3, 3]
    assert all(
        not ({0, 30} <= {frame["timestamp_seconds"] for frame in window["frames"]})
        for window in windows
    )
    assert any("scene_boundary" in window["window_policy"]["split_reasons"] for window in windows[1:])


@pytest.mark.parametrize("count", [1, 2])
def test_short_scene_fragment_is_non_mandatory_evidence(tmp_path, count):
    plan = plan_frame_windows(_manifest(tmp_path, count), 20)
    assert plan["mandatory_windows"] == []
    assert len(plan["non_mandatory_fragments"]) == 1
    fragment = plan["non_mandatory_fragments"][0]
    assert fragment["mandatory"] is False
    assert fragment["reason"] == "insufficient_scene_samples"
    assert len(fragment["frames"]) == count
    assert fragment["validation"]["model_result_required"] is False


def test_short_sampling_boundary_fragment_merges_when_policy_safe(tmp_path):
    frames = _manifest(tmp_path, 5)
    frames[3]["sample_reasons"] = ["boundary"]
    plan = plan_frame_windows(frames, 20)
    assert plan["non_mandatory_fragments"] == []
    assert [len(window["frames"]) for window in plan["mandatory_windows"]] == [5]
    assert "merged_short_fragment" in plan["mandatory_windows"][0]["window_policy"]["split_reasons"]


@pytest.mark.parametrize("left_count", [4, 5])
def test_soft_boundary_cluster_repartitions_without_fragment(tmp_path, left_count):
    frames = _manifest(tmp_path, 6)
    frames[left_count]["sample_reasons"] = ["boundary"]

    first = plan_frame_windows(frames, 20, min_frames=3, max_frames=3)
    second = plan_frame_windows(frames, 20, min_frames=3, max_frames=3)

    assert [len(window["frames"]) for window in first["mandatory_windows"]] == [3, 3]
    assert first["non_mandatory_fragments"] == []
    assert [window["window_uuid"] for window in first["mandatory_windows"]] == [
        window["window_uuid"] for window in second["mandatory_windows"]
    ]
    planned_fingerprints = [
        frame["fingerprint"]
        for window in first["mandatory_windows"]
        for frame in window["frames"]
    ]
    input_fingerprints = [multi_frame.frame_fingerprint(Path(row["frame_path"])) for row in frames]
    assert sorted(planned_fingerprints) == sorted(input_fingerprints)
    assert len(planned_fingerprints) == len(set(planned_fingerprints)) == len(frames)


def test_soft_boundary_unpartitionable_frames_coalesce_as_one_fragment(tmp_path):
    frames = _manifest(tmp_path, 2)
    frames[1]["sample_reasons"] = ["boundary"]

    plan = plan_frame_windows(frames, 10, min_frames=3, max_frames=3)

    assert plan["mandatory_windows"] == []
    assert [len(fragment["frames"]) for fragment in plan["non_mandatory_fragments"]] == [2]
    assert "merged_short_fragment" in plan["non_mandatory_fragments"][0]["window_policy"]["split_reasons"]


@pytest.mark.parametrize("boundary_kind", ["scene", "large_gap"])
def test_short_cluster_does_not_cross_hard_boundary_or_large_gap(tmp_path, boundary_kind):
    frames = _manifest(tmp_path, 4)
    if boundary_kind == "scene":
        frames[2]["sample_reasons"] = ["scene"]
    else:
        frames[2]["timestamp_seconds"] = 30
        frames[3]["timestamp_seconds"] = 32

    plan = plan_frame_windows(frames, 40, min_frames=3, max_frames=3)

    assert plan["mandatory_windows"] == []
    assert [len(fragment["frames"]) for fragment in plan["non_mandatory_fragments"]] == [2, 2]
    reasons = {
        reason
        for fragment in plan["non_mandatory_fragments"]
        for reason in fragment["window_policy"]["split_reasons"]
    }
    assert ("scene_boundary" if boundary_kind == "scene" else "large_temporal_gap") in reasons
    covered_fingerprints = [
        frame["fingerprint"]
        for window in plan["mandatory_windows"]
        for frame in window["frames"]
    ] + [
        frame["fingerprint"]
        for fragment in plan["non_mandatory_fragments"]
        for frame in fragment["frames"]
    ]
    input_fingerprints = [multi_frame.frame_fingerprint(Path(row["frame_path"])) for row in frames]
    assert sorted(covered_fingerprints) == sorted(input_fingerprints)
    assert len(covered_fingerprints) == len(set(covered_fingerprints)) == len(frames)


def test_short_hard_scene_fragment_remains_explicitly_uncovered(tmp_path):
    frames = _manifest(tmp_path, 5)
    frames[3]["sample_reasons"] = ["scene"]
    first = plan_frame_windows(frames, 20)
    second = plan_frame_windows(frames, 20)
    assert [len(window["frames"]) for window in first["mandatory_windows"]] == [3]
    assert [len(item["frames"]) for item in first["non_mandatory_fragments"]] == [2]
    assert first["non_mandatory_fragments"][0]["reason"] == "insufficient_scene_samples"
    assert [window["window_uuid"] for window in first["mandatory_windows"]] == [window["window_uuid"] for window in second["mandatory_windows"]]
    assert [item["fragment_uuid"] for item in first["non_mandatory_fragments"]] == [item["fragment_uuid"] for item in second["non_mandatory_fragments"]]
    assert all(validate_window(window)["status"] == "pass" for window in first["mandatory_windows"])


def test_local_capability_requires_explicit_valid_contract():
    class Local:
        provider = "local"

    assert provider_capability(Local(), {"ai": {"local": {}}})["capability_source"] == "missing"
    capability = provider_capability(Local(), {"ai": {"local": {"multi_frame_capability": {
        "supports_multi_image": True,
        "maximum_images": 4,
        "supported_image_formats": ["jpeg", "png"],
        "provider_contract_version": "local-multiframe-v1",
        "prompt_contract_version": "prompt-v1",
        "schema_version": 1,
        "capability_source": "explicit_config",
    }}}})
    assert capability["supports_multi_image"] is True
    assert capability["maximum_images"] == 4
    assert LocalProvider.supports_multi_frame is False


def test_verified_probe_registry_enables_formal_multi_image_without_metadata(tmp_path, monkeypatch):
    cfg = {
        "library_root": str(tmp_path),
        "ffmpeg_path": "missing-ffmpeg",
        "ai": {"provider": "local", "local": {
            "base_url": "http://127.0.0.1:1234/v1",
            "model": "vision-model",
        }},
    }
    scope = validate_local_endpoint_scope("http://127.0.0.1:1234/v1")
    persisted = persist_probe_capability(
        cfg,
        provider="local",
        model="vision-model",
        endpoint_scope=scope,
        verified=True,
        maximum_images=3,
        supported_image_formats=["jpeg"],
        provider_contract_version=multi_frame.MULTI_FRAME_CONTRACT_VERSION,
        prompt_contract_version=multi_frame.MULTI_FRAME_PROMPT_VERSION,
        capability_schema_version=multi_frame.MULTI_FRAME_SCHEMA_VERSION,
        probe_evidence={
            "status": "pass",
            "semantic_validation": "image_count_and_order_match",
            "image_count": 3,
            "validated_network_scope": "loopback",
            "request_reasoning_control": "per_request",
        },
    )
    assert persisted["status"] == "persisted"

    class VerifiedLocal:
        provider = "local"
        model = "vision-model"
        base_url = "http://127.0.0.1:1234/v1"
        multi_frame_prompt_version = multi_frame.MULTI_FRAME_PROMPT_VERSION

        def __init__(self):
            self.calls = 0

        def analyze_window(self, _paths, timestamps, _video):
            self.calls += 1
            return ({
                "summary": "咖啡沖煮畫面",
                "action": "倒水沖煮",
                "start_seconds": timestamps[0],
                "end_seconds": timestamps[-1],
                "shot_role": "過程",
                "technical_quality": {"score": 0.9, "issues": []},
                "duplicate_group": "",
                "natural_audio_recommendation": "keep",
                "confidence": 0.9,
                "tags": ["coffee"],
            }, {"choices": [{"message": {"content": "{}"}}]})

    provider = VerifiedLocal()
    monkeypatch.setattr(vision_pipeline, "provider_from_config", lambda _cfg: provider)
    monkeypatch.setattr(multi_frame, "_write_contact_sheet", lambda _paths, output, _ffmpeg: (output.write_bytes(b"sheet"), "")[1])
    result = vision_pipeline.analyze_frame_windows(
        {"duration_seconds": 10, "filename": "clip.mp4", "category": "coffee"},
        cfg,
        _manifest(tmp_path, 3),
        duration_seconds=10,
        evidence_root=tmp_path / "evidence",
    )
    assert provider.calls == 1
    assert result["vision_calls"] == 1
    assert result["multi_frame_contract"]["capability"]["capability_source"] == "verified_probe"
    assert multi_frame_publish_errors(result) == []

    changed_contract = resolve_verified_probe_capability(
        cfg,
        provider="local",
        model="vision-model",
        base_url="http://127.0.0.1:1234/v1",
        provider_contract_version="changed-contract",
        prompt_contract_version=multi_frame.MULTI_FRAME_PROMPT_VERSION,
        capability_schema_version=multi_frame.MULTI_FRAME_SCHEMA_VERSION,
    )
    assert changed_contract["status"] == "missing"


def test_same_binding_failed_probe_overrides_explicit_config_and_blocks_calls(tmp_path, monkeypatch):
    explicit = {
        "supports_multi_image": True,
        "maximum_images": 3,
        "supported_image_formats": ["jpeg"],
        "provider_contract_version": multi_frame.MULTI_FRAME_CONTRACT_VERSION,
        "prompt_contract_version": multi_frame.MULTI_FRAME_PROMPT_VERSION,
        "schema_version": multi_frame.MULTI_FRAME_SCHEMA_VERSION,
        "capability_source": "explicit_config",
    }
    cfg = {
        "library_root": str(tmp_path),
        "ffmpeg_path": "missing-ffmpeg",
        "ai": {"provider": "local", "local": {
            "base_url": "http://127.0.0.1:1234/v1",
            "model": "vision-model",
            "multi_frame_capability": explicit,
        }},
    }

    class Local:
        provider = "local"
        model = "vision-model"
        base_url = "http://127.0.0.1:1234/v1"

        def __init__(self):
            self.calls = 0

        def analyze_window(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("failed probe must block before generation")

    provider = Local()
    initial = provider_capability(provider, cfg)
    assert initial["supports_multi_image"] is True
    assert initial["capability_source"] == "explicit_config"

    scope = validate_local_endpoint_scope(provider.base_url)
    persisted = persist_probe_capability(
        cfg,
        provider="local",
        model=provider.model,
        endpoint_scope=scope,
        verified=False,
        maximum_images=0,
        supported_image_formats=[],
        provider_contract_version=multi_frame.MULTI_FRAME_CONTRACT_VERSION,
        prompt_contract_version=multi_frame.MULTI_FRAME_PROMPT_VERSION,
        capability_schema_version=multi_frame.MULTI_FRAME_SCHEMA_VERSION,
        probe_evidence={"status": "blocked", "semantic_validation": "wrong_order"},
    )
    assert persisted["status"] == "persisted"
    assert persisted["verification_source"] == "probe_failed"

    blocked = provider_capability(provider, cfg)
    assert blocked["supports_multi_image"] is False
    assert blocked["capability_source"] == "verified_probe_blocked"
    assert blocked["registry_reason"] == "verified_probe_failed"

    monkeypatch.setattr(vision_pipeline, "provider_from_config", lambda _cfg: provider)
    with pytest.raises(MultiFrameUnsupported, match="does not have verified multi-image capability"):
        vision_pipeline.analyze_frame_windows(
            {"duration_seconds": 10, "filename": "clip.mp4", "category": "coffee"},
            cfg,
            _manifest(tmp_path, 3),
            duration_seconds=10,
            evidence_root=tmp_path / "evidence",
        )
    assert provider.calls == 0


def test_local_capability_requires_jpeg_for_current_request_contract():
    class Local:
        provider = "local"

    cfg = {"ai": {"local": {"multi_frame_capability": {
        "supports_multi_image": True,
        "maximum_images": 4,
        "supported_image_formats": ["png"],
        "provider_contract_version": "local-multiframe-v1",
        "prompt_contract_version": "prompt-v1",
        "schema_version": 1,
        "capability_source": "explicit_config",
    }}}}
    assert provider_capability(Local(), cfg)["capability_source"] == "invalid"


def test_parse_window_response_accepts_chat_content_parts():
    assert parse_window_response({
        "choices": [{"message": {"content": [
            {"type": "text", "text": '{"summary":"畫面"}'},
        ]}}],
    }) == {"summary": "畫面"}


def test_partition_respects_provider_maximum_images():
    assert _partition_lengths(6, min_frames=3, max_frames=3) == [3, 3]
    assert max(_partition_lengths(7, min_frames=3, max_frames=3)) <= 3


def test_provider_exact_three_limit_records_partition_remainder(tmp_path):
    plan = plan_frame_windows(_manifest(tmp_path, 7), 20, min_frames=3, max_frames=3)
    assert [len(window["frames"]) for window in plan["mandatory_windows"]] == [3, 3]
    assert [len(fragment["frames"]) for fragment in plan["non_mandatory_fragments"]] == [1]
    assert "provider_partition_remainder" in plan["non_mandatory_fragments"][0]["window_policy"]["split_reasons"]
    assert all(validate_window(window, min_frames=3, max_frames=3)["status"] == "pass" for window in plan["mandatory_windows"])


def test_insufficient_evidence_blocks_without_single_frame_fallback(tmp_path):
    result = vision_pipeline.analyze_frame_windows(
        {"duration_seconds": 10, "filename": "short.mp4", "category": "coffee"},
        {
            "library_root": str(tmp_path),
            "ffmpeg_path": "missing-ffmpeg",
            "ai": {"provider": "mock", "model": "mock-v1"},
        },
        _manifest(tmp_path, 2),
        duration_seconds=10,
        evidence_root=tmp_path / "evidence",
    )
    assert result["window_results"] == []
    assert result["segments"] == []
    assert result["vision_calls"] == 0
    assert result["window_validation"]["status"] == "blocked"
    assert "insufficient_scene_samples" in result["window_validation"]["needs_review_reasons"]
    assert result["non_mandatory_evidence"][0]["mandatory"] is False
    assert result["multi_frame_contract"]["status"] == "blocked"
    errors = multi_frame_publish_errors(result)
    assert "multi_frame_contract_not_pass" in errors
    assert "missing_evidence_window_results" in errors


def test_mixed_pass_and_insufficient_windows_fail_closed(tmp_path):
    pass_root = tmp_path / "pass"
    skipped_root = tmp_path / "skipped"
    pass_root.mkdir()
    skipped_root.mkdir()
    valid = build_frame_windows(_manifest(pass_root, 4), 20)[0]
    invalid = build_frame_windows(_manifest(skipped_root, 4), 20)[0]
    invalid["frames"] = invalid["frames"][:2]
    invalid["validation"] = validate_window(invalid)
    planned = [valid, invalid]
    result = vision_pipeline.analyze_frame_windows(
        {"duration_seconds": 20, "filename": "mixed.mp4", "category": "travel"},
        {
            "library_root": str(tmp_path),
            "ffmpeg_path": "missing-ffmpeg",
            "ai": {"provider": "mock", "model": "mock-v1"},
        },
        [],
        duration_seconds=20,
        evidence_root=tmp_path / "evidence",
        windows=planned,
    )
    assert result["window_validation"]["status"] == "blocked"
    assert result["window_results"] == []
    assert result["segments"] == []
    assert result["vision_calls"] == 0
    assert "insufficient_evidence_frames" in result["window_validation"]["needs_review_reasons"]
    assert "window_validation_not_pass" in multi_frame_publish_errors(result)


def test_publish_gate_rejects_duplicate_or_mismatched_window_uuids(tmp_path, monkeypatch):
    monkeypatch.setattr(
        multi_frame,
        "_write_contact_sheet",
        lambda _paths, output, _ffmpeg: (output.write_bytes(b"sheet"), "")[1],
    )
    result = vision_pipeline.analyze_frame_windows(
        {"duration_seconds": 20, "filename": "uuid.mp4", "category": "coffee"},
        {
            "library_root": str(tmp_path),
            "ffmpeg_path": "missing-ffmpeg",
            "ai": {"provider": "mock", "model": "mock-v1"},
        },
        _manifest(tmp_path, 6),
        duration_seconds=20,
        evidence_root=tmp_path / "evidence",
    )
    assert multi_frame_publish_errors(result) == []

    duplicate = copy.deepcopy(result)
    duplicate["segments"][1]["window_uuid"] = duplicate["segments"][0]["window_uuid"]
    duplicate_errors = multi_frame_publish_errors(duplicate)
    assert "published_segment_has_duplicate_uuid" in duplicate_errors
    assert "published_segment_coverage_mismatch" in duplicate_errors

    mismatch = copy.deepcopy(result)
    mismatch["window_results"][0]["window_uuid"] = "window_unplanned"
    mismatch_errors = multi_frame_publish_errors(mismatch)
    assert "window_result_coverage_mismatch" in mismatch_errors
    assert "published_segment_coverage_mismatch" in mismatch_errors


def test_normalize_window_result_rejects_action_outside_window(tmp_path):
    window = build_frame_windows(_manifest(tmp_path, 3), 10)[0]
    payload = {
        "summary": "連續畫面",
        "action": "倒水",
        "start_seconds": -1,
        "end_seconds": 2,
        "shot_role": "process",
        "technical_quality": {"score": 0.8, "issues": []},
        "duplicate_group": "g1",
        "natural_audio_recommendation": "keep",
        "confidence": 0.8,
    }
    with pytest.raises(MultiFrameValidationError, match="outside"):
        normalize_window_result(payload, window)


def test_normalize_window_result_converts_provider_score_scales(tmp_path):
    window = build_frame_windows(_manifest(tmp_path, 3), 10)[0]
    payload = {
        "summary": "連續畫面",
        "action": "倒水",
        "start_seconds": 0,
        "end_seconds": 4,
        "shot_role": "process",
        "technical_quality": {"score": 8, "issues": []},
        "duplicate_group": "",
        "natural_audio_recommendation": "keep",
        "confidence": 80,
    }
    result = normalize_window_result(payload, window)
    assert result["technical_quality"]["score"] == 0.8
    assert result["confidence"] == 0.8


def test_evidence_bundle_excludes_frame_paths_from_public_window(tmp_path):
    window = build_frame_windows(_manifest(tmp_path, 3), 10)[0]
    evidence = write_window_evidence(
        window,
        {"summary": "測試"},
        {"status": "pass"},
        tmp_path / "evidence",
        ffmpeg_path="missing-ffmpeg",
        raw_response={"raw": "ok", "frame_path": r"D:\private\source.jpg"},
        provider_contract={"capability_source": "built_in_mock"},
    )
    public_window = (tmp_path / "evidence" / window["window_uuid"] / "window.json").read_text(encoding="utf-8")
    assert "frame_path" not in public_window
    assert evidence["artifact_id"] == window["window_uuid"]
    raw = (tmp_path / "evidence" / window["window_uuid"] / "raw_response.json").read_text(encoding="utf-8")
    assert "frame_path" not in raw
    index = json.loads((tmp_path / "evidence" / "raw_response_index.json").read_text(encoding="utf-8"))
    assert index[window["window_uuid"]]["raw_response"].endswith("/raw_response.json")


def test_invalid_multiframe_cache_is_ignored(monkeypatch, tmp_path):
    def contact_sheet(_paths, output, _ffmpeg):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"sheet")
        return ""

    monkeypatch.setattr(multi_frame, "_write_contact_sheet", contact_sheet)
    cfg = {"library_root": str(tmp_path), "ffmpeg_path": "ffmpeg", "ai": {"provider": "mock"}}
    frames = _manifest(tmp_path, 3)
    first = vision_pipeline.analyze_frame_windows(
        {"duration_seconds": 10, "filename": "clip.mp4", "category": "coffee"},
        cfg,
        frames,
        duration_seconds=10,
        evidence_root=tmp_path / "evidence",
    )
    cache_path = tmp_path / "05_index" / "raw_ai_outputs" / "multiframe" / f"{first['window_results'][0]['cache_key']}.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    cache["cache_key"] = "stale-cache"
    cache_path.write_text(json.dumps(cache), encoding="utf-8")
    second = vision_pipeline.analyze_frame_windows(
        {"duration_seconds": 10, "filename": "clip.mp4", "category": "coffee"},
        cfg,
        frames,
        duration_seconds=10,
        evidence_root=tmp_path / "evidence",
    )
    assert second["window_results"][0]["cache_hit"] is False


def test_provider_without_multi_frame_is_blocked(monkeypatch, tmp_path):
    class SingleFrameProvider:
        provider = "single"
        model = "single-v1"
        supports_multi_frame = False

    monkeypatch.setattr(vision_pipeline, "provider_from_config", lambda _cfg: SingleFrameProvider())
    frames = _manifest(tmp_path, 3)
    with pytest.raises(MultiFrameUnsupported, match="no single-frame fallback"):
        vision_pipeline.analyze_frame_windows(
            {"duration_seconds": 10, "filename": "clip.mp4"},
            {"library_root": str(tmp_path), "ai": {"provider": "single"}},
            frames,
            duration_seconds=10,
            evidence_root=tmp_path / "evidence",
        )


def test_contact_sheet_failure_blocks_window(monkeypatch, tmp_path):
    monkeypatch.setattr(multi_frame, "_write_contact_sheet", lambda *_args: "contact sheet failed")
    result = vision_pipeline.analyze_frame_windows(
        {"duration_seconds": 10, "filename": "clip.mp4", "category": "coffee"},
        {"library_root": str(tmp_path), "ffmpeg_path": "ffmpeg", "ai": {"provider": "mock"}},
        _manifest(tmp_path, 3),
        duration_seconds=10,
        evidence_root=tmp_path / "evidence",
    )
    assert result["window_validation"]["status"] == "blocked"
    assert result["window_results"][0]["validation"]["status"] == "blocked"


def test_project_perception_persists_multiframe_run_results(tmp_path, monkeypatch):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    video_id = upsert_video(db, {
        "original_path": str(source),
        "current_path": str(source),
        "filename": source.name,
        "category": "coffee",
        "duration_seconds": 16,
        "status": "uploaded",
    })
    project_id = create_project(db, "multi-frame", [video_id], category="coffee")
    cfg = {
        "library_root": str(tmp_path),
        "frame_interval_seconds": 4,
        "frame_height": 720,
        "ffmpeg_path": "missing-ffmpeg",
        "ffprobe_path": "missing-ffprobe",
        "sampling": {
            "mode": "fixed",
            "baseline_interval_seconds": 4,
            "policy_name": "test",
            "policy_version": 1,
            "max_frames_per_clip": 20,
            "max_frames_per_minute": 60,
        },
        "ai": {"provider": "mock", "model": "mock-v1"},
    }

    def fake_extract(_source: Path, out_dir: Path, _cfg: dict) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for index in range(4):
            path = out_dir / f"frame_{index:02}.jpg"
            path.write_bytes(f"frame-{index}".encode())
            paths.append(path)
        return paths

    monkeypatch.setattr(project_perception, "extract_frames", fake_extract)
    monkeypatch.setattr(project_perception, "rename_after_perception", lambda _cfg, _db, video: video)
    monkeypatch.setattr(project_perception, "perceive_output", lambda *args, **kwargs: None)
    monkeypatch.setattr(project_perception, "write_plan_files", lambda *args, **kwargs: None)
    monkeypatch.setattr(project_perception, "build_project_plan", lambda *args, **kwargs: {})
    monkeypatch.setattr(multi_frame, "_write_contact_sheet", lambda _paths, output, _ffmpeg: (output.write_bytes(b"sheet"), "")[1])

    result = run_project_perception(cfg, db, project_id, dict(project_videos(db, project_id)[0]))
    assert result["run"]["status"] == "succeeded"
    assert result["window_results"]
    assert result["window_results"][0]["cache_hit"] is False
    run = result["run"]
    assert run["window_manifest"]
    assert run["window_results"]
    assert run["window_validation"]["status"] == "pass"
    assert run["window_results"][0]["segment_uuid"]
    assert run["window_results"][0]["model_provenance"]["provider"] == "mock"
    assert run["window_results"][0]["model_provenance"]["model"] == "rules"
    assert multi_frame_publish_errors(result) == []
    normalized = Path(run["staging_path"]) / "evidence" / run["window_results"][0]["window_uuid"] / "normalized.json"
    assert json.loads(normalized.read_text(encoding="utf-8"))["segment_uuid"] == run["window_results"][0]["segment_uuid"]


def test_loaded_yaml_false_uses_explicit_legacy_single_frame_contract(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    yaml_library_root = str(tmp_path).replace("\\", "/")
    config_path.write_text(
        f"library_root: {yaml_library_root}\n"
        "frame_interval_seconds: 5\n"
        "perception:\n"
        "  multi_frame:\n"
        "    enabled: false\n"
        "  audio:\n"
        "    enabled: false\n"
        "ai:\n"
        "  provider: mock\n",
        encoding="utf-8",
    )
    cfg = load_config(str(config_path))
    assert cfg["perception"]["multi_frame"]["enabled"] is False

    db = tmp_path / "db.sqlite3"
    init_db(db)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    video_id = upsert_video(db, {
        "original_path": str(source), "current_path": str(source), "filename": source.name,
        "category": "coffee", "duration_seconds": 20, "status": "uploaded",
    })
    project_id = create_project(db, "legacy-yaml", [video_id], category="coffee")

    def fake_extract(_source: Path, out_dir: Path, _cfg: dict) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for index in range(4):
            path = out_dir / f"frame_{index:02}.jpg"
            path.write_bytes(f"frame-{index}".encode())
            paths.append(path)
        return paths

    monkeypatch.setattr(project_perception, "extract_frames", fake_extract)
    monkeypatch.setattr(project_perception, "rename_after_perception", lambda _cfg, _db, video: video)
    monkeypatch.setattr(project_perception, "perceive_output", lambda *args, **kwargs: None)
    monkeypatch.setattr(project_perception, "write_plan_files", lambda *args, **kwargs: None)
    monkeypatch.setattr(project_perception, "build_project_plan", lambda *args, **kwargs: {})

    result = run_project_perception(cfg, db, project_id, dict(project_videos(db, project_id)[0]))
    assert result["run"]["status"] == "succeeded"
    assert result["window_results"] == []
    assert result["segments"]


def test_project_perception_does_not_publish_blocked_evidence(tmp_path, monkeypatch):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    video_id = upsert_video(db, {
        "original_path": str(source), "current_path": str(source), "filename": source.name,
        "category": "coffee", "duration_seconds": 20, "status": "uploaded",
    })
    project_id = create_project(db, "blocked-evidence", [video_id], category="coffee")
    cfg = {
        "library_root": str(tmp_path), "frame_interval_seconds": 5, "frame_height": 720,
        "ffmpeg_path": "missing-ffmpeg", "ffprobe_path": "missing-ffprobe",
        "sampling": {"mode": "fixed", "baseline_interval_seconds": 5,
                      "policy_name": "test", "policy_version": 1,
                      "max_frames_per_clip": 20, "max_frames_per_minute": 60},
        "ai": {"provider": "mock", "model": "mock-v1"},
    }

    def fake_extract(_source: Path, out_dir: Path, _cfg: dict) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for index in range(4):
            path = out_dir / f"frame_{index:02}.jpg"
            path.write_bytes(f"frame-{index}".encode())
            paths.append(path)
        return paths

    monkeypatch.setattr(project_perception, "extract_frames", fake_extract)
    monkeypatch.setattr(multi_frame, "_write_contact_sheet", lambda *_args: "contact sheet failed")
    with pytest.raises(MultiFrameValidationError, match="evidence validation blocked"):
        run_project_perception(cfg, db, project_id, dict(project_videos(db, project_id)[0]))
    with connect(db) as con:
        run_row = con.execute("select status from analysis_runs order by id desc limit 1").fetchone()
        frame_count = con.execute("select count(*) from frames where video_id=?", (video_id,)).fetchone()[0]
    assert dict(run_row)["status"] == "failed"
    assert frame_count == 0


def test_project_perception_fails_closed_on_insufficient_evidence(tmp_path, monkeypatch):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    video_id = upsert_video(db, {
        "original_path": str(source), "current_path": str(source), "filename": source.name,
        "category": "travel", "duration_seconds": 20, "status": "uploaded",
    })
    project_id = create_project(db, "insufficient-evidence", [video_id], category="travel")
    cfg = {
        "library_root": str(tmp_path), "frame_interval_seconds": 5, "frame_height": 720,
        "ffmpeg_path": "missing-ffmpeg", "ffprobe_path": "missing-ffprobe",
        "sampling": {"mode": "fixed", "baseline_interval_seconds": 5,
                      "policy_name": "test", "policy_version": 1,
                      "max_frames_per_clip": 20, "max_frames_per_minute": 60},
        "ai": {"provider": "mock", "model": "mock-v1"},
    }

    def fake_extract(_source: Path, out_dir: Path, _cfg: dict) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for index in range(4):
            path = out_dir / f"frame_{index:02}.jpg"
            path.write_bytes(f"frame-{index}".encode())
            paths.append(path)
        return paths

    monkeypatch.setattr(project_perception, "extract_frames", fake_extract)
    monkeypatch.setattr(
        project_perception,
        "dedupe_visual_samples",
        lambda paths, samples, _cfg, _policy: (paths[:2], samples[:2], {}),
    )
    with pytest.raises(MultiFrameValidationError, match="evidence validation blocked"):
        run_project_perception(cfg, db, project_id, dict(project_videos(db, project_id)[0]))
    with connect(db) as con:
        run_row = con.execute(
            "select status, window_results_json, window_validation_json from analysis_runs order by id desc limit 1"
        ).fetchone()
        segment_count = con.execute("select count(*) from segments where video_id=?", (video_id,)).fetchone()[0]
        project_video = con.execute(
            "select analysis_status from project_videos where video_id=? and project_id=?",
            (video_id, project_id),
        ).fetchone()
    assert dict(run_row)["status"] == "failed"
    assert json.loads(dict(run_row)["window_results_json"]) == []
    validation = json.loads(dict(run_row)["window_validation_json"])
    assert validation["status"] == "blocked"
    assert "insufficient_scene_samples" in validation["needs_review_reasons"]
    assert segment_count == 0
    assert dict(project_video)["analysis_status"] == "failed"

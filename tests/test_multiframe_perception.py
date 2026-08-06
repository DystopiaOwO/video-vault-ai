from pathlib import Path
import json

import pytest

from video_vault.analyzer.multi_frame import (
    MultiFrameUnsupported,
    MultiFrameValidationError,
    _partition_lengths,
    build_frame_windows,
    normalize_window_result,
    provider_capability,
    window_cache_key,
    write_window_evidence,
)
from video_vault.analyzer import vision_pipeline
from video_vault.database import init_db, project_videos, upsert_video
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
        "duration_seconds": 20,
        "status": "uploaded",
    })
    project_id = create_project(db, "multi-frame", [video_id], category="coffee")
    cfg = {
        "library_root": str(tmp_path),
        "frame_interval_seconds": 5,
        "frame_height": 720,
        "ffmpeg_path": "missing-ffmpeg",
        "ffprobe_path": "missing-ffprobe",
        "sampling": {
            "mode": "fixed",
            "baseline_interval_seconds": 5,
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
    normalized = Path(run["staging_path"]) / "evidence" / run["window_results"][0]["window_uuid"] / "normalized.json"
    assert json.loads(normalized.read_text(encoding="utf-8"))["segment_uuid"] == run["window_results"][0]["segment_uuid"]

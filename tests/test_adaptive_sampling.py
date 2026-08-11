from pathlib import Path
from types import SimpleNamespace

import pytest

import video_vault.sampling as sampling
from video_vault.config import load_config
from video_vault.sampling import (
    SamplingError,
    build_sampling_plan,
    dedupe_visual_samples,
    estimate_sampling_count,
    resolved_ai_model,
    resolved_sampling_policy,
    sampling_contract_hash,
)
from video_vault.analyzer.frame_analysis import merge_frames_to_segments
from video_vault.analyzer.vision_pipeline import provider_from_config
from video_vault.ui import _sampling_override


def _cfg(**sampling_overrides) -> dict:
    return {
        "frame_interval_seconds": 5,
        "frame_height": 720,
        "ffmpeg_path": "ffmpeg",
        "sampling": {
            "mode": "adaptive",
            "preset": "balanced",
            "baseline_interval_seconds": 5,
            "prescan_interval_seconds": 0.5,
            "dense_interval_seconds": 1,
            "scene_threshold": 0.32,
            "motion_threshold": 0.06,
            "min_interval_seconds": 0.25,
            "max_frames_per_clip": 180,
            "max_frames_per_minute": 30,
            "visual_dedupe_threshold": 0.985,
            **sampling_overrides,
        },
        "ai": {"provider": "mock", "model": "legacy-wrong-model"},
    }


def _metadata_output(rows: list[tuple[float, float]]) -> str:
    return "\n".join(
        f"frame:{index} pts:{index} pts_time:{timestamp}\n"
        f"lavfi.scene_score={score:.6f}"
        for index, (timestamp, score) in enumerate(rows)
    )


def test_legacy_config_migrates_to_fixed_without_silent_sampling_change():
    policy = resolved_sampling_policy({"frame_interval_seconds": 7, "frame_height": 720})
    assert policy["mode"] == "fixed"
    assert policy["baseline_interval_seconds"] == 7
    assert policy["migrated_from_fixed_interval"] is True


def test_loaded_legacy_yaml_keeps_fixed_mode_while_new_defaults_are_adaptive(tmp_path):
    legacy = tmp_path / "legacy.yaml"
    legacy.write_text("frame_interval_seconds: 7\n", encoding="utf-8")
    migrated = resolved_sampling_policy(load_config(str(legacy)))
    fresh = resolved_sampling_policy(load_config(str(tmp_path / "missing.yaml")))
    assert migrated["mode"] == "fixed"
    assert migrated["baseline_interval_seconds"] == 7
    assert migrated["migrated_from_fixed_interval"] is True
    assert fresh["mode"] == "adaptive"


def test_loaded_legacy_local_url_is_migrated_to_canonical_base_url(tmp_path):
    legacy = tmp_path / "legacy-local.yaml"
    legacy.write_text(
        "ai:\n  provider: local\n  local:\n    ollama_url: http://localhost:11434\n    model: vision\n",
        encoding="utf-8",
    )
    cfg = load_config(str(legacy))
    assert cfg["ai"]["local"]["base_url"] == "http://localhost:11434/v1"


def test_migrated_fixed_mode_preserves_legacy_integer_duration_boundary():
    cfg = {"frame_interval_seconds": 5, "frame_height": 720}
    plan = build_sampling_plan(Path("legacy.mp4"), 10.9, cfg)
    assert [sample["timestamp_seconds"] for sample in plan["samples"]] == [0.0, 5.0]


def test_fixed_sampling_applies_clip_and_minute_caps():
    cfg = {
        "frame_interval_seconds": 1,
        "frame_height": 720,
        "sampling": {
            "mode": "fixed",
            "max_frames_per_clip": 5,
            "max_frames_per_minute": 3,
        },
    }
    plan = build_sampling_plan(Path("legacy.mp4"), 60, cfg)

    assert len(plan["samples"]) == 3
    assert plan["candidate_counts"]["baseline"] == 60


def test_stable_minute_uses_far_fewer_calls_than_one_second_sampling(monkeypatch):
    monkeypatch.setattr(
        sampling.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stderr=_metadata_output([(index / 2, 0.0) for index in range(120)]),
            stdout="",
        ),
    )
    plan = build_sampling_plan(Path("stable.mp4"), 60, _cfg())
    assert len(plan["samples"]) < 60
    assert plan["candidate_counts"]["motion"] == 0
    assert plan["candidate_counts"]["scene"] == 0
    assert plan["samples"][0]["timestamp_seconds"] == 0
    assert plan["samples"][-1]["timestamp_seconds"] == pytest.approx(59.75)


def test_short_motion_between_baselines_and_hard_cut_get_candidates(monkeypatch):
    monkeypatch.setattr(
        sampling.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stderr=_metadata_output(
                [(0, 0), (0.5, 0), (1.0, 0), (1.5, 0.09), (2.0, 0.1), (5.0, 0.8)]
            ),
            stdout="",
        ),
    )
    plan = build_sampling_plan(Path("action.mp4"), 10, _cfg())
    motion = [
        row["timestamp_seconds"]
        for row in plan["samples"]
        if "motion" in row["reasons"]
    ]
    scene = [
        row["timestamp_seconds"]
        for row in plan["samples"]
        if "scene" in row["reasons"]
    ]
    assert any(1.0 <= timestamp <= 2.5 for timestamp in motion)
    assert any(timestamp < 5 for timestamp in scene)
    assert any(timestamp > 5 for timestamp in scene)
    timestamps = [row["timestamp_seconds"] for row in plan["samples"]]
    assert all(
        later - earlier >= 0.25
        for earlier, later in zip(timestamps, timestamps[1:])
    )


def test_sampling_is_deterministic_and_hard_capped(monkeypatch):
    output = _metadata_output([(index / 2, 0.1) for index in range(120)])
    monkeypatch.setattr(
        sampling.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stderr=output, stdout=""
        ),
    )
    cfg = _cfg(max_frames_per_clip=12, max_frames_per_minute=12)
    first = build_sampling_plan(Path("busy.mp4"), 60, cfg)
    second = build_sampling_plan(Path("busy.mp4"), 60, cfg)
    assert first == second
    assert len(first["samples"]) == 12
    source = {"size": 100, "mtime_ns": 2, "sample_sha256": "abc"}
    assert sampling_contract_hash(source, first["policy"], first["samples"]) == sampling_contract_hash(
        source, second["policy"], second["samples"]
    )


def test_sampling_hash_ignores_path_and_mtime_but_tracks_content():
    policy = resolved_sampling_policy(_cfg())
    samples = [{"timestamp_seconds": 0, "reasons": ["boundary"]}]
    first = {"path": "C:/one/clip.mp4", "size": 10, "mtime_ns": 1, "sample_sha256": "same"}
    moved = {"path": "D:/two/clip.mp4", "size": 10, "mtime_ns": 2, "sample_sha256": "same"}
    changed = {**moved, "sample_sha256": "changed"}

    assert sampling_contract_hash(first, policy, samples) == sampling_contract_hash(moved, policy, samples)
    assert sampling_contract_hash(first, policy, samples) != sampling_contract_hash(changed, policy, samples)


def test_adaptive_grouping_uses_effective_baseline_interval():
    frames = [
        {"timestamp_seconds": 0.0, "usefulness_score": 0.9, "tags": ["travel"], "summary": "一"},
        {"timestamp_seconds": 1.0, "usefulness_score": 0.9, "tags": ["landscape"], "summary": "二"},
    ]

    segments = merge_frames_to_segments(frames, 0.5, duration_seconds=2.0)

    assert len(segments) == 2


def test_short_clip_keeps_boundaries_and_estimate_respects_cap(monkeypatch):
    monkeypatch.setattr(
        sampling.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stderr=_metadata_output([(0, 0), (0.5, 0)]),
            stdout="",
        ),
    )
    plan = build_sampling_plan(Path("short.mp4"), 0.8, _cfg())
    assert [row["timestamp_seconds"] for row in plan["samples"]] == [0.0, 0.55]
    policy = resolved_sampling_policy(_cfg(max_frames_per_clip=3))
    assert estimate_sampling_count(60, policy) == 3


def test_long_clip_uses_a_wider_media_end_guard(monkeypatch):
    monkeypatch.setattr(
        sampling.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stderr=_metadata_output([(0, 0)]),
            stdout="",
        ),
    )
    plan = build_sampling_plan(Path("long.mp4"), 60, _cfg())

    assert plan["samples"][-1]["timestamp_seconds"] <= 59.75 + 1e-6


def test_prescan_failure_is_explicit(monkeypatch):
    monkeypatch.setattr(
        sampling.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stderr="Invalid data found", stdout=""
        ),
    )
    with pytest.raises(SamplingError, match="prescan failed"):
        build_sampling_plan(Path("broken.mp4"), 5, _cfg())


def test_cuda_prescan_is_explicit_and_audited(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stderr=_metadata_output([(0, 0), (0.5, 0)]),
            stdout="",
        )

    monkeypatch.setattr(sampling.subprocess, "run", fake_run)
    cfg = _cfg(hardware_decode="cuda")
    plan = build_sampling_plan(Path("cuda.mp4"), 1, cfg)

    assert calls[0][calls[0].index("-hwaccel") + 1] == "cuda"
    assert calls[0].index("-hwaccel") < calls[0].index("-i")
    assert plan["decode"] == {
        "requested": "cuda",
        "implementation": "cuda_nvdec",
        "ffmpeg_input_args": ["-hwaccel", "cuda"],
        "fallback": False,
    }


def test_unknown_hardware_decode_fails_closed_before_ffmpeg(monkeypatch):
    monkeypatch.setattr(sampling.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not run"))

    with pytest.raises(SamplingError, match="unsupported sampling hardware_decode"):
        build_sampling_plan(Path("unknown.mp4"), 1, _cfg(hardware_decode="magic"))


def test_visual_dedupe_preserves_boundaries_and_merges_reasons(tmp_path, monkeypatch):
    paths = [tmp_path / f"frame_{index:05d}.jpg" for index in range(3)]
    for path in paths:
        path.write_bytes(b"frame")
    signatures = {
        paths[0]: bytes([10]) * 256,
        paths[1]: bytes([10]) * 256,
        paths[2]: bytes([10]) * 256,
    }
    monkeypatch.setattr(sampling, "_visual_signature", lambda path, _cfg: signatures[path])
    samples = [
        {"timestamp_seconds": 0, "reasons": ["boundary"], "activity_score": 0},
        {"timestamp_seconds": 5, "reasons": ["baseline", "motion"], "activity_score": 0.1},
        {"timestamp_seconds": 10, "reasons": ["boundary"], "activity_score": 0},
    ]
    kept_paths, kept_samples, audit, discarded = dedupe_visual_samples(
        paths, samples, _cfg(), resolved_sampling_policy(_cfg())
    )
    assert kept_paths == [paths[0], paths[2]]
    assert {"baseline", "motion"}.issubset(set(kept_samples[0]["reasons"]))
    assert audit["status"] == "applied"
    assert audit["removed"] == 1
    assert audit["decision_contract"] == "visual-dedupe-decisions-v1"
    assert audit["decisions"][0]["dedupe_decision"] == "discarded_visual_duplicate"
    assert audit["decisions"][0]["rescue_outcome"] == "not_selected"
    assert discarded[0]["path"] == paths[1]
    assert paths[1].is_file(), "orchestrator owns deletion until rescue selection completes"


def test_provider_model_comes_from_active_provider_branch():
    local = _cfg()
    local["ai"] = {"provider": "local", "local": {"model": "qwen-vl"}}
    cloud = _cfg()
    cloud["ai"] = {"provider": "cloud", "cloud": {"model": "gpt-4.1-mini"}}
    assert resolved_ai_model(local) == "qwen-vl"
    assert resolved_ai_model(cloud) == "gpt-4.1-mini"


def test_mock_provider_audit_model_matches_provider():
    cfg = _cfg()

    assert resolved_ai_model(cfg) == provider_from_config(cfg).model == "rules"


def test_web_sampling_override_is_bounded_and_versionable():
    assert _sampling_override(
        {
            "sampling": {
                "mode": "adaptive",
                "preset": "dense",
                "baseline_interval_seconds": 2.5,
                "max_frames_per_clip": 240,
            }
        }
    ) == {
        "mode": "adaptive",
        "preset": "dense",
        "baseline_interval_seconds": 2.5,
        "max_frames_per_clip": 240,
    }
    with pytest.raises(ValueError, match="fixed 或 adaptive"):
        _sampling_override({"sampling": {"mode": "cloud"}})
    with pytest.raises(ValueError, match="介於"):
        _sampling_override({"sampling": {"max_frames_per_clip": 0}})


def test_adaptive_segment_bounds_follow_manifest_timestamps():
    frames = [
        {"timestamp_seconds": 0.0, "usefulness_score": 0.1, "tags": [], "summary": ""},
        {"timestamp_seconds": 1.25, "usefulness_score": 0.9, "tags": ["travel"], "summary": "第一段"},
        {"timestamp_seconds": 9.5, "usefulness_score": 0.9, "tags": ["landscape"], "summary": "第二段"},
    ]

    segments = merge_frames_to_segments(frames, 5.0, duration_seconds=10.0)

    assert segments[0]["start_seconds"] == pytest.approx(0.625)
    assert segments[0]["end_seconds"] == pytest.approx(5.375)
    assert segments[1]["start_seconds"] == pytest.approx(5.375)
    assert segments[1]["end_seconds"] == pytest.approx(10.0)

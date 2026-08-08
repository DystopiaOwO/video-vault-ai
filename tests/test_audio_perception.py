from __future__ import annotations

from array import array
from pathlib import Path
import math
import struct

import pytest

from video_vault.audio_perception import (
    AUDIO_PERCEPTION_VERSION,
    AudioPerceptionError,
    analyze_audio_file,
    analyze_pcm,
    audio_policy,
)
from video_vault.database import init_db, project_videos, upsert_video
from video_vault.perception_runs import analysis_run, create_perception_run, set_run_audio_perception
from video_vault.project import create_project
from video_vault.project import _merge_audio_user_decisions
import video_vault.project_perception as project_perception
from video_vault.project_perception import run_project_perception


def _pcm(values: list[int]) -> bytes:
    return struct.pack("<" + "h" * len(values), *values)


def _sine(sample_rate: int, seconds: float, amplitude: int = 8000, frequency: float = 220.0) -> bytes:
    return _pcm([
        int(amplitude * math.sin(2 * math.pi * frequency * index / sample_rate))
        for index in range(int(sample_rate * seconds))
    ])


def test_audio_policy_is_bounded_and_deterministic():
    cfg = {"perception": {"audio": {"sample_rate": 8000, "window_seconds": 1, "hop_seconds": 0.5}}}
    assert audio_policy(cfg) == audio_policy(cfg)
    with pytest.raises(ValueError, match="hop_seconds"):
        audio_policy({"perception": {"audio": {"window_seconds": 1, "hop_seconds": 2}}})


def test_local_audio_result_is_versioned_and_keeps_recommendation_separate_from_user_decision():
    result = analyze_pcm(
        _sine(8000, 2.0),
        sample_rate=8000,
        video_id=7,
        visual_segments=[{"segment_uuid": "visual-1", "start_seconds": 0, "end_seconds": 2}],
        policy={"window_seconds": 1.0, "hop_seconds": 1.0, "vad_threshold_db": -48.0},
    )
    assert result["schema_version"] == AUDIO_PERCEPTION_VERSION
    assert result["provider"] == "local"
    assert result["audit"] == {
        "local_only": True,
        "transcription_requested": False,
        "cloud_audio_requested": False,
        "user_decisions_overridden": False,
    }
    assert result["candidates"]
    assert {item["segment_uuid"] for item in result["candidates"]} == {"visual-1"}
    assert all(item["natural_audio_recommendation"] in {"keep", "mute", "duck"} for item in result["candidates"])
    assert all(item["user_audio_decision"] is None for item in result["candidates"])


def test_clipped_pcm_is_invalid_noise_and_recommends_mute():
    result = analyze_pcm(
        _pcm([32767, -32768] * 4000),
        sample_rate=8000,
        video_id=8,
        policy={"window_seconds": 1.0, "hop_seconds": 1.0},
    )
    assert result["candidates"][0]["event"] == "invalid_noise"
    assert result["candidates"][0]["natural_audio_recommendation"] == "mute"


def test_empty_pcm_is_auditable_no_audio_result():
    result = analyze_pcm(b"", sample_rate=8000, video_id=9, duration_seconds=3.5)
    assert result["status"] == "no_audio"
    assert result["timeline"]["duration_seconds"] == 3.5
    assert result["candidates"] == []


def test_audio_extraction_failure_is_fail_closed(monkeypatch, tmp_path: Path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"source")

    def fail(*_args, **_kwargs):
        raise OSError("ffmpeg unavailable")

    monkeypatch.setattr("video_vault.audio_perception.subprocess.run", fail)
    with pytest.raises(AudioPerceptionError, match="ffmpeg unavailable"):
        analyze_audio_file(source, {"ffmpeg_path": "ffmpeg"}, video_id=1)


def test_audio_audit_persists_with_existing_perception_run(tmp_path: Path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"immutable")
    video_id = upsert_video(db, {"original_path": str(source), "current_path": str(source), "filename": source.name, "duration_seconds": 2, "status": "uploaded"})
    project_id = create_project(db, "Audio", [video_id], category="travel")
    cfg = {"library_root": str(tmp_path), "frame_interval_seconds": 5, "ffmpeg_path": "ffmpeg", "ai": {"provider": "mock"}}
    run = create_perception_run(db, cfg, project_id, dict(project_videos(db, project_id)[0]))
    audit = analyze_pcm(_sine(8000, 1.0), sample_rate=8000, video_id=video_id)
    set_run_audio_perception(db, run["run_uuid"], audit)
    assert analysis_run(db, run["run_uuid"])["audio_perception"] == audit


def test_enabled_audio_perception_is_published_with_visual_run(tmp_path: Path, monkeypatch):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"immutable")
    video_id = upsert_video(db, {"original_path": str(source), "current_path": str(source), "filename": source.name, "duration_seconds": 2, "status": "uploaded"})
    project_id = create_project(db, "Audio", [video_id], category="travel")
    cfg = {
        "library_root": str(tmp_path),
        "frame_interval_seconds": 5,
        "frame_height": 720,
        "ffmpeg_path": "ffmpeg",
        "ffprobe_path": "ffprobe",
        "ai": {"provider": "mock", "model": "mock-v1"},
        "perception": {"audio": {"enabled": True}},
    }

    def fake_extract(_source: Path, out_dir: Path, _cfg: dict) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        result = [out_dir / "frame_00000.jpg"]
        result[0].write_bytes(b"frame")
        return result

    def fake_analyze(_video: dict, _cfg: dict, manifest: list[dict], progress=None, should_cancel=None) -> dict:
        del progress, should_cancel
        return {
            "provider": "mock",
            "model": "mock-v1",
            "frames": [{"frame_path": manifest[0]["frame_path"], "timestamp_seconds": 0, "summary": "景色", "tags": []}],
            "segments": [{"start_seconds": 0, "end_seconds": 2, "segment_type": "scene", "title": "scene", "score": 0.8}],
        }

    audio_audit = analyze_pcm(_sine(8000, 1.0), sample_rate=8000, video_id=video_id)
    monkeypatch.setattr(project_perception, "extract_frames", fake_extract)
    monkeypatch.setattr(project_perception, "analyze_frame_manifest", fake_analyze)
    monkeypatch.setattr(project_perception, "analyze_audio_file", lambda *args, **kwargs: audio_audit)
    monkeypatch.setattr(project_perception, "rename_after_perception", lambda _cfg, _db, video: video)
    monkeypatch.setattr(project_perception, "perceive_output", lambda *args, **kwargs: None)
    monkeypatch.setattr(project_perception, "write_plan_files", lambda *args, **kwargs: None)
    monkeypatch.setattr(project_perception, "draft_plan", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(project_perception, "build_project_plan", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(project_perception, "migrate_segment_state_for_video", lambda *_args, **_kwargs: [])

    result = run_project_perception(cfg, db, project_id, dict(project_videos(db, project_id)[0]))
    persisted = analysis_run(db, result["run"]["run_uuid"])["audio_perception"]
    assert persisted["schema_version"] == AUDIO_PERCEPTION_VERSION
    assert persisted["status"] == "succeeded"
    assert result["audio_perception"] == persisted


def test_human_audio_decision_is_exposed_as_effective_without_mutating_ai_recommendation():
    state = {"current_audio_perception": {"audit": {"user_decisions_overridden": False}, "candidates": [{
        "segment_uuid": "segment-1",
        "natural_audio_recommendation": "duck",
        "decision_source": "recommendation_only",
    }]}}
    merged = _merge_audio_user_decisions(state, [{"segment_id": "segment-1", "natural_audio_recommendation": "mute"}])
    candidate = merged["current_audio_perception"]["candidates"][0]
    assert candidate["natural_audio_recommendation"] == "duck"
    assert candidate["user_audio_decision"] == "mute"
    assert candidate["decision_source"] == "human"
    assert merged["current_audio_perception"]["audit"]["user_decisions_overridden"] is True
    assert "user_audio_decision" not in state["current_audio_perception"]["candidates"][0]

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from video_vault.audio_preview import AudioPreviewError, _source_fingerprint, audio_preview_file_path, render_project_audio_preview
from video_vault.audio_state import default_audio_state, save_audio_state
from video_vault.database import add_analysis, add_bgm_track, add_project_bgm, connect, init_db, upsert_video
from video_vault.project import build_project_plan, create_project, project_dir


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
pytestmark = [
    pytest.mark.media_e2e,
    pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg/ffprobe not installed"),
]


def _run(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)
    assert result.returncode == 0, result.stderr


def _make_source(path: Path, *, duration: float = 4.0) -> None:
    _run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=teal:s=640x360:r=30",
        "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=44100",
        "-t", str(duration), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "44100", "-ac", "1", "-shortest", str(path),
    ])


def _make_bgm(path: Path, *, frequency: int = 220, duration: float = 1.0) -> None:
    _run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000",
        "-t", str(duration), "-c:a", "aac", str(path),
    ])


def _make_video_only(path: Path, *, duration: float = 4.0) -> None:
    _run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"color=c=purple:s=640x360:r=30",
        "-t", str(duration), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
    ])


def _project(tmp_path: Path, *, count: int = 1) -> tuple[dict, Path, int, Path, Path]:
    library = tmp_path / "Audio Preview Library"
    library.mkdir()
    cfg = {"library_root": str(library), "ffmpeg_path": FFMPEG, "ffprobe_path": FFPROBE}
    db = library / "video_vault.sqlite3"
    init_db(db)
    source = tmp_path / "source.mp4"
    bgm = tmp_path / "bgm.mp4"
    _make_source(source)
    _make_bgm(bgm)
    sources = [source]
    videos = []
    for index in range(count):
        current = source if index == 0 else tmp_path / f"source-{index}.mp4"
        if index:
            _make_source(current)
            sources.append(current)
        video_id = upsert_video(db, {"original_path": str(current), "current_path": str(current), "filename": current.name, "category": "travel", "duration_seconds": 4})
        add_analysis(db, video_id, "mock", "audio-preview-e2e", {"segments": [{"start_seconds": 0, "end_seconds": 4, "segment_type": "key_action", "title": f"preview-{index}", "reason": "e2e", "tags": ["travel"], "score": 1, "suggested_use": "main"}]}, tmp_path / f"raw-{index}.json")
        videos.append(video_id)
    project_id = create_project(db, "Audio Preview E2E", videos, category="travel")
    build_project_plan(cfg, db, project_id)
    with connect(db) as connection:
        connection.execute("delete from project_bgm where project_id=?", (project_id,))
    return cfg, db, project_id, source, bgm


def test_audio_preview_cache_force_range_and_same_path_bgm_change(tmp_path: Path):
    cfg, db, project_id, source, bgm = _project(tmp_path)
    track_id = add_bgm_track(db, {"title": "Preview BGM", "artist": "Test", "file_path": str(bgm), "source_url": "https://example.com/bgm", "license_name": "CC0", "attribution_text": "Preview BGM"})
    add_project_bgm(db, project_id, track_id)
    state = default_audio_state()
    state["bgm"].update({"enabled": True, "bgm_id": track_id, "start_seconds": 0.25, "loop": True})
    save_audio_state(cfg, db, project_id, state, mark_review=False)

    first = render_project_audio_preview(cfg, db, project_id, timeline_start_seconds=0.5, duration_seconds=3)
    assert first["cache_hit"] is False
    assert first["timeline_start_seconds"] == pytest.approx(0.5)
    output = audio_preview_file_path(cfg, project_id, first["file"])
    assert output.exists()
    metadata = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
    assert metadata["contract_version"] == 2
    assert metadata["cache_payload"]["preview_range"]["start_seconds"] == pytest.approx(0.5)
    assert metadata["cache_payload"]["segments"][0]["source_sha256"]
    probe = subprocess.run([FFPROBE, "-v", "error", "-show_streams", "-of", "json", str(output)], capture_output=True, text=True, check=False)
    streams = json.loads(probe.stdout)["streams"]
    assert any(item.get("codec_type") == "video" for item in streams)
    assert any(item.get("codec_type") == "audio" for item in streams)

    second = render_project_audio_preview(cfg, db, project_id, timeline_start_seconds=0.5, duration_seconds=3)
    assert second["cache_hit"] is True and second["file"] == first["file"]
    forced = render_project_audio_preview(cfg, db, project_id, timeline_start_seconds=0.5, duration_seconds=3, force=True)
    assert forced["cache_hit"] is False

    _make_bgm(bgm, frequency=330)
    changed = render_project_audio_preview(cfg, db, project_id, timeline_start_seconds=0.5, duration_seconds=3)
    assert changed["cache_hit"] is False
    with pytest.raises((ValueError, FileNotFoundError)):
        audio_preview_file_path(cfg, project_id, "..\\..\\secret.mp4")


def test_transient_preview_can_use_unattached_global_bgm(tmp_path: Path):
    cfg, db, project_id, source, bgm = _project(tmp_path)
    track_id = add_bgm_track(db, {"title": "Transient BGM", "artist": "Test", "file_path": str(bgm), "source_url": "https://example.com/transient", "license_name": "CC0", "attribution_text": "Transient BGM"})
    patch = default_audio_state()
    patch["bgm"].update({"enabled": True, "bgm_id": track_id, "loop": True})
    result = render_project_audio_preview(
        cfg,
        db,
        project_id,
        duration_seconds=3,
        audio_patch=patch,
    )
    assert result["ok"] is True


def test_audio_preview_without_bgm_still_normalizes_audio(tmp_path: Path):
    cfg, db, project_id, source, bgm = _project(tmp_path)
    state = default_audio_state()
    state["bgm"].update({"enabled": False, "bgm_id": None})
    state["normalization"].update({"enabled": True, "target_lufs": -16, "true_peak_db": -2})
    save_audio_state(cfg, db, project_id, state, mark_review=False)

    result = render_project_audio_preview(cfg, db, project_id, duration_seconds=3)
    output = audio_preview_file_path(cfg, project_id, result["file"])
    probe = subprocess.run([FFPROBE, "-v", "error", "-show_streams", "-of", "json", str(output)], capture_output=True, text=True, check=False)
    audio = next(item for item in json.loads(probe.stdout)["streams"] if item.get("codec_type") == "audio")
    assert audio.get("sample_rate") == "48000"
    assert int(audio.get("channels") or 0) == 2


def test_audio_preview_source_without_audio_gets_silence_stream(tmp_path: Path):
    cfg, db, project_id, source, bgm = _project(tmp_path)
    _make_video_only(source)
    state = default_audio_state()
    state["bgm"].update({"enabled": False, "bgm_id": None})
    save_audio_state(cfg, db, project_id, state, mark_review=False)

    result = render_project_audio_preview(cfg, db, project_id, duration_seconds=3, force=True)
    output = audio_preview_file_path(cfg, project_id, result["file"])
    probe = subprocess.run([FFPROBE, "-v", "error", "-show_streams", "-of", "json", str(output)], capture_output=True, text=True, check=False)
    audio = next(item for item in json.loads(probe.stdout)["streams"] if item.get("codec_type") == "audio")
    assert audio.get("sample_rate") == "48000"
    assert int(audio.get("channels") or 0) == 2


def test_audio_preview_rejects_invalid_duration(tmp_path: Path):
    cfg, db, project_id, source, bgm = _project(tmp_path)
    with pytest.raises(AudioPreviewError, match="3 到 30"):
        render_project_audio_preview(cfg, db, project_id, duration_seconds=2)


def test_unrelated_segment_audio_change_does_not_invalidate_preview_cache(tmp_path: Path):
    cfg, db, project_id, source, bgm = _project(tmp_path, count=2)
    state = default_audio_state()
    state["bgm"].update({"enabled": False, "bgm_id": None})
    save_audio_state(cfg, db, project_id, state, mark_review=False)
    first = render_project_audio_preview(cfg, db, project_id, timeline_start_seconds=0.2, duration_seconds=3)
    detail = __import__("video_vault.project", fromlist=["project_detail"]).project_detail(cfg, db, project_id)
    second_id = detail["segments"][1]["segment_id"]
    update = {"segments": {second_id: {"fade_in_seconds": 0.7}}}
    save_audio_state(cfg, db, project_id, {**state, **update}, mark_review=False)
    cached = render_project_audio_preview(cfg, db, project_id, timeline_start_seconds=0.2, duration_seconds=3)
    assert cached["cache_hit"] is True
    assert cached["file"] == first["file"]


def test_preview_segment_audio_change_invalidates_preview_cache(tmp_path: Path):
    cfg, db, project_id, source, bgm = _project(tmp_path)
    state = default_audio_state()
    state["bgm"].update({"enabled": False, "bgm_id": None})
    save_audio_state(cfg, db, project_id, state, mark_review=False)
    first = render_project_audio_preview(cfg, db, project_id, timeline_start_seconds=0.2, duration_seconds=3)
    segment_id = __import__("video_vault.project", fromlist=["project_detail"]).project_detail(cfg, db, project_id)["segments"][0]["segment_id"]
    state["segments"] = {segment_id: {"fade_in_seconds": 0.7}}
    save_audio_state(cfg, db, project_id, state, mark_review=False)
    changed = render_project_audio_preview(cfg, db, project_id, timeline_start_seconds=0.2, duration_seconds=3)
    assert changed["cache_hit"] is False
    assert changed["file"] != first["file"]


def test_preview_contract_version_invalidates_old_cache(tmp_path: Path, monkeypatch):
    cfg, db, project_id, source, bgm = _project(tmp_path)
    state = default_audio_state()
    state["bgm"].update({"enabled": False, "bgm_id": None})
    save_audio_state(cfg, db, project_id, state, mark_review=False)
    first = render_project_audio_preview(cfg, db, project_id, duration_seconds=3)
    monkeypatch.setattr("video_vault.audio_preview.AUDIO_PREVIEW_CONTRACT_VERSION", 3)
    changed = render_project_audio_preview(cfg, db, project_id, duration_seconds=3)
    assert changed["cache_hit"] is False
    assert changed["file"] != first["file"]


def test_segment_preview_reports_actual_project_timeline_start(tmp_path: Path):
    cfg, db, project_id, source, bgm = _project(tmp_path, count=2)
    state = default_audio_state()
    state["bgm"].update({"enabled": False, "bgm_id": None})
    save_audio_state(cfg, db, project_id, state, mark_review=False)
    from video_vault.project import project_detail

    segment_id = project_detail(cfg, db, project_id)["segments"][1]["segment_id"]
    result = render_project_audio_preview(cfg, db, project_id, segment_id=segment_id, duration_seconds=3)
    assert result["timeline_start_seconds"] == pytest.approx(4.0, abs=0.1)


def test_source_same_size_same_mtime_content_change_changes_fingerprint(tmp_path: Path):
    source = tmp_path / "fingerprint.bin"
    source.write_bytes(b"AAAA")
    before = _source_fingerprint(source)
    mtime = source.stat().st_mtime_ns
    source.write_bytes(b"BBBB")
    os.utime(source, ns=(mtime, mtime))
    after = _source_fingerprint(source)
    assert before["size"] == after["size"]
    assert before["mtime_ns"] == after["mtime_ns"]
    assert before["sha256"] != after["sha256"]

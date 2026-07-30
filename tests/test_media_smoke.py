from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from video_vault.media_probe import probe_media
from video_vault.database import init_db, project_videos, upsert_video
from video_vault.project import create_project
from video_vault.project_perception import run_project_perception


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
pytestmark = [
    pytest.mark.media_smoke,
    pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg/ffprobe not installed"),
]


def test_tiny_media_pipeline_preserves_source_and_decodes_frame(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "source.mp4"
    source_command = [
        FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=160x90:rate=10",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=8000",
        "-t",
        "0.6",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-ar",
        "8000",
        "-ac",
        "1",
        "-shortest",
        str(source),
    ]
    created = subprocess.run(source_command, capture_output=True, text=True, check=False)
    assert created.returncode == 0, created.stderr

    before_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    import video_vault.segment_renderer as renderer

    tiny_profile = {
        "profile_id": "smoke_160x90",
        "width": 160,
        "height": 90,
        "fps": 10,
        "video_codec": "h264",
        "pixel_format": "yuv420p",
        "audio_codec": "aac",
        "audio_sample_rate": 8000,
        "audio_channels": 1,
    }
    monkeypatch.setattr(renderer, "get_render_profile", lambda profile_id: dict(tiny_profile))
    manifest = {
        "project_id": 1,
        "profile": {"profile_id": "smoke_160x90"},
        "settings": {"encoder": "cpu", "color": {"mode": "none"}, "audio": {}},
    }
    segment = {
        "segment_id": "smoke_001",
        "clip_id": "smoke_001",
        "video_id": 1,
        "source_file": str(source),
        "source_in_seconds": 0.0,
        "source_out_seconds": 0.5,
        "speed": 1.0,
        "timeline_duration_seconds": 0.5,
        "audio_role": "keep_original",
    }
    cfg = {"ffmpeg_path": FFMPEG, "ffprobe_path": FFPROBE, "library_root": str(tmp_path)}
    result = renderer.render_segment(cfg, manifest, segment, cache_root=tmp_path / "cache")
    assert result.cache_hit is False
    assert result.output_path.is_file() and result.output_path.stat().st_size > 0
    assert result.duration_seconds == pytest.approx(0.5, abs=0.2)

    metadata_path = result.output_path.with_suffix(".json")
    assert metadata_path.is_file()
    assert json.loads(metadata_path.read_text(encoding="utf-8"))["cache_key"] == result.cache_key

    second = renderer.render_segment(cfg, manifest, segment, cache_root=tmp_path / "cache")
    assert second.cache_hit is True

    probed = probe_media(FFPROBE, result.output_path)
    assert (probed.width, probed.height) == (160, 90)
    assert probed.fps == pytest.approx(10, abs=0.1)
    assert probed.duration_seconds == pytest.approx(0.5, abs=0.2)

    decoded = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(result.output_path), "-frames:v", "1", "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert decoded.returncode == 0, decoded.stderr
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before_hash
    assert not list(tmp_path.rglob("*.partial*"))
    assert not list(tmp_path.rglob("*.tmp"))


def test_adaptive_perception_publishes_a_short_clip_without_tail_decode_failure(tmp_path: Path):
    source = tmp_path / "short-perception.mp4"
    created = subprocess.run(
        [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000",
            "-t", "0.6", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "8000", "-ac", "1",
            "-shortest", str(source),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert created.returncode == 0, created.stderr

    db = tmp_path / "short-perception.sqlite3"
    init_db(db)
    video_id = upsert_video(
        db,
        {
            "original_path": str(source),
            "current_path": str(source),
            "filename": source.name,
            "category": "test",
            "duration_seconds": 0.6,
            "status": "uploaded",
        },
    )
    project_id = create_project(db, "short-perception", [video_id], category="test")
    cfg = {
        "library_root": str(tmp_path),
        "ffmpeg_path": FFMPEG,
        "ffprobe_path": FFPROBE,
        "frame_height": 120,
        "frame_interval_seconds": 0.5,
        "sampling": {
            "mode": "adaptive",
            "baseline_interval_seconds": 0.5,
            "prescan_interval_seconds": 0.2,
            "max_frames_per_clip": 10,
            "max_frames_per_minute": 60,
        },
        "ai": {"provider": "mock", "model": "rules"},
    }

    result = run_project_perception(
        cfg,
        db,
        project_id,
        dict(project_videos(db, project_id)[0]),
    )

    assert result["run"]["status"] == "succeeded"
    assert result["frames"]
    assert all(0 <= frame["timestamp_seconds"] <= 0.6 for frame in result["frames"])

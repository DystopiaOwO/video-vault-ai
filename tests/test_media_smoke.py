from __future__ import annotations

import hashlib
import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from video_vault.media_probe import probe_media
from video_vault.database import init_db, project_videos, upsert_video
from video_vault.project import create_project
from video_vault.project_perception import run_project_perception
import video_vault.ui as ui


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


def test_non_square_sar_uses_display_geometry_before_final_square_pixels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import video_vault.segment_renderer as renderer

    source = tmp_path / "sar-source.mp4"
    created = subprocess.run(
        [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=red:size=720x480:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000",
            "-t", "0.6", "-vf", "setsar=4/3", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "8000", "-ac", "1",
            "-shortest", str(source),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert created.returncode == 0, created.stderr

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
        "segment_id": "sar_001",
        "clip_id": "sar_001",
        "video_id": 1,
        "source_file": str(source),
        "source_in_seconds": 0.0,
        "source_out_seconds": 0.5,
        "speed": 1.0,
        "timeline_duration_seconds": 0.5,
        "audio_role": "keep_original",
    }
    cfg = {"ffmpeg_path": FFMPEG, "ffprobe_path": FFPROBE, "library_root": str(tmp_path)}
    result = renderer.render_segment(cfg, manifest, segment, cache_root=tmp_path / "sar-cache")
    assert result.output_path.is_file()

    source_probe = probe_media(FFPROBE, source, mode="fast")
    output_probe = probe_media(FFPROBE, result.output_path, mode="fast")
    assert source_probe.sample_aspect_ratio == "4:3"
    assert source_probe.display_aspect_ratio == "2:1"
    assert (output_probe.width, output_probe.height) == (160, 90)
    assert output_probe.sample_aspect_ratio == "1:1"
    assert output_probe.display_aspect_ratio == "16:9"

    def first_rgb_frame(path: Path) -> bytes:
        decoded = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(path), "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
            capture_output=True,
            check=False,
        )
        assert decoded.returncode == 0, decoded.stderr.decode("utf-8", errors="replace")
        assert len(decoded.stdout) == 160 * 90 * 3
        return decoded.stdout

    frame = first_rgb_frame(result.output_path)
    top_left = frame[0:3]
    content_left = frame[(5 * 160) * 3:(5 * 160 + 1) * 3]
    assert max(top_left) < 20
    assert content_left[0] > 150 and content_left[1] < 80 and content_left[2] < 80

    square_source = tmp_path / "square-equivalent.mp4"
    square_created = subprocess.run(
        [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=red:size=960x480:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000",
            "-t", "0.6", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "8000", "-ac", "1", "-shortest", str(square_source),
        ],
        capture_output=True,
        check=False,
    )
    assert square_created.returncode == 0, square_created.stderr.decode("utf-8", errors="replace")
    square_segment = dict(segment, segment_id="sar_square_001", source_file=str(square_source))
    square_result = renderer.render_segment(cfg, manifest, square_segment, cache_root=tmp_path / "square-cache")
    assert first_rgb_frame(square_result.output_path) == frame


def _patch_video_display_matrix_90(source: Path, output: Path) -> None:
    """Add a real MP4 video-track Display Matrix to a tiny synthetic fixture."""

    data = bytearray(source.read_bytes())
    containers = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}

    def boxes(start: int, end: int):
        position = start
        while position + 8 <= end:
            size = struct.unpack(">I", data[position:position + 4])[0]
            if size == 0:
                size = end - position
            if size < 8 or position + size > end:
                return
            yield position, size, bytes(data[position + 4:position + 8])
            position += size

    def find_box(start: int, end: int, wanted: bytes):
        for position, size, box_type in boxes(start, end):
            if box_type == wanted:
                return position, size
            if box_type in containers:
                found = find_box(position + 8, position + size, wanted)
                if found:
                    return found
        return None

    moov = find_box(0, len(data), b"moov")
    assert moov is not None
    video_tkhd = None
    for trak_position, trak_size, box_type in boxes(moov[0] + 8, moov[0] + moov[1]):
        if box_type != b"trak":
            continue
        handler = find_box(trak_position + 8, trak_position + trak_size, b"hdlr")
        tkhd = find_box(trak_position + 8, trak_position + trak_size, b"tkhd")
        if handler and tkhd and data[handler[0] + 16:handler[0] + 20] == b"vide":
            video_tkhd = tkhd[0]
            break
    assert video_tkhd is not None
    assert data[video_tkhd + 8] == 0  # version 0 tkhd used by the smoke fixture
    matrix = struct.pack(">9i", 0, 65536, 0, -65536, 0, 0, 0, 0, 1073741824)
    data[video_tkhd + 48:video_tkhd + 84] = matrix
    output.write_bytes(data)


def test_rotated_non_square_sar_matches_canonical_upright_media(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import video_vault.segment_renderer as renderer

    coded = tmp_path / "coded-landscape-sar.mp4"
    created = subprocess.run(
        [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=red:size=720x480:rate=10",
            "-t", "0.6", "-vf", "drawbox=x=0:y=0:w=360:h=480:color=blue:t=fill,setsar=4/3",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(coded),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert created.returncode == 0, created.stderr

    rotated = tmp_path / "rotated-sar.mp4"
    _patch_video_display_matrix_90(coded, rotated)
    rotated_probe = probe_media(FFPROBE, rotated, mode="fast")
    assert rotated_probe.rotation_degrees == -90
    assert rotated_probe.sample_aspect_ratio == "4:3"
    assert rotated_probe.display_aspect_ratio == "1:2"

    canonical = tmp_path / "canonical-upright.mp4"
    canonical_created = subprocess.run(
        [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(rotated),
            "-vf", "scale=360:720:flags=neighbor,setsar=1", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", str(canonical),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert canonical_created.returncode == 0, canonical_created.stderr
    canonical_probe = probe_media(FFPROBE, canonical, mode="fast")
    assert (canonical_probe.width, canonical_probe.height) == (360, 720)
    assert canonical_probe.sample_aspect_ratio == "1:1"
    assert canonical_probe.display_aspect_ratio == "1:2"

    profile = {
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
    monkeypatch.setattr(renderer, "get_render_profile", lambda profile_id: dict(profile))
    manifest = {"project_id": 1, "profile": {"profile_id": profile["profile_id"]}, "settings": {"encoder": "cpu", "color": {"mode": "none"}, "audio": {}}}
    cfg = {"ffmpeg_path": FFMPEG, "ffprobe_path": FFPROBE, "library_root": str(tmp_path)}

    def render(source: Path, segment_id: str, cache_name: str):
        return renderer.render_segment(
            cfg,
            manifest,
            {"segment_id": segment_id, "clip_id": segment_id, "video_id": 1, "source_file": str(source), "source_in_seconds": 0.0, "source_out_seconds": 0.5, "speed": 1.0, "timeline_duration_seconds": 0.5, "audio_role": "mute"},
            cache_root=tmp_path / cache_name,
        )

    rotated_result = render(rotated, "rotated_001", "rotated-cache")
    canonical_result = render(canonical, "canonical_001", "canonical-cache")
    rotated_output = probe_media(FFPROBE, rotated_result.output_path, mode="fast")
    canonical_output = probe_media(FFPROBE, canonical_result.output_path, mode="fast")
    assert (rotated_output.width, rotated_output.height) == (160, 90)
    assert (canonical_output.width, canonical_output.height) == (160, 90)
    assert rotated_output.sample_aspect_ratio == canonical_output.sample_aspect_ratio == "1:1"
    assert rotated_output.display_aspect_ratio == canonical_output.display_aspect_ratio == "16:9"

    def sample_rgb(path: Path) -> list[tuple[int, int, int]]:
        decoded = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(path), "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
            capture_output=True,
            check=False,
        )
        assert decoded.returncode == 0, decoded.stderr.decode("utf-8", errors="replace")
        return [tuple(decoded.stdout[offset:offset + 3]) for offset in range(0, len(decoded.stdout), 3)]

    rotated_frame = sample_rgb(rotated_result.output_path)
    canonical_frame = sample_rgb(canonical_result.output_path)
    assert len(rotated_frame) == len(canonical_frame) == 160 * 90
    assert sum(abs(a - b) for left, right in zip(rotated_frame, canonical_frame) for a, b in zip(left, right)) < 160 * 90 * 12
    center_column = [rotated_frame[row * 160 + 80] for row in range(0, 90, 10)]
    assert center_column[0] != center_column[-1]


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
        # This smoke test intentionally exercises the legacy single-frame
        # contract; formal multi-frame perception must opt in explicitly.
        "perception": {"multi_frame": {"enabled": False}},
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


def test_production_multiframe_perception_smoke_publishes_evidence_and_keeps_get_read_only(tmp_path: Path):
    source = tmp_path / "multiframe-smoke.mp4"
    created = subprocess.run(
        [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000",
            "-t", "1.2", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "8000", "-ac", "1",
            "-shortest", str(source),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert created.returncode == 0, created.stderr

    before_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    before_stat = source.stat()
    db = tmp_path / "multiframe-smoke.sqlite3"
    init_db(db)
    video_id = upsert_video(
        db,
        {
            "original_path": str(source),
            "current_path": str(source),
            "filename": source.name,
            "category": "coffee",
            "duration_seconds": 1.2,
            "status": "uploaded",
        },
    )
    project_id = create_project(db, "multiframe-smoke", [video_id], category="coffee")
    cfg = {
        "library_root": str(tmp_path),
        "ffmpeg_path": FFMPEG,
        "ffprobe_path": FFPROBE,
        "frame_interval_seconds": 0.2,
        "frame_height": 90,
        "sampling": {
            "mode": "fixed",
            "baseline_interval_seconds": 0.2,
            "max_frames_per_clip": 10,
            "max_frames_per_minute": 60,
            "visual_dedupe_threshold": 0.0,
        },
        "perception": {"multi_frame": {"enabled": True, "min_frames": 3, "max_frames": 5}},
        "ai": {"provider": "mock", "mock": {"model": "rules"}},
    }

    result = run_project_perception(
        cfg,
        db,
        project_id,
        dict(project_videos(db, project_id)[0]),
        sampling_override={"mode": "fixed", "baseline_interval_seconds": 0.2},
    )
    run = result["run"]
    assert run["status"] == "succeeded"
    assert run["window_validation"]["status"] == "pass"
    assert run["sampling_manifest"]["actual_vision_calls"] == 1
    window = run["window_results"][0]
    assert 3 <= len(window["frame_timestamps"]) <= 5
    assert window["publish_status"] == "published"
    assert window["segment_uuid"]

    evidence_dir = Path(run["staging_path"]) / "evidence" / window["window_uuid"]
    contact_sheet = evidence_dir / "contact_sheet.jpg"
    assert contact_sheet.is_file() and contact_sheet.stat().st_size > 0
    decoded = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(contact_sheet), "-frames:v", "1", "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert decoded.returncode == 0, decoded.stderr

    resolved = ui.perception_evidence_path(
        cfg,
        db,
        project_id,
        run["run_uuid"],
        window["window_uuid"],
        "contact_sheet.jpg",
    )
    assert resolved == contact_sheet.resolve()

    staging_root = Path(run["staging_path"])
    evidence_root = staging_root / "evidence"
    root_mtime = evidence_root.stat().st_mtime_ns
    missing_window = evidence_root / "missing-window"
    with pytest.raises(FileNotFoundError):
        ui.perception_evidence_path(
            cfg,
            db,
            project_id,
            run["run_uuid"],
            "missing-window",
            "window.json",
        )
    assert not missing_window.exists()
    assert evidence_root.stat().st_mtime_ns == root_mtime
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before_hash
    assert source.stat().st_size == before_stat.st_size
    assert source.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert not list(tmp_path.rglob("*.partial*"))
    assert not list(tmp_path.rglob("*.tmp"))

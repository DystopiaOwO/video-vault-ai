from pathlib import Path
import hashlib
import json
import shutil
import subprocess

import pytest

from video_vault.database import create_project_row, init_db
from video_vault.delivery_qa import run_delivery_qa
from video_vault.project import project_dir
from video_vault.segment_cache import build_segment_cache_key


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.media_e2e
@pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg/ffprobe not installed")
def test_real_ffmpeg_delivery_qa_creates_evidence_without_touching_source(tmp_path: Path):
    source = tmp_path / "旅行原始素材.mp4"
    command = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000",
        "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "2", "-shortest", "-movflags", "+faststart", str(source),
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)
    assert result.returncode == 0, result.stderr
    original_hash = _sha(source)
    library = tmp_path / "isolated-library"
    library.mkdir()
    cfg = {"library_root": str(library), "ffmpeg_path": FFMPEG, "ffprobe_path": FFPROBE, "delivery_qa": {"timeout_seconds": 120}}
    db = library / "video_vault.sqlite3"
    init_db(db)
    project_id = create_project_row(db, "Travel QA", content_type="travel_diary")
    folder = project_dir(cfg, project_id)
    output = folder / "renders" / "formal.mp4"
    output.parent.mkdir(parents=True)
    shutil.copyfile(source, output)
    manifest_hash = "c" * 64
    manifest = {
        "project_id": project_id,
        "manifest_hash": manifest_hash,
        "profile": {"width": 320, "height": 180, "fps": 30.0, "pixel_format": "yuv420p", "video_codec": "h264", "audio_codec": "aac", "audio_sample_rate": 48000, "audio_channels": 2},
        "expected_duration_seconds": 3,
        "segments": [{"segment_id": "stable-segment-1", "source_file": str(source), "source_in_seconds": 0, "source_out_seconds": 3, "timeline_duration_seconds": 3, "audio_role": "keep_original", "group_id": "chapter-1"}],
        "visual_timeline": {"resolved_duration_seconds": 3, "resolved_items": [{"stable_id": "chapter-card-1", "type": "chapter_card", "group_id": "chapter-1", "start_seconds": 0, "duration_seconds": 1, "style_id": "location-lower-left"}]},
        "settings": {},
        "bgm": [],
    }
    snapshot = {"snapshot_id": "approval-real", "snapshot_hash": "d" * 64, "manifest_hash": manifest_hash, "manifest": manifest, "assets": [{"canonical_path": str(source), "sha256": original_hash, "kind": "source_media"}]}
    output.with_name(output.name + ".render.json").write_text(json.dumps({
        "manifest_hash": manifest_hash,
        "output_sha256": _sha(output),
        "loudness": {"final": {"measured_I": -14, "measured_TP": -1.2}},
        "segments": [{"cache_key": build_segment_cache_key(manifest, manifest["segments"][0])}],
        "bgm": {"used": False, "fingerprint": {}},
        "qc": {"passed": True},
        "measurements": {"decode": {"ok": True}, "timestamp_monotonic": True},
    }), encoding="utf-8")

    report = run_delivery_qa(cfg, db, project_id, render_job_uuid="real-render-job", output_path=output, approval_snapshot=snapshot, render_manifest_hash=manifest_hash)

    run_root = folder / "qa" / report["qa_run_uuid"]
    assert report["summary"]["blocked"] == 0, report["checks"]
    assert next(check for check in report["checks"] if check["check_id"] == "audio")["status"] == "pass"
    assert (run_root / "report.json").is_file()
    assert (run_root / "REPORT.md").is_file()
    assert (run_root / "artifact-index.json").is_file()
    assert (run_root / "overview-contact-sheet.jpg").is_file()
    assert (run_root / "waveform-or-audio-summary" / "audio-summary.json").is_file()
    assert _sha(source) == original_hash
    assert source.name not in (run_root / "report.json").read_text(encoding="utf-8")


@pytest.mark.media_e2e
@pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg/ffprobe not installed")
def test_real_ffmpeg_brightness_flash_switch_creates_timestamped_warning(tmp_path: Path):
    output_source = tmp_path / "rapid-flash.mp4"
    command = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=black:size=320x180:rate=30:duration=0.4",
        "-f", "lavfi", "-i", "color=c=white:size=320x180:rate=30:duration=0.4",
        "-f", "lavfi", "-i", "color=c=black:size=320x180:rate=30:duration=0.4",
        "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000:duration=1.2",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map", "[v]", "-map", "3:a", "-t", "1.2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "2", "-movflags", "+faststart",
        str(output_source),
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)
    assert result.returncode == 0, result.stderr

    source = tmp_path / "flash-source.mp4"
    source.write_bytes(b"approved source")
    library = tmp_path / "isolated-library"
    library.mkdir()
    cfg = {"library_root": str(library), "ffmpeg_path": FFMPEG, "ffprobe_path": FFPROBE, "delivery_qa": {"timeout_seconds": 120}}
    db = library / "video_vault.sqlite3"
    init_db(db)
    project_id = create_project_row(db, "Flash QA", content_type="travel_diary")
    folder = project_dir(cfg, project_id)
    output = folder / "renders" / "flash.mp4"
    output.parent.mkdir(parents=True)
    shutil.copyfile(output_source, output)
    manifest_hash = "e" * 64
    manifest = {
        "project_id": project_id,
        "manifest_hash": manifest_hash,
        "profile": {"width": 320, "height": 180, "fps": 30.0, "pixel_format": "yuv420p", "video_codec": "h264", "audio_codec": "aac", "audio_sample_rate": 48000, "audio_channels": 2},
        "expected_duration_seconds": 1.2,
        "segments": [{"segment_id": "flash-segment", "source_file": str(source), "source_in_seconds": 0, "source_out_seconds": 1.2, "timeline_duration_seconds": 1.2, "audio_role": "keep_original", "group_id": "chapter-1"}],
        "visual_timeline": {"resolved_duration_seconds": 1.2, "resolved_items": []},
        "settings": {},
        "bgm": [],
    }
    snapshot = {"snapshot_id": "approval-flash", "snapshot_hash": "f" * 64, "manifest_hash": manifest_hash, "manifest": manifest, "assets": [{"canonical_path": str(source), "sha256": _sha(source), "kind": "source_media"}]}
    output.with_name(output.name + ".render.json").write_text(json.dumps({
        "manifest_hash": manifest_hash,
        "output_sha256": _sha(output),
        "segments": [{"cache_key": build_segment_cache_key(manifest, manifest["segments"][0])}],
        "bgm": {"used": False, "fingerprint": {}},
        "qc": {"passed": True},
        "measurements": {"decode": {"ok": True}, "timestamp_monotonic": True},
    }), encoding="utf-8")

    report = run_delivery_qa(cfg, db, project_id, render_job_uuid="flash-render-job", output_path=output, approval_snapshot=snapshot, render_manifest_hash=manifest_hash)
    check = next(item for item in report["checks"] if item["check_id"] == "black_flash")
    assert check["status"] == "warning", check
    assert check["metrics"]["flash_event_count"] >= 2
    assert all(event.get("timestamp_seconds") is not None for event in check["metrics"]["events"] if event.get("kind") == "flash")
    assert check["evidence_artifact_ids"]

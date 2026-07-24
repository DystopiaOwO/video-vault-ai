import re
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from video_vault.media_probe import probe_media
from video_vault.segment_cache import cache_paths
from video_vault.segment_renderer import render_segment


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
pytestmark = [
    pytest.mark.media_e2e,
    pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg/ffprobe not installed"),
]


def _run(command):
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)
    assert result.returncode == 0, result.stderr


def _make_source(path: Path, *, size: str, rate: int, duration: float, audio: bool):
    command = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate={rate}"]
    if audio:
        command += ["-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000"]
    command += ["-t", str(duration), "-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if audio:
        command += ["-c:a", "aac", "-shortest"]
    command += [str(path)]
    _run(command)


def _manifest(source: Path, *, segment_id="clip_001", start=0.0, end=2.0, speed=1.0, role="keep_original", settings=None):
    return {
        "project_id": 1,
        "profile": {"profile_id": "accurate_preview_1080p"},
        "settings": {"encoder": "cpu", "color": {"mode": "none", "lut_path": ""}, "audio": {"original_gain_db": 0, "lower_original_gain_db": -12}, **(settings or {})},
        "segments": [],
    }, {"segment_id": segment_id, "clip_id": segment_id, "video_id": 1, "source_file": str(source), "source_in_seconds": start, "source_out_seconds": end, "source_duration_seconds": end - start, "speed": speed, "timeline_duration_seconds": (end - start) / speed, "audio_role": role}


def _cfg(tmp_path):
    return {"library_root": str(tmp_path), "ffmpeg_path": FFMPEG, "ffprobe_path": FFPROBE}


def _mean_volume(path: Path) -> float:
    result = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", os.devnull], capture_output=True, text=True, encoding="utf-8", check=False)
    match = re.search(r"mean_volume:\s*(-?inf|[-+]?\d+(?:\.\d+)?) dB", result.stderr)
    assert match, result.stderr
    return float("-inf") if match.group(1) == "-inf" else float(match.group(1))


def _identity_lut(path: Path):
    path.write_text("\n".join(["TITLE \"identity\"", "LUT_3D_SIZE 2", "DOMAIN_MIN 0 0 0", "DOMAIN_MAX 1 1 1", "0 0 0", "0 0 1", "0 1 0", "0 1 1", "1 0 0", "1 0 1", "1 1 0", "1 1 1"]) + "\n", encoding="utf-8")


def test_real_renderer_handles_audio_silent_speed_cache_corruption_and_lut(tmp_path: Path):
    source = tmp_path / "with-audio.mp4"
    silent = tmp_path / "silent.mp4"
    sixty = tmp_path / "sixty-fps.mp4"
    _make_source(source, size="1280x720", rate=24, duration=3, audio=True)
    _make_source(silent, size="640x360", rate=30, duration=3, audio=False)
    _make_source(sixty, size="1280x720", rate=60, duration=1.2, audio=True)
    cfg = _cfg(tmp_path)
    cache_root = tmp_path / "cache"

    manifest, segment = _manifest(source, start=1.25, end=2.875)
    first = render_segment(cfg, manifest, segment, cache_root=cache_root)
    assert not first.cache_hit and first.output_path.exists()
    probe = probe_media(FFPROBE, first.output_path)
    assert (probe.width, probe.height, probe.fps, probe.pixel_format) == (1920, 1080, 30, "yuv420p")
    assert probe.has_audio and (probe.sample_rate, probe.channels) == (48000, 2)
    assert probe.duration_seconds == pytest.approx(1.625, abs=0.12)

    hit = render_segment(cfg, manifest, segment, cache_root=cache_root)
    assert hit.cache_hit and hit.cache_key == first.cache_key
    first.output_path.write_bytes(b"corrupt")
    rebuilt = render_segment(cfg, manifest, segment, cache_root=cache_root)
    assert not rebuilt.cache_hit and rebuilt.output_path.stat().st_size > 1000
    paths = cache_paths(cache_root, rebuilt.cache_key)
    assert paths["partial"].name.endswith(".partial.mp4")

    silent_manifest, silent_segment = _manifest(silent, end=2)
    silent_result = render_segment(cfg, silent_manifest, silent_segment, cache_root=cache_root)
    silent_probe = probe_media(FFPROBE, silent_result.output_path)
    assert silent_probe.has_audio and silent_probe.sample_rate == 48000 and silent_probe.channels == 2

    half_manifest, half_segment = _manifest(source, segment_id="half", end=2, speed=0.5)
    half_probe = probe_media(FFPROBE, render_segment(cfg, half_manifest, half_segment, cache_root=cache_root).output_path)
    assert half_probe.duration_seconds == pytest.approx(4, abs=0.15)
    double_manifest, double_segment = _manifest(source, segment_id="double", end=2, speed=2)
    double_probe = probe_media(FFPROBE, render_segment(cfg, double_manifest, double_segment, cache_root=cache_root).output_path)
    assert double_probe.duration_seconds == pytest.approx(1, abs=0.12)

    keep_manifest, keep_segment = _manifest(source, segment_id="keep", end=1, role="keep_original")
    lower_manifest, lower_segment = _manifest(source, segment_id="lower", end=1, role="lower_original")
    mute_manifest, mute_segment = _manifest(source, segment_id="mute", end=1, role="mute")
    keep = _mean_volume(render_segment(cfg, keep_manifest, keep_segment, cache_root=cache_root).output_path)
    lower = _mean_volume(render_segment(cfg, lower_manifest, lower_segment, cache_root=cache_root).output_path)
    mute = _mean_volume(render_segment(cfg, mute_manifest, mute_segment, cache_root=cache_root).output_path)
    assert lower < keep - 6
    # AAC encoding can leave a very small quantization floor instead of -inf.
    assert mute <= -80

    lut_dir = tmp_path / "LUT dir,semi;[test]'quote"
    lut_dir.mkdir()
    lut = lut_dir / "identity look.cube"
    _identity_lut(lut)
    lut_manifest, lut_segment = _manifest(source, segment_id="lut", end=1, settings={"color": {"mode": "dji_lut", "lut_path": str(lut)}})
    lut_result = render_segment(cfg, lut_manifest, lut_segment, cache_root=cache_root)
    assert lut_result.output_path.exists()
    lut.write_text(lut.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
    changed_lut = render_segment(cfg, lut_manifest, lut_segment, cache_root=cache_root)
    assert changed_lut.cache_key != lut_result.cache_key and not changed_lut.cache_hit

    sixty_manifest, sixty_segment = _manifest(sixty, segment_id="sixty", end=1)
    sixty_probe = probe_media(FFPROBE, render_segment(cfg, sixty_manifest, sixty_segment, cache_root=cache_root).output_path)
    assert (sixty_probe.width, sixty_probe.height, sixty_probe.fps, sixty_probe.pixel_format) == (1920, 1080, 30, "yuv420p")
    assert sixty_probe.has_audio and (sixty_probe.sample_rate, sixty_probe.channels) == (48000, 2)
    assert sixty_probe.duration_seconds == pytest.approx(1, abs=0.12)

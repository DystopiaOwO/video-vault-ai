import json
import math
import os
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from video_vault.database import add_analysis, add_bgm_track, add_project_bgm, connect, init_db, upsert_video
from video_vault.audio_state import default_audio_state, save_audio_state
from video_vault.project import build_project_plan, create_project, project_detail, project_dir, save_segment_review, set_review_status
from video_vault.project_renderer import render_project
from video_vault.storyboard import generate_storyboard


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
pytestmark = [
    pytest.mark.media_e2e,
    pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg/ffprobe not installed"),
]


def _run(command):
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)
    assert result.returncode == 0, result.stderr


def _make_color_source(path: Path, color: str, rate: int, frequency: int, duration: float = 1.5):
    _run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"color=c={color}:s=1280x720:r={rate}",
        "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000",
        "-t", str(duration), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    ])


def _make_bgm(path: Path, duration: float = 0.5):
    _run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000",
        "-t", str(duration), "-c:a", "aac", str(path),
    ])


def _create_project(cfg, db: Path, sources: list[Path], *, bgm: Path | None = None, loop: bool = True, gain_db: float = -12) -> int:
    videos = []
    for index, source in enumerate(sources):
        video_id = upsert_video(db, {"original_path": str(source), "current_path": str(source), "filename": source.name, "category": "travel", "duration_seconds": 1.5})
        add_analysis(db, video_id, "mock", "e2e", {"segments": [{"start_seconds": 0, "end_seconds": 1.5, "segment_type": "key_action", "title": source.stem, "reason": "e2e", "tags": ["travel"], "score": 1, "suggested_use": "main"}]}, source.with_suffix(".raw.json"))
        videos.append(video_id)
    project_id = create_project(db, f"E2E {sources[0].stem}", videos, category="travel")
    build_project_plan(cfg, db, project_id)
    # Story planning may auto-recommend a library track; keep this fixture explicit.
    with connect(db) as con:
        con.execute("delete from project_bgm where project_id=?", (project_id,))
    generate_storyboard(cfg, db, project_id)
    if bgm:
        track_id = add_bgm_track(db, {"title": "E2E BGM", "artist": "Test", "file_path": str(bgm), "source_url": "https://example.com/bgm", "license_name": "CC0", "license_url": "https://example.com/license", "attribution_required": 0, "attribution_text": "E2E", "mood": "calm", "duration_seconds": 0.5})
        add_project_bgm(db, project_id, track_id)
        settings_path = project_dir(cfg, project_id) / "render_settings.json"
        settings = {"profile_id": "final_1080p", "encoder": "cpu", "color": {"mode": "none", "lut_path": ""}, "audio": {"original_gain_db": 0, "lower_original_gain_db": -12, "bgm_gain_db": -12}, "transition": {"type": "cut", "duration_seconds": 0}, "overlay": {"enabled": False}, "bgm": [{"track_id": track_id, "loop": loop, "gain_db": -12, "fade_in_seconds": 0.1, "fade_out_seconds": 0.1}]}
        settings["bgm"][0]["gain_db"] = gain_db
        settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    set_review_status(cfg, db, project_id, "approved")
    return project_id


def _sample_rgb(path: Path, seconds: float) -> tuple[float, float, float]:
    result = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-ss", str(seconds), "-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=False)
    assert result.returncode == 0 and result.stdout
    values = result.stdout
    return tuple(sum(values[index::3]) / max(1, len(values[index::3])) for index in range(3))


def _tone_amplitude(path: Path, seconds: float, frequency: float) -> float:
    result = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-ss", str(seconds), "-i", str(path), "-t", "0.3", "-vn", "-map", "0:a:0", "-ac", "1", "-ar", "8000", "-f", "f32le", "-"],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0 and len(result.stdout) >= 32, result.stderr.decode(errors="replace")
    count = len(result.stdout) // 4
    samples = struct.unpack(f"<{count}f", result.stdout[: count * 4])
    real = sum(sample * math.cos(2 * math.pi * frequency * index / 8000) for index, sample in enumerate(samples))
    imaginary = sum(sample * math.sin(2 * math.pi * frequency * index / 8000) for index, sample in enumerate(samples))
    return 2 * math.hypot(real, imaginary) / count


def test_project_renderer_assembles_order_bgm_and_final_cache(tmp_path: Path):
    library = tmp_path / "Project dir,semi;[test]'quote 中文"
    library.mkdir()
    cfg = {"library_root": str(library), "ffmpeg_path": FFMPEG, "ffprobe_path": FFPROBE}
    db = library / "video_vault.sqlite3"
    init_db(db)
    red = tmp_path / "red.mp4"
    blue = tmp_path / "blue.mp4"
    _make_color_source(red, "red", 24, 880)
    _make_color_source(blue, "blue", 60, 660)

    project_id = _create_project(cfg, db, [red, blue])
    first = render_project(cfg, db, project_id)
    assert not first.cache_hit
    assert first.duration_seconds == pytest.approx(3, abs=0.2)
    assert _sample_rgb(first.output_path, 0.5)[0] > _sample_rgb(first.output_path, 0.5)[2]
    assert _sample_rgb(first.output_path, 2.0)[2] > _sample_rgb(first.output_path, 2.0)[0]
    assert first.output_path.with_name(first.output_path.name + ".render.json").exists()

    hit = render_project(cfg, db, project_id)
    assert hit.cache_hit
    first.output_path.write_bytes(b"broken")
    rebuilt = render_project(cfg, db, project_id)
    assert not rebuilt.cache_hit and rebuilt.output_path.stat().st_size > 1000
    report = rebuilt.output_path.with_name(rebuilt.output_path.name + ".render.json")
    report.unlink()
    rebuilt_report = render_project(cfg, db, project_id)
    assert not rebuilt_report.cache_hit and report.exists()

    bgm = tmp_path / "short bgm.mp4"
    _make_bgm(bgm)
    loop_project = _create_project(cfg, db, [red], bgm=bgm, loop=True)
    loop_result = render_project(cfg, db, loop_project)
    assert loop_result.bgm_used and loop_result.duration_seconds == pytest.approx(1.5, abs=0.2)
    assert _tone_amplitude(loop_result.output_path, 1.1, 220) > 0.002
    nonloop_project = _create_project(cfg, db, [red], bgm=bgm, loop=False)
    nonloop_result = render_project(cfg, db, nonloop_project)
    assert nonloop_result.bgm_used and nonloop_result.duration_seconds == pytest.approx(1.5, abs=0.2)
    assert _tone_amplitude(nonloop_result.output_path, 1.1, 880) > _tone_amplitude(nonloop_result.output_path, 1.1, 220) * 1.5
    gain_zero_project = _create_project(cfg, db, [red], bgm=bgm, gain_db=0)
    gain_zero_result = render_project(cfg, db, gain_zero_project)
    gain_low_project = _create_project(cfg, db, [red], bgm=bgm, gain_db=-18)
    gain_low_result = render_project(cfg, db, gain_low_project)
    assert _tone_amplitude(gain_zero_result.output_path, 0.3, 220) > _tone_amplitude(gain_low_result.output_path, 0.3, 220) * 1.5


def _create_audio_role_project(cfg, db: Path, source: Path, role: str, *, speed: float = 1.0, bgm: Path | None = None) -> int:
    project_id = _create_project(cfg, db, [source])
    detail = project_detail(cfg, db, project_id)
    segment = detail["segments"][0]
    segment["speed"] = speed
    save_segment_review(cfg, db, project_id, [segment])
    state = default_audio_state()
    state["normalization"]["enabled"] = False
    state["original_audio"]["default_role"] = role
    if bgm is not None:
        track_id = add_bgm_track(db, {"title": "Global role BGM", "artist": "Test", "file_path": str(bgm), "source_url": "https://example.com/role-bgm", "license_name": "CC0", "attribution_text": "role BGM"})
        state["bgm"].update({"bgm_id": track_id, "enabled": True, "loop": True, "volume_db": 0})
    save_audio_state(cfg, db, project_id, state, mark_review=False)
    set_review_status(cfg, db, project_id, "approved")
    return project_id


@pytest.mark.parametrize("role", ["keep", "lower", "mute"])
def test_real_ffmpeg_original_audio_roles(tmp_path: Path, role: str):
    library = tmp_path / f"roles-{role}"
    library.mkdir()
    cfg = {"library_root": str(library), "ffmpeg_path": FFMPEG, "ffprobe_path": FFPROBE}
    db = library / "video_vault.sqlite3"
    init_db(db)
    source = tmp_path / f"role-{role}.mp4"
    _make_color_source(source, "green", 30, 880)
    project_id = _create_audio_role_project(cfg, db, source, role)
    result = render_project(cfg, db, project_id)
    probe = json.loads(subprocess.run([FFPROBE, "-v", "error", "-show_streams", "-of", "json", str(result.output_path)], capture_output=True, text=True, check=False).stdout)
    audio = next(stream for stream in probe["streams"] if stream.get("codec_type") == "audio")
    assert audio.get("sample_rate") == "48000"
    assert int(audio.get("channels") or 0) == 2
    amplitude = _tone_amplitude(result.output_path, 0.3, 880)
    if role == "mute":
        assert amplitude < 0.01
    else:
        assert amplitude > 0.01


def test_real_ffmpeg_bgm_only_uses_unattached_global_track(tmp_path: Path):
    library = tmp_path / "bgm-only"
    library.mkdir()
    cfg = {"library_root": str(library), "ffmpeg_path": FFMPEG, "ffprobe_path": FFPROBE}
    db = library / "video_vault.sqlite3"
    init_db(db)
    source = tmp_path / "bgm-only-source.mp4"
    bgm = tmp_path / "bgm-only.mp4"
    _make_color_source(source, "green", 30, 880)
    _make_bgm(bgm, duration=1.0)
    project_id = _create_audio_role_project(cfg, db, source, "bgm_only", bgm=bgm)
    result = render_project(cfg, db, project_id)
    assert result.bgm_used
    assert _tone_amplitude(result.output_path, 0.4, 220) > 0.01


@pytest.mark.parametrize(("speed", "expected"), [(0.5, 3.0), (2.0, 0.75)])
def test_real_ffmpeg_speed_keeps_audio_video_aligned(tmp_path: Path, speed: float, expected: float):
    library = tmp_path / f"speed-{speed}"
    library.mkdir()
    cfg = {"library_root": str(library), "ffmpeg_path": FFMPEG, "ffprobe_path": FFPROBE}
    db = library / "video_vault.sqlite3"
    init_db(db)
    source = tmp_path / f"speed-{speed}.mp4"
    _make_color_source(source, "green", 30, 880)
    project_id = _create_audio_role_project(cfg, db, source, "keep", speed=speed)
    result = render_project(cfg, db, project_id)
    probe = json.loads(subprocess.run([FFPROBE, "-v", "error", "-show_streams", "-of", "json", str(result.output_path)], capture_output=True, text=True, check=False).stdout)
    streams = probe["streams"]
    assert any(stream.get("codec_type") == "video" for stream in streams)
    assert any(stream.get("codec_type") == "audio" for stream in streams)
    assert result.duration_seconds == pytest.approx(expected, abs=0.15)

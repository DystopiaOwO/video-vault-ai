import json
from pathlib import Path
import shutil
import subprocess

import pytest

from video_vault.audio_state import default_audio_state, save_audio_state
from video_vault.database import add_analysis, init_db, upsert_video
from video_vault.project import build_project_plan, create_project
from video_vault.storyboard import generate_storyboard, generate_thumbnail
from video_vault.storyboard_preview import render_storyboard_preview, storyboard_preview_path


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
pytestmark = [
    pytest.mark.media_e2e,
    pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg/ffprobe not installed"),
]


def _project(tmp_path: Path) -> tuple[dict, Path, int]:
    db = tmp_path / "db.sqlite3"
    init_db(db)
    source = tmp_path / "storyboard-source.mp4"
    command = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "10", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source)]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)
    assert result.returncode == 0, result.stderr
    video_id = upsert_video(db, {"original_path": str(source), "current_path": str(source), "filename": source.name, "category": "travel", "duration_seconds": 10})
    add_analysis(db, video_id, "mock", "rules", {"segments": [
        {"start_seconds": 0, "end_seconds": 5, "segment_type": "key_action", "title": "抵達", "reason": "first", "tags": ["travel"], "score": 0.9, "suggested_use": "main"},
        {"start_seconds": 5, "end_seconds": 10, "segment_type": "detail", "title": "用餐", "reason": "second", "tags": ["food"], "score": 0.8, "suggested_use": "B-roll"},
    ]}, tmp_path / "raw.json")
    cfg = {"library_root": str(tmp_path), "ffmpeg_path": FFMPEG, "ffprobe_path": FFPROBE}
    project_id = create_project(db, "Storyboard preview", [video_id], category="travel", content_type="travel_diary")
    build_project_plan(cfg, db, project_id)
    generate_storyboard(cfg, db, project_id)
    return cfg, db, project_id


def test_thumbnail_generation_and_cache_contract(tmp_path: Path):
    cfg, db, project_id = _project(tmp_path)
    state = json.loads((tmp_path / "08_projects" / f"project_{project_id}" / "storyboard.json").read_text(encoding="utf-8"))
    segment_id = next(iter(state["segments"]))
    shutil.rmtree(tmp_path / "08_projects" / f"project_{project_id}" / "cache" / "storyboard")

    first = generate_thumbnail(cfg, db, project_id, segment_id, 0.5)
    second = generate_thumbnail(cfg, db, project_id, segment_id, 0.5)
    changed = generate_thumbnail(cfg, db, project_id, segment_id, 0.75)

    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert changed["file"] != first["file"]
    probe = subprocess.run([FFPROBE, "-v", "error", "-show_streams", "-of", "json", str(tmp_path / "08_projects" / f"project_{project_id}" / "cache" / "storyboard" / first["file"])], capture_output=True, text=True, encoding="utf-8", check=False)
    streams = json.loads(probe.stdout)["streams"]
    assert 0 < int(streams[0]["width"]) <= 640

    state = default_audio_state()
    save_audio_state(cfg, db, project_id, state, mark_review=False)
    assert generate_thumbnail(cfg, db, project_id, segment_id, 0.5)["cache_hit"] is True


def test_segment_preview_is_at_most_five_seconds(tmp_path: Path):
    cfg, db, project_id = _project(tmp_path)
    segment_id = next(iter(json.loads((tmp_path / "08_projects" / f"project_{project_id}" / "storyboard.json").read_text(encoding="utf-8"))["segments"]))

    result = render_storyboard_preview(cfg, db, project_id, mode="segment", segment_id=segment_id)

    assert result["duration_seconds"] <= 5.01
    assert storyboard_preview_path(cfg, project_id, result["file"]).is_file()


def test_transition_preview_contains_adjacent_segments(tmp_path: Path):
    cfg, db, project_id = _project(tmp_path)
    state = json.loads((tmp_path / "08_projects" / f"project_{project_id}" / "storyboard.json").read_text(encoding="utf-8"))
    segment_ids = list(state["segments"])

    result = render_storyboard_preview(cfg, db, project_id, mode="transition", segment_id=segment_ids[0])

    assert [item["kind"] for item in result["previews"]] == ["outgoing"]
    assert result["previews"][0]["duration_seconds"] == pytest.approx(4.0, abs=0.2)
    probe = subprocess.run([FFPROBE, "-v", "error", "-show_streams", "-of", "json", str(storyboard_preview_path(cfg, project_id, result["file"]))], capture_output=True, text=True, encoding="utf-8", check=False)
    assert {stream["codec_type"] for stream in json.loads(probe.stdout)["streams"]} >= {"video", "audio"}


def test_transition_preview_returns_incoming_slice(tmp_path: Path):
    cfg, db, project_id = _project(tmp_path)
    state = json.loads((tmp_path / "08_projects" / f"project_{project_id}" / "storyboard.json").read_text(encoding="utf-8"))
    segment_ids = list(state["segments"])

    result = render_storyboard_preview(cfg, db, project_id, mode="transition", segment_id=segment_ids[-1])

    assert [item["kind"] for item in result["previews"]] == ["incoming"]
    assert result["previews"][0]["duration_seconds"] == pytest.approx(4.0, abs=0.2)


def test_single_segment_transition_preview(tmp_path: Path):
    cfg, db, project_id = _project(tmp_path)
    state = json.loads((tmp_path / "08_projects" / f"project_{project_id}" / "storyboard.json").read_text(encoding="utf-8"))
    segment_ids = list(state["segments"])
    state["segments"][segment_ids[1]]["included"] = False
    (tmp_path / "08_projects" / f"project_{project_id}" / "storyboard.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    result = render_storyboard_preview(cfg, db, project_id, mode="transition", segment_id=segment_ids[0])

    assert [item["kind"] for item in result["previews"]] == ["outgoing"]
    assert result["previews"][0]["duration_seconds"] <= 4.2


def test_range_preview_honors_timeline_start_and_transient_state(tmp_path: Path):
    cfg, db, project_id = _project(tmp_path)
    state_path = tmp_path / "08_projects" / f"project_{project_id}" / "storyboard.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    before = state_path.read_text(encoding="utf-8")

    result = render_storyboard_preview(cfg, db, project_id, mode="range", timeline_start_seconds=3, duration_seconds=5, storyboard_state=state)

    assert result["timeline_start_seconds"] == pytest.approx(3, abs=0.01)
    assert state_path.read_text(encoding="utf-8") == before

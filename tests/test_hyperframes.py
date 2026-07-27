from pathlib import Path

import pytest

from video_vault.database import add_analysis, init_db, upsert_video
from video_vault.hyperframes import export_hyperframes_project, render_fast_draft, unload_local_llm_model
from video_vault.opencut import export_opencut_handoff as real_opencut_handoff
from video_vault.project import create_project


def test_hyperframes_export_writes_html_timeline(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video = tmp_path / "20260617_081500_travel.mp4"
    video.write_bytes(b"v")
    video_id = upsert_video(db, {"original_path": str(video), "current_path": str(video), "filename": video.name, "category": "travel", "duration_seconds": 10})
    add_analysis(db, video_id, "mock", "rules", {"segments": [{"start_seconds": 0, "end_seconds": 5, "segment_type": "scene", "title": "station", "reason": "ok", "tags": ["travel"], "score": 1, "suggested_use": "B-roll"}]}, tmp_path / "raw.json")
    project_id = create_project(db, "trip", [video_id], category="travel", content_type="travel_diary")

    out = export_hyperframes_project({"library_root": str(tmp_path)}, db, project_id, render_clips=False)

    html = Path(out, "index.html").read_text(encoding="utf-8")
    assert 'data-composition-id="story"' in html
    assert "<video" in html
    assert Path(out, "timeline.json").exists()


def test_needs_review_project_can_export_hyperframes_preview_but_not_mp4(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video = tmp_path / "20260617_081500_travel.mp4"
    video.write_bytes(b"v")
    video_id = upsert_video(db, {"original_path": str(video), "current_path": str(video), "filename": video.name, "category": "travel", "duration_seconds": 10})
    add_analysis(db, video_id, "mock", "rules", {"segments": [{"start_seconds": 0, "end_seconds": 5, "segment_type": "scene", "title": "station", "reason": "ok", "tags": ["travel"], "score": 1, "suggested_use": "B-roll"}]}, tmp_path / "raw.json")
    cfg = {"library_root": str(tmp_path)}
    project_id = create_project(db, "trip", [video_id], category="travel", content_type="travel_diary")

    out = export_hyperframes_project(cfg, db, project_id, render_clips=False)

    assert Path(out, "index.html").exists()
    with pytest.raises(PermissionError):
        render_fast_draft(out, cfg, db=db, project_id=project_id)


def test_unload_local_llm_model_uses_configured_model(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        class Proc:
            returncode = 0
            stdout = "999"
            stderr = ""
        return Proc()

    monkeypatch.setattr("video_vault.hyperframes.subprocess.run", fake_run)
    monkeypatch.setattr("video_vault.hyperframes.shutil.which", lambda name: "lms")

    result = unload_local_llm_model({"ai": {"local": {"model": "gemma-4-12b-it"}}})

    assert result["model"] == "gemma-4-12b-it"
    assert seen["cmd"] == ["lms", "unload", "gemma-4-12b-it"]


def test_render_fast_draft_encodes_from_concat_once(tmp_path, monkeypatch):
    project = tmp_path / "hf"
    media = project / "media"
    media.mkdir(parents=True)
    (media / "a.mp4").write_bytes(b"x")
    (project / "timeline.json").write_text('{"clips":[{"file":"a.mp4"}]}', encoding="utf-8")
    segment_calls = []
    encode_calls = []

    monkeypatch.setattr("video_vault.hyperframes.unload_local_llm_model", lambda cfg: {"ok": True})
    monkeypatch.setattr("video_vault.hyperframes.subprocess.run", lambda cmd, **kwargs: segment_calls.append(cmd) or (project / "fast_segments" / "001_000000_a.mp4").write_bytes(b"x" * 2048))
    monkeypatch.setattr("video_vault.hyperframes.run_ffmpeg", lambda cmd, cfg: encode_calls.append(cmd))

    result = render_fast_draft(project, {"ffmpeg_path": "ffmpeg", "color": {"video_encoder": "h264_nvenc"}})

    assert result["ok"]
    assert "-ss" in segment_calls[0] and "-t" in segment_calls[0]
    assert len(encode_calls) == 1
    assert "-f" in encode_calls[0] and "concat" in encode_calls[0]
    assert "-c:v" in encode_calls[0] and "h264_nvenc" in encode_calls[0]


def test_hyperframes_render_export_skips_graded_clip_prerender(tmp_path, monkeypatch):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video = tmp_path / "20260617_081500_travel.mp4"
    video.write_bytes(b"v")
    video_id = upsert_video(db, {"original_path": str(video), "current_path": str(video), "filename": video.name, "category": "travel", "duration_seconds": 10})
    add_analysis(db, video_id, "mock", "rules", {"segments": [{"start_seconds": 0, "end_seconds": 5, "segment_type": "scene", "title": "station", "reason": "ok", "tags": ["travel"], "score": 1, "suggested_use": "B-roll"}]}, tmp_path / "raw.json")
    project_id = create_project(db, "trip", [video_id], category="travel", content_type="travel_diary")
    calls = []

    monkeypatch.setattr("video_vault.hyperframes.unload_local_llm_model", lambda cfg: {"ok": True})
    monkeypatch.setattr("video_vault.hyperframes.export_opencut_handoff", lambda cfg, db, project_id, render_clips, max_segments: calls.append(render_clips) or real_opencut_handoff(cfg, db, project_id, render_clips, max_segments))

    with pytest.raises(PermissionError, match="正式交付"):
        export_hyperframes_project({"library_root": str(tmp_path)}, db, project_id, render_clips=True)

    assert calls == []

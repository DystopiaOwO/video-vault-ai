from pathlib import Path

from video_vault.color_consistency import (
    analyze_project_color,
    default_color_state,
    effective_color_settings,
    normalize_color_state,
    preview_cache_key,
    render_project_color_previews,
    save_project_color_state,
)
from video_vault.database import connect, init_db, upsert_video
from video_vault.project import create_project


def _project(tmp_path: Path):
    cfg = {"library_root": str(tmp_path), "ffmpeg_path": "ffmpeg"}
    db = tmp_path / "05_index" / "video_vault.sqlite3"
    init_db(db)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    video_id = upsert_video(db, {"original_path": str(source), "current_path": str(source), "filename": source.name, "category": "travel"})
    project_id = create_project(db, "travel", [video_id], category="travel", content_type="travel_diary")
    return cfg, db, project_id, source, video_id


def test_color_state_keeps_suggested_and_applied_separate_and_clamps():
    state = normalize_color_state({"suggested": {"exposure": 2}, "applied": {"exposure": -2, "saturation": 5}})
    assert state["suggested"]["exposure"] == 1.0
    assert state["applied"]["exposure"] == -1.5
    assert state["applied"]["saturation"] == 1.2
    assert state["suggested"] is not state["applied"]


def test_analysis_selects_highest_scoring_reference_and_creates_suggestions(tmp_path, monkeypatch):
    cfg, db, project_id, source, video_id = _project(tmp_path)
    with connect(db) as con:
        con.execute("insert into segments(video_id, start_seconds, end_seconds, title, score) values(?, ?, ?, ?, ?)", (video_id, 0, 5, "車站", 0.4))
        con.execute("insert into segments(video_id, start_seconds, end_seconds, title, score) values(?, ?, ?, ?, ?)", (video_id, 20, 30, "核心畫面", 0.95))
    monkeypatch.setattr("video_vault.color_consistency._reference_luma", lambda cfg, reference: {"average": 190, "highlight_ratio": 0.2, "sampled_frames": 1})
    state = analyze_project_color(cfg, db, project_id)
    assert state["reference"]["label"] == "核心畫面"
    assert state["suggested"]["exposure"] < 0
    assert state["applied"] == state["suggested"]


def test_effective_color_settings_respects_enable_and_exclude():
    state = default_color_state()
    state["enabled"] = False
    state["applied"].update({"mode": "manual", "exposure": 0.4})
    assert effective_color_settings(state)["mode"] == "none"
    state["enabled"] = True
    state["segments"]["seg-1"] = {"enabled": True, "locked": False, "excluded": True}
    assert effective_color_settings(state, "seg-1")["mode"] == "none"


def test_preview_cache_key_changes_when_applied_color_changes(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    state = default_color_state()
    first = preview_cache_key(source, state)
    changed = normalize_color_state({**state, "applied": {**state["applied"], "exposure": 0.3}})
    assert preview_cache_key(source, changed) != first


def test_project_before_after_preview_reuses_cache(tmp_path, monkeypatch):
    cfg, db, project_id, source, _ = _project(tmp_path)
    state = default_color_state()
    save_project_color_state(cfg, db, project_id, {**state, "analysis": {"basis_text": "test"}}, mark_review=False)
    calls = []

    def fake_preview(source_path, output, cfg, *args, **kwargs):
        calls.append((str(output), kwargs.get("color_settings")))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"preview")
        return output

    monkeypatch.setattr("video_vault.color.render_color_preview", fake_preview)
    first = render_project_color_previews(cfg, db, project_id)
    second = render_project_color_previews(cfg, db, project_id)
    assert len(calls) == 2
    assert first["previews"][0]["cache_hit"] is False
    assert second["previews"][0]["cache_hit"] is True

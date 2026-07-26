from pathlib import Path
import json

import pytest

from video_vault.color_consistency import analyze_project_color, default_color_state, save_project_color_state
from video_vault.database import init_db, project_revision, set_project_status, upsert_video
from video_vault.project import create_project, project_dir, save_segment_review
from video_vault.project_lifecycle import ProjectRevisionConflict
from video_vault.ui import ProjectRevisionRequired, _require_api_base_revision


def _project_with_segment(tmp_path: Path):
    cfg = {"library_root": str(tmp_path)}
    db = tmp_path / "db.sqlite3"
    init_db(db)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    video_id = upsert_video(
        db,
        {
            "original_path": str(source),
            "current_path": str(source),
            "filename": source.name,
            "duration_seconds": 30.0,
            "category": "travel",
        },
    )
    project_id = create_project(db, "issue66", [video_id])
    folder = project_dir(cfg, project_id)
    plan = {
        "status": "needs_review",
        "groups": [{"segments": [{
            "segment_id": "segment-1",
            "clip_id": "clip-1",
            "video_id": video_id,
            "start_seconds": 0.0,
            "end_seconds": 5.0,
            "title": "opening",
            "score": 0.8,
            "include": True,
        }]}],
    }
    (folder / "project_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (folder / "review_status.json").write_text(json.dumps({"status": "needs_review", "approved_by_user": False}), encoding="utf-8")
    set_project_status(db, project_id, "needs_review")
    return cfg, db, project_id


def test_nested_noop_review_writer_does_not_clear_segment_change_revision(tmp_path: Path):
    cfg, db, project_id = _project_with_segment(tmp_path)
    assert project_revision(db, project_id) == 1
    changed = [{"segment_id": "segment-1", "start_seconds": 0.5, "end_seconds": 5.0, "speed": 1.0}]

    save_segment_review(cfg, db, project_id, changed, base_revision=1)
    assert project_revision(db, project_id) == 2

    save_segment_review(cfg, db, project_id, changed, base_revision=2)
    assert project_revision(db, project_id) == 2


def test_color_analyze_stale_revision_preserves_state_and_current_revision_can_retry(tmp_path: Path, monkeypatch):
    cfg, db, project_id = _project_with_segment(tmp_path)
    save_project_color_state(cfg, db, project_id, default_color_state(), mark_review=False, base_revision=1)
    current = project_revision(db, project_id)
    path = project_dir(cfg, project_id) / "color_consistency.json"
    before = path.read_bytes()

    with pytest.raises(ProjectRevisionConflict):
        analyze_project_color(cfg, db, project_id, force=True, base_revision=current - 1)
    assert path.read_bytes() == before
    assert project_revision(db, project_id) == current

    import video_vault.color_consistency as color_module

    def analyzed(*args, **kwargs):
        state = default_color_state()
        state["analysis"] = {"basis_text": "retry succeeded"}
        save_project_color_state(cfg, db, project_id, state, mark_review=False)
        return state

    monkeypatch.setattr(color_module, "_analyze_project_color", analyzed)
    result = analyze_project_color(cfg, db, project_id, force=True, base_revision=current)
    assert result["analysis"]
    assert project_revision(db, project_id) == current + 1


def test_modern_color_api_requires_base_revision_while_legacy_writer_is_explicit():
    with pytest.raises(ProjectRevisionRequired):
        _require_api_base_revision({"project_id": 1})
    assert _require_api_base_revision({"project_id": 1, "base_revision": 9}) == 9

import json

from video_vault.database import init_db, upsert_video
from video_vault.project import build_project_plan, can_project_render, create_project, set_review_status


def _project(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    source = tmp_path / "20260718_120000_food.mp4"
    source.write_bytes(b"source")
    video_id = upsert_video(db, {"original_path": str(source), "current_path": str(source),
                                 "filename": source.name, "category": "food", "duration_seconds": 5})
    project_id = create_project(db, "測試", [video_id], category="food")
    build_project_plan({"library_root": str(tmp_path)}, db, project_id)
    return {"library_root": str(tmp_path)}, db, project_id


def test_project_gate_requires_approval_and_accepts_current_approval(tmp_path):
    cfg, db, project_id = _project(tmp_path)
    allowed, reason = can_project_render(cfg, db, project_id)
    assert not allowed and "needs_review" in reason

    set_review_status(cfg, db, project_id, "approved")
    allowed, reason = can_project_render(cfg, db, project_id)
    assert allowed, reason


def test_project_gate_detects_plan_change_after_approval(tmp_path):
    cfg, db, project_id = _project(tmp_path)
    set_review_status(cfg, db, project_id, "approved")
    plan_path = tmp_path / "08_projects" / f"project_{project_id}" / "project_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["title_cards"] = [{"where": "開頭", "text": "changed"}]
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    allowed, reason = can_project_render(cfg, db, project_id)
    assert not allowed and "重新核准" in reason

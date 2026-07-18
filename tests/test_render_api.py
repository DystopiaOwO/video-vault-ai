import json

import pytest

from video_vault.database import init_db, upsert_video
from video_vault.project import build_project_plan, create_project
from video_vault.render_api import RenderApiError, compile_project, update_render_settings


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


def test_compile_project_writes_manifest(tmp_path):
    cfg, db, project_id = _project(tmp_path)
    result = compile_project(cfg, db, project_id)
    assert result["ok"]
    assert result["manifest_hash"]
    assert (tmp_path / "08_projects" / f"project_{project_id}" / "render" / "manifest" / "render_manifest.json").exists()


def test_invalid_profile_is_structured_and_settings_invalidate(tmp_path):
    cfg, db, project_id = _project(tmp_path)
    with pytest.raises(RenderApiError) as error:
        update_render_settings(cfg, db, project_id, {"profile": "not-a-profile"})
    assert error.value.code == "invalid_profile"

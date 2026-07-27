from pathlib import Path

import pytest

from video_vault import ui
from video_vault.project_lifecycle import ProjectRevisionConflict


def test_approve_api_returns_structured_storyboard_required_error(monkeypatch, tmp_path: Path):
    def missing_storyboard(*_args, **_kwargs):
        raise ValueError("缺少 storyboard.json，請先明確初始化分鏡後再核准")

    monkeypatch.setattr(ui, "set_review_status", missing_storyboard)

    result = ui._approve_project_api(
        {"library_root": str(tmp_path)},
        tmp_path / "db.sqlite3",
        {"project_id": 7, "notes": "", "base_revision": 3},
    )

    assert result == {
        "ok": False,
        "code": "storyboard_required",
        "error": "尚未建立 storyboard.json，請先到「分鏡審核」執行「建立分鏡」，完成後再核准。",
    }


def test_approve_api_does_not_swallow_revision_conflict(monkeypatch, tmp_path: Path):
    conflict = ProjectRevisionConflict(7, 3, 4)

    def stale_approval(*_args, **_kwargs):
        raise conflict

    monkeypatch.setattr(ui, "set_review_status", stale_approval)

    with pytest.raises(ProjectRevisionConflict) as raised:
        ui._approve_project_api(
            {"library_root": str(tmp_path)},
            tmp_path / "db.sqlite3",
            {"project_id": 7, "notes": "", "base_revision": 3},
        )

    assert raised.value is conflict

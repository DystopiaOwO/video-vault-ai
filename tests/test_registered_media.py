from pathlib import Path

import pytest

from video_vault.database import init_db, project_videos, upsert_video
from video_vault.project import create_project, sync_project_files
from video_vault.ui import _project_detail_for_api, registered_project_media_path


def test_registered_project_media_is_project_scoped(tmp_path: Path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    video_id = upsert_video(db, {"original_path": str(source), "current_path": str(source), "filename": source.name, "category": "travel", "duration_seconds": 1})
    project_id = create_project(db, "project", [video_id], category="travel")
    cfg = {"library_root": str(tmp_path)}
    sync_project_files(cfg, db, project_id)
    media_id = str(project_videos(db, project_id)[0]["project_media_uuid"])
    path = registered_project_media_path(cfg, db, project_id, media_id)
    assert path.read_bytes() == b"media"

    with pytest.raises((FileNotFoundError, ValueError)):
        registered_project_media_path(cfg, db, project_id, "../source.mp4")


def test_registered_project_media_rejects_unregistered_id(tmp_path: Path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    cfg = {"library_root": str(tmp_path)}
    with pytest.raises(FileNotFoundError):
        registered_project_media_path(cfg, db, 1, "video_1")


def test_project_detail_api_uses_scoped_media_url_without_local_paths():
    detail = {
        "project": {"id": 7},
        "folder": r"D:\private\project_7",
        "clips": [{"project_media_id": "media-7", "source_path": r"D:\private\source.mp4"}],
        "segments": [{"project_media_id": "media-7", "source_file": r"D:\private\source.mp4"}],
        "plan": {"groups": [{"segments": [{"source_file": r"D:\private\source.mp4"}]}]},
    }
    result = _project_detail_for_api({}, detail)
    encoded = str(result)
    assert "D:\\private" not in encoded
    assert result["clips"][0]["media_url"].endswith("project_id=7&media_id=media-7")
    assert result["segments"][0]["media_url"].endswith("project_id=7&media_id=media-7")

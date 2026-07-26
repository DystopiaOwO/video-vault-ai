from __future__ import annotations

import json
from pathlib import Path

from video_vault.approval_snapshot import load_approval_snapshot, validate_snapshot
from video_vault.database import add_analysis, init_db, upsert_video
from video_vault.project import can_project_render, create_project, project_dir, set_review_status
from video_vault.project import build_project_plan


def _approved_project(tmp_path: Path) -> tuple[dict, Path, int, Path]:
    db = tmp_path / "db.sqlite3"
    init_db(db)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"approved-source-v1")
    video_id = upsert_video(db, {"original_path": str(source), "current_path": str(source), "filename": source.name, "category": "travel", "duration_seconds": 8})
    add_analysis(db, video_id, "mock", "rules", {"segments": [{"start_seconds": 0, "end_seconds": 4, "segment_type": "key_action", "title": "opening", "reason": "test", "tags": ["travel"], "score": 0.9, "suggested_use": "main"}]}, tmp_path / "raw.json")
    cfg = {"library_root": str(tmp_path)}
    project_id = create_project(db, "snapshot", [video_id], category="travel")
    build_project_plan(cfg, db, project_id)
    set_review_status(cfg, db, project_id, "approved")
    return cfg, db, project_id, source


def test_approval_publishes_immutable_snapshot_and_pointer(tmp_path: Path):
    cfg, _db, project_id, _source = _approved_project(tmp_path)
    folder = project_dir(cfg, project_id)
    review = json.loads((folder / "review_status.json").read_text(encoding="utf-8"))
    assert review["approval_snapshot_id"]
    path = folder / review["approval_snapshot_path"]
    snapshot = load_approval_snapshot(path)
    assert snapshot["snapshot_id"] == review["approval_snapshot_id"]
    assert snapshot["manifest_hash"] == review["approved_manifest_hash"]
    assert validate_snapshot(snapshot)["valid"]


def test_asset_replacement_invalidates_current_approval(tmp_path: Path):
    cfg, db, project_id, _source = _approved_project(tmp_path)
    assert can_project_render(cfg, db, project_id) == (True, "approved")
    review = json.loads((project_dir(cfg, project_id) / "review_status.json").read_text(encoding="utf-8"))
    snapshot = load_approval_snapshot(project_dir(cfg, project_id) / review["approval_snapshot_path"])
    Path(snapshot["assets"][0]["canonical_path"]).write_bytes(b"approved-source-v2-replaced")
    allowed, reason = can_project_render(cfg, db, project_id)
    assert not allowed
    assert "approval snapshot 已失效" in reason


def test_snapshot_hash_detects_manifest_tampering(tmp_path: Path):
    cfg, _db, project_id, _source = _approved_project(tmp_path)
    folder = project_dir(cfg, project_id)
    review = json.loads((folder / "review_status.json").read_text(encoding="utf-8"))
    path = folder / review["approval_snapshot_path"]
    snapshot = load_approval_snapshot(path)
    snapshot["manifest"]["segments"][0]["speed"] = 2.0
    assert not validate_snapshot(snapshot, check_assets=False)["valid"]

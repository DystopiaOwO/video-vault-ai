from __future__ import annotations

import json
from pathlib import Path
import threading

import pytest

from video_vault.approval_snapshot import load_approval_snapshot, validate_snapshot
from video_vault.database import add_analysis, add_bgm_track, add_project_bgm, init_db, project, project_revision, upsert_video
from video_vault.project import can_project_render, create_project, mark_project_needs_review, project_dir, set_review_status
from video_vault.project import build_project_plan
from video_vault.project_lifecycle import ProjectRevisionConflict


def _planned_project(tmp_path: Path) -> tuple[dict, Path, int, Path]:
    db = tmp_path / "db.sqlite3"
    init_db(db)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"approved-source-v1")
    video_id = upsert_video(db, {"original_path": str(source), "current_path": str(source), "filename": source.name, "category": "travel", "duration_seconds": 8})
    add_analysis(db, video_id, "mock", "rules", {"segments": [{"start_seconds": 0, "end_seconds": 4, "segment_type": "key_action", "title": "opening", "reason": "test", "tags": ["travel"], "score": 0.9, "suggested_use": "main"}]}, tmp_path / "raw.json")
    cfg = {"library_root": str(tmp_path)}
    project_id = create_project(db, "snapshot", [video_id], category="travel")
    build_project_plan(cfg, db, project_id)
    return cfg, db, project_id, source


def _approved_project(tmp_path: Path) -> tuple[dict, Path, int, Path]:
    cfg, db, project_id, source = _planned_project(tmp_path)
    from video_vault.storyboard import generate_storyboard

    generate_storyboard(cfg, db, project_id)
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


def test_snapshot_effective_storyboard_matches_manifest_render_state(tmp_path: Path):
    cfg, _db, project_id, _source = _approved_project(tmp_path)
    folder = project_dir(cfg, project_id)
    review = json.loads((folder / "review_status.json").read_text(encoding="utf-8"))
    snapshot = load_approval_snapshot(folder / review["approval_snapshot_path"])
    assert snapshot["effective"]["storyboard"] == snapshot["manifest"]["storyboard_render_state"]


def test_legacy_manifest_bgm_is_exposed_as_effective_selected_bgm(tmp_path: Path):
    cfg, db, project_id, _source = _approved_project(tmp_path)
    bgm = tmp_path / "legacy.mp3"
    bgm.write_bytes(b"legacy-bgm")
    track_id = add_bgm_track(db, {"title": "Legacy BGM", "file_path": str(bgm), "source_url": "https://example.test/legacy", "license_name": "CC0", "attribution_text": "Legacy"})
    add_project_bgm(db, project_id, track_id)
    set_review_status(cfg, db, project_id, "approved")
    folder = project_dir(cfg, project_id)
    review = json.loads((folder / "review_status.json").read_text(encoding="utf-8"))
    snapshot = load_approval_snapshot(folder / review["approval_snapshot_path"])
    assert snapshot["effective"]["selected_bgm"]["track_id"] == track_id


def test_approval_failure_restores_files_db_revision_and_snapshot_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg, db, project_id, _source = _approved_project(tmp_path)
    folder = project_dir(cfg, project_id)
    tracked = [
        folder / "render_manifest.json",
        folder / "review_status.json",
        folder / "project_plan.json",
        folder / "decisions" / "decision_log.jsonl",
        folder / "checkpoints" / "review_approved.json",
    ]
    before = {path: path.read_bytes() for path in tracked}
    before_mtime = {path: path.stat().st_mtime_ns for path in tracked}
    approvals_before = {path: path.read_bytes() for path in (folder / "approvals").glob("*.json")}
    before_revision = project_revision(db, project_id)
    before_status = str(project(db, project_id)["status"])

    def fail_status(*_args, **_kwargs):
        raise RuntimeError("status publish failed")

    monkeypatch.setattr("video_vault.project.set_project_status", fail_status)
    with pytest.raises(RuntimeError, match="status publish failed"):
        set_review_status(cfg, db, project_id, "approved")

    assert {path: path.read_bytes() for path in tracked} == before
    assert {path: path.stat().st_mtime_ns for path in tracked} == before_mtime
    assert {path: path.read_bytes() for path in (folder / "approvals").glob("*.json")} == approvals_before
    assert project_revision(db, project_id) == before_revision
    assert str(project(db, project_id)["status"]) == before_status
    assert not list(folder.glob(".approval-stage-*"))


def test_stale_approval_cannot_publish_after_concurrent_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg, db, project_id, _source = _approved_project(tmp_path)
    mark_project_needs_review(cfg, db, project_id)
    base_revision = project_revision(db, project_id)
    folder = project_dir(cfg, project_id)
    entered = threading.Event()
    release = threading.Event()
    approval_errors: list[BaseException] = []
    real_build = __import__("video_vault.render_manifest", fromlist=["build_render_manifest"]).build_render_manifest

    def blocked_build(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=10), "approval preparation did not release"
        return real_build(*args, **kwargs)

    monkeypatch.setattr("video_vault.render_manifest.build_render_manifest", blocked_build)

    def approve():
        try:
            set_review_status(cfg, db, project_id, "approved", base_revision=base_revision)
        except BaseException as exc:  # assert the exact lifecycle failure below
            approval_errors.append(exc)

    worker = threading.Thread(target=approve)
    worker.start()
    assert entered.wait(timeout=10), "approval did not reach preparation barrier"

    set_review_status(cfg, db, project_id, "rejected", "concurrent revision", base_revision=base_revision)
    after_writer = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (
            folder / "render_manifest.json",
            folder / "review_status.json",
            folder / "project_plan.json",
            folder / "decisions" / "decision_log.jsonl",
        )
        if path.is_file()
    }
    writer_revision = project_revision(db, project_id)
    writer_status = str(project(db, project_id)["status"])
    release.set()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert len(approval_errors) == 1
    assert isinstance(approval_errors[0], ProjectRevisionConflict)
    assert project_revision(db, project_id) == writer_revision
    assert str(project(db, project_id)["status"]) == writer_status
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in after_writer
    } == after_writer
    assert not list(folder.glob(".approval-stage-*"))


def test_approval_missing_storyboard_fails_without_live_state_changes(tmp_path: Path):
    cfg, db, project_id, _source = _planned_project(tmp_path)
    folder = project_dir(cfg, project_id)
    thumbnail = folder / "cache" / "storyboard" / "clip_001.jpg"
    thumbnail.parent.mkdir(parents=True, exist_ok=True)
    thumbnail.write_bytes(b"thumbnail-before-approval")
    before_files = {
        path.relative_to(folder): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in folder.rglob("*")
        if path.is_file()
    }
    before_revision = project_revision(db, project_id)
    before_row = dict(project(db, project_id))

    with pytest.raises(ValueError, match="缺少 storyboard.json"):
        set_review_status(cfg, db, project_id, "approved", base_revision=before_revision)

    after_files = {
        path.relative_to(folder): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in folder.rglob("*")
        if path.is_file()
    }
    assert after_files == before_files
    assert not (folder / "storyboard.json").exists()
    assert project_revision(db, project_id) == before_revision
    assert dict(project(db, project_id)) == before_row


def test_explicit_storyboard_initialization_then_approval_advances_one_revision(tmp_path: Path):
    cfg, db, project_id, _source = _planned_project(tmp_path)
    from video_vault.storyboard import generate_storyboard

    generate_storyboard(cfg, db, project_id)
    base_revision = project_revision(db, project_id)
    set_review_status(cfg, db, project_id, "approved", base_revision=base_revision)

    assert project_revision(db, project_id) == base_revision + 1
    review = json.loads((project_dir(cfg, project_id) / "review_status.json").read_text(encoding="utf-8"))
    assert review["approved_project_revision"] == base_revision + 1

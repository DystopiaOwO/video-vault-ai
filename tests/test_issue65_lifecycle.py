from pathlib import Path

import pytest

import video_vault.project as project_module
from video_vault.database import init_db, project_revision
from video_vault.job_coordinator import JobCoordinator, JobState
from video_vault.project import build_project_plan, create_project, project_dir, revise_project
from video_vault.project_lifecycle import ProjectRevisionConflict


def test_running_cancel_has_one_terminal_cancelled_state_and_releases_slot():
    coordinator = JobCoordinator(ffmpeg_slots=1)
    first = coordinator.submit(1, "render", {"ffmpeg_heavy"}, 1, resources={"ffmpeg_heavy"})
    second = coordinator.submit(1, "render", {"ffmpeg_heavy"}, 1, resources={"ffmpeg_heavy"})
    assert first.state is JobState.RUNNING
    assert second.state is JobState.QUEUED

    assert coordinator.cancel(first.job_id).state is JobState.CANCELLING
    assert coordinator.complete(first.job_id).state is JobState.CANCELLING
    assert coordinator.finish_cancel(first.job_id).state is JobState.CANCELLED
    assert coordinator.finish_cancel(first.job_id).state is JobState.CANCELLED
    coordinator.start(second.job_id)
    assert second.state is JobState.RUNNING


def test_stale_revise_does_not_write_revision_notes_or_plan(tmp_path: Path):
    db = tmp_path / "db.sqlite3"
    cfg = {"library_root": str(tmp_path)}
    init_db(db)
    project_id = create_project(db, "stale", [])
    folder = project_dir(cfg, project_id)
    folder.mkdir(parents=True, exist_ok=True)
    plan = folder / "project_plan.json"
    plan.write_text('{"status":"approved","keep":true}\n', encoding="utf-8")
    review = folder / "review_status.json"
    review.write_text('{"status":"approved","approved_by_user":true}\n', encoding="utf-8")
    notes = folder / "feedback" / "revision_notes.md"
    notes.parent.mkdir(parents=True, exist_ok=True)
    notes.write_text("old", encoding="utf-8")
    before_plan = plan.read_bytes()
    before_review = review.read_bytes()
    before_notes = notes.read_bytes()

    with pytest.raises(ProjectRevisionConflict):
        revise_project(cfg, db, project_id, "new", base_revision=project_revision(db, project_id) - 1)

    assert plan.read_bytes() == before_plan
    assert review.read_bytes() == before_review
    assert notes.read_bytes() == before_notes
    assert project_revision(db, project_id) == 1


def test_revise_failure_rolls_back_notes_and_plan(tmp_path: Path, monkeypatch):
    db = tmp_path / "db.sqlite3"
    cfg = {"library_root": str(tmp_path)}
    init_db(db)
    project_id = create_project(db, "failed revise", [])
    folder = project_dir(cfg, project_id)
    folder.mkdir(parents=True, exist_ok=True)
    plan = folder / "project_plan.json"
    plan.write_text('{"status":"approved","keep":true}\n', encoding="utf-8")
    notes = folder / "feedback" / "revision_notes.md"
    notes.parent.mkdir(parents=True, exist_ok=True)
    notes.write_text("old", encoding="utf-8")
    monkeypatch.setattr(project_module, "_build_project_plan", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("plan failed")))

    with pytest.raises(RuntimeError, match="plan failed"):
        revise_project(cfg, db, project_id, "new", base_revision=1)

    assert plan.read_text(encoding="utf-8") == '{"status":"approved","keep":true}\n'
    assert notes.read_text(encoding="utf-8") == "old"
    assert [p for p in (folder / "feedback").glob("revision_*.md") if p.name != "revision_notes.md"] == []
    assert project_revision(db, project_id) == 1


def test_noop_project_commit_still_has_no_revision_change(tmp_path: Path):
    db = tmp_path / "db.sqlite3"
    cfg = {"library_root": str(tmp_path)}
    init_db(db)
    project_id = create_project(db, "noop", [])
    folder = project_dir(cfg, project_id)
    folder.mkdir(parents=True, exist_ok=True)
    plan = {"status": "draft", "groups": []}
    (folder / "project_plan.json").write_text('{"status":"draft","groups":[]}\n', encoding="utf-8")
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(project_module, "_build_project_plan", lambda *args, **kwargs: dict(plan))
        build_project_plan(cfg, db, project_id, base_revision=1)
        assert project_revision(db, project_id) == 1

        monkeypatch.setattr(project_module, "_build_project_plan", lambda *args, **kwargs: {**plan, "groups": [{"label": "new"}]})
        build_project_plan(cfg, db, project_id, base_revision=1)
        assert project_revision(db, project_id) == 2
    finally:
        monkeypatch.undo()

from pathlib import Path

import pytest

from video_vault.database import init_db, project, project_revision
from video_vault.job_coordinator import JobCoordinator, JobState
from video_vault.project import create_project
from video_vault.project_lifecycle import ProjectRevisionConflict, project_commit


def test_project_revision_migration_and_commit_are_monotonic(tmp_path: Path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    project_id = create_project(db, "revision", [])
    assert project_revision(db, project_id) == 1

    with project_commit(db, project_id, base_revision=1) as commit:
        commit.changed = True
    assert project_revision(db, project_id) == 2

    with pytest.raises(ProjectRevisionConflict):
        with project_commit(db, project_id, base_revision=1):
            pytest.fail("stale writer entered commit")
    assert project_revision(db, project_id) == 2


def test_noop_commit_does_not_increment_revision(tmp_path: Path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    project_id = create_project(db, "no-op", [])
    with project_commit(db, project_id, base_revision=1) as commit:
        commit.changed = False
    assert project_revision(db, project_id) == 1
    assert int(project(db, project_id)["project_revision"]) == 1


def test_coordinator_queues_incompatible_jobs_and_releases_slot():
    coordinator = JobCoordinator(ffmpeg_slots=1, gpu_slots=1)
    first = coordinator.submit(1, "render", {"ffmpeg_heavy", "gpu_heavy"}, 1, resources={"ffmpeg_heavy", "gpu_heavy"})
    second = coordinator.submit(1, "color", {"project_state_writer", "gpu_heavy"}, 1, resources={"gpu_heavy"})
    assert first.state == JobState.RUNNING
    assert second.state == JobState.QUEUED
    assert second.queue_reason
    coordinator.complete(first.job_id)
    coordinator.start(second.job_id)
    assert second.state == JobState.RUNNING


def test_coordinator_publish_guard_rejects_revision_change_and_cancel():
    coordinator = JobCoordinator()
    job = coordinator.submit(4, "perception", {"source_analysis_writer"}, 8, resources={"ai_provider"})
    assert coordinator.publish_allowed(job.job_id, job.generation, 8) == (True, "ok")
    allowed, reason = coordinator.publish_allowed(job.job_id, job.generation, 9)
    assert not allowed
    assert "revision" in reason
    assert job.state == JobState.SUPERSEDED

    cancelled = coordinator.submit(4, "preview", {"read_only_snapshot"}, 9)
    coordinator.cancel(cancelled.job_id)
    allowed, reason = coordinator.publish_allowed(cancelled.job_id, cancelled.generation, 9)
    assert not allowed
    assert "取消" in reason

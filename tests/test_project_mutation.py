from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from video_vault.database import init_db
from video_vault.project import create_project, project_detail
from video_vault.project_mutation import ProjectConflict, project_mutation, project_revision


def _project(tmp_path: Path) -> tuple[Path, int, dict]:
    db = tmp_path / "db.sqlite3"
    init_db(db)
    project_id = create_project(db, "A", [])
    return db, project_id, {"library_root": str(tmp_path)}


def test_effective_mutation_advances_revision_exactly_once(tmp_path):
    db, project_id, _cfg = _project(tmp_path)
    assert project_revision(db, project_id) == 0

    mutation = project_mutation(db, project_id, 0, reason="test")
    with mutation:
        mutation.mark_changed()
        mutation.mark_changed()

    assert mutation.current_revision == 0
    assert mutation.committed_revision == 1
    assert project_revision(db, project_id) == 1


def test_noop_and_failed_mutations_do_not_advance_revision(tmp_path):
    db, project_id, _cfg = _project(tmp_path)

    no_op = project_mutation(db, project_id, 0, reason="noop")
    with no_op:
        pass
    assert no_op.committed_revision == 0
    assert project_revision(db, project_id) == 0

    with pytest.raises(RuntimeError, match="write failed"):
        with project_mutation(db, project_id, 0, reason="failure") as mutation:
            mutation.mark_changed()
            raise RuntimeError("write failed")
    assert project_revision(db, project_id) == 0


def test_missing_and_stale_revisions_are_explicit_conflicts(tmp_path):
    db, project_id, _cfg = _project(tmp_path)

    with pytest.raises(ProjectConflict) as missing:
        with project_mutation(db, project_id, None, reason="missing"):
            pass
    assert missing.value.code == "missing_project_revision"
    assert missing.value.current_revision == 0

    with project_mutation(db, project_id, 0, reason="first") as first:
        first.mark_changed()

    with pytest.raises(ProjectConflict) as stale:
        with project_mutation(db, project_id, 0, reason="stale"):
            pass
    assert stale.value.code == "project_revision_conflict"
    assert stale.value.expected_revision == 0
    assert stale.value.current_revision == 1
    assert project_revision(db, project_id) == 1


def test_same_revision_concurrent_writes_serialize_and_one_conflicts(tmp_path):
    db, project_id, _cfg = _project(tmp_path)
    barrier = Barrier(2)

    def write(name: str) -> str:
        barrier.wait()
        try:
            with project_mutation(db, project_id, 0, reason=name) as mutation:
                mutation.mark_changed()
            return "committed"
        except ProjectConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(write, ("a", "b")))

    assert results == ["committed", "conflict"]
    assert project_revision(db, project_id) == 1


def test_read_only_project_detail_does_not_advance_revision(tmp_path):
    db, project_id, cfg = _project(tmp_path)

    before = project_revision(db, project_id)
    detail = project_detail(cfg, db, project_id)
    after = project_revision(db, project_id)

    assert detail["project"]["project_revision"] == before
    assert after == before

"""Project-scoped commit, revision and cancellation contracts.

The application still has a few legacy writers, so this module deliberately
keeps the public API small and backwards compatible.  New writers should use
``project_commit`` around their staging/publish boundary and pass the client
base revision when one is available.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Iterator

from .database import connect, init_db, project_revision


class ProjectRevisionConflict(RuntimeError):
    """The caller attempted to write against an older project revision."""

    code = "stale_project_revision"

    def __init__(self, project_id: int, expected: int | None, current: int):
        self.project_id = int(project_id)
        self.expected = expected
        self.current = int(current)
        expected_text = "缺少" if expected is None else str(expected)
        super().__init__(f"專案版本已更新：目前 revision {current}，請重新載入後再儲存（收到 {expected_text}）")


class ProjectCommitError(RuntimeError):
    pass


_LOCKS: dict[int, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_ACTIVE = threading.local()


def project_lock(project_id: int) -> threading.RLock:
    project_id = int(project_id)
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(project_id, threading.RLock())


def current_revision(db: Path, project_id: int) -> int:
    return project_revision(db, int(project_id))


def revision_payload(db: Path, project_id: int) -> dict[str, int]:
    return {"project_revision": current_revision(db, project_id)}


def check_base_revision(db: Path, project_id: int, base_revision: int | None) -> int:
    current = current_revision(db, project_id)
    if base_revision is not None and int(base_revision) != current:
        raise ProjectRevisionConflict(project_id, int(base_revision), current)
    return current


@dataclass
class ProjectCommit:
    project_id: int
    base_revision: int
    changed: bool = False
    new_revision: int | None = None

    def record_changed(self, value: bool = True) -> bool:
        """Accumulate effective changes across nested project writers.

        A nested writer can be a no-op even when its caller has already
        staged a real mutation.  ``changed`` therefore has OR semantics for
        the entire commit boundary and must never be reset by a nested call.
        """
        self.changed = self.changed or bool(value)
        return self.changed


def _active_commit(project_id: int) -> ProjectCommit | None:
    active = getattr(_ACTIVE, "commit", None)
    return active if active and active.project_id == int(project_id) else None


@contextmanager
def project_commit(db: Path, project_id: int, base_revision: int | None = None) -> Iterator[ProjectCommit]:
    """Serialize one project mutation and advance revision exactly once.

    Long-running work must happen outside this context.  Keep only validation,
    staged publication and the final revision update inside it.
    """
    existing = _active_commit(project_id)
    if existing is not None:
        if base_revision is not None and int(base_revision) != existing.base_revision:
            raise ProjectRevisionConflict(project_id, int(base_revision), existing.base_revision)
        yield existing
        return

    lock = project_lock(project_id)
    with lock:
        init_db(db)
        current = check_base_revision(db, project_id, base_revision)
        commit = ProjectCommit(int(project_id), current)
        _ACTIVE.commit = commit
        try:
            yield commit
            if commit.changed:
                with connect(db) as con:
                    updated = con.execute(
                        "update projects set project_revision=project_revision+1, updated_at=current_timestamp "
                        "where id=? and project_revision=?",
                        (commit.project_id, current),
                    )
                    if updated.rowcount != 1:
                        raise ProjectRevisionConflict(project_id, current, current + 1)
                    commit.new_revision = current + 1
            else:
                commit.new_revision = current
        finally:
            if getattr(_ACTIVE, "commit", None) is commit:
                _ACTIVE.commit = None


def mark_changed(project_id: int, changed: bool = True) -> None:
    active = _active_commit(project_id)
    if active is not None:
        active.record_changed(changed)


class CancellationToken:
    """Small cooperative cancellation token usable by providers and workers."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise CancellationRequested("工作已取消")


class CancellationRequested(RuntimeError):
    pass


__all__ = [
    "CancellationRequested",
    "CancellationToken",
    "ProjectCommit",
    "ProjectCommitError",
    "ProjectRevisionConflict",
    "check_base_revision",
    "current_revision",
    "mark_changed",
    "project_commit",
    "project_lock",
    "revision_payload",
]

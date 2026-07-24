from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Lock, RLock
from typing import Any

from .database import connect, init_db

_LOCKS_GUARD = Lock()
_PROJECT_LOCKS: dict[int, RLock] = {}


def _lock_for(project_id: int) -> RLock:
    project_id = int(project_id)
    with _LOCKS_GUARD:
        lock = _PROJECT_LOCKS.get(project_id)
        if lock is None:
            lock = RLock()
            _PROJECT_LOCKS[project_id] = lock
        return lock


@dataclass(slots=True)
class ProjectConflict(RuntimeError):
    project_id: int
    expected_revision: int | None
    current_revision: int
    code: str = "project_revision_conflict"
    message: str = "專案已被其他操作更新，請重新整理後再試。"

    def __str__(self) -> str:
        return self.message

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "error": self.message,
            "project_id": self.project_id,
            "expected_revision": self.expected_revision,
            "current_revision": self.current_revision,
        }


def project_revision(db: Path, project_id: int) -> int:
    init_db(db)
    with connect(db) as con:
        row = con.execute(
            "select project_revision from projects where id=?",
            (int(project_id),),
        ).fetchone()
    if row is None:
        raise ValueError(f"project not found: {project_id}")
    return int(row["project_revision"] or 0)


def parse_base_revision(value: Any) -> int:
    if value is None or value == "":
        raise ValueError("missing project revision")
    try:
        revision = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid project revision") from exc
    if revision < 0:
        raise ValueError("invalid project revision")
    return revision


class ProjectMutation:
    """Serialize one project's commit boundary and advance its revision once.

    Long-running work should happen before entering this context. The lock is
    intended for short validation plus publish sequences. Call ``mark_changed``
    only after an effective state change has been committed.
    """

    def __init__(
        self,
        db: Path,
        project_id: int,
        expected_revision: int | None,
        *,
        require_revision: bool = True,
        reason: str = "",
    ) -> None:
        self.db = db
        self.project_id = int(project_id)
        self.expected_revision = expected_revision
        self.require_revision = bool(require_revision)
        self.reason = str(reason)
        self.current_revision = 0
        self.committed_revision = 0
        self.changed = False
        self._lock = _lock_for(self.project_id)
        self._entered = False

    def __enter__(self) -> "ProjectMutation":
        self._lock.acquire()
        self._entered = True
        try:
            self.current_revision = project_revision(self.db, self.project_id)
            self.committed_revision = self.current_revision
            if self.require_revision and self.expected_revision is None:
                raise ProjectConflict(
                    self.project_id,
                    None,
                    self.current_revision,
                    code="missing_project_revision",
                    message="缺少專案版本，請重新整理後再試。",
                )
            if self.expected_revision is not None and int(self.expected_revision) != self.current_revision:
                raise ProjectConflict(
                    self.project_id,
                    int(self.expected_revision),
                    self.current_revision,
                )
            return self
        except Exception:
            self._entered = False
            self._lock.release()
            raise

    def mark_changed(self, changed: bool = True) -> None:
        self.changed = self.changed or bool(changed)

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            if exc_type is None and self.changed:
                with connect(self.db) as con:
                    cursor = con.execute(
                        """update projects
                        set project_revision=project_revision+1,
                            updated_at=current_timestamp
                        where id=? and project_revision=?""",
                        (self.project_id, self.current_revision),
                    )
                    if cursor.rowcount != 1:
                        row = con.execute(
                            "select project_revision from projects where id=?",
                            (self.project_id,),
                        ).fetchone()
                        current = int(row["project_revision"] or 0) if row else self.current_revision
                        raise ProjectConflict(
                            self.project_id,
                            self.current_revision,
                            current,
                            message="專案版本在提交時發生變更，請重新整理後再試。",
                        )
                self.committed_revision = self.current_revision + 1
            else:
                self.committed_revision = self.current_revision
        finally:
            if self._entered:
                self._entered = False
                self._lock.release()
        return False


def project_mutation(
    db: Path,
    project_id: int,
    expected_revision: int | None,
    *,
    require_revision: bool = True,
    reason: str = "",
) -> ProjectMutation:
    return ProjectMutation(
        db,
        project_id,
        expected_revision,
        require_revision=require_revision,
        reason=reason,
    )

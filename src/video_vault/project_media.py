"""Project-owned media identity and reversible legacy migration helpers."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from .database import connect, init_db, project_videos
from .source_fingerprint import parse_source_fingerprint, persisted_fingerprint_for_stat

PROJECT_MEDIA_SCHEMA_VERSION = 2


def ensure_project_media_ownership(cfg: dict, db: Path, project_id: int) -> dict[str, Any]:
    """Backfill project-local source fingerprints without touching source media.

    ``project_media_uuid`` is the stable local identity. The migration records a
    backup of the relation metadata before filling missing fingerprints, making
    the operation repeatable and reversible for legacy projects.
    """
    init_db(db)
    root = Path(cfg["library_root"]) / "08_projects" / f"project_{int(project_id)}"
    validation = root / "validation"
    validation.mkdir(parents=True, exist_ok=True)
    marker = validation / "project_media_migration.json"
    rows = [dict(row) for row in project_videos(db, int(project_id))]
    before = {
        str(row["project_media_uuid"]): {
            "source_fingerprint_json": row.get("source_fingerprint_json") or "{}",
            "ownership_state": row.get("ownership_state") or "project_owned",
            "migration_generation": int(row.get("migration_generation") or 0),
        }
        for row in rows
        if row.get("project_media_uuid")
    }
    changed = False
    with connect(db) as con:
        for row in rows:
            media_id = str(row.get("project_media_uuid") or "")
            if not media_id:
                continue
            current = str(row.get("source_fingerprint_json") or "{}").strip()
            source = Path(str(row.get("current_path") or "")).expanduser()
            if (
                str(row.get("ownership_state") or "") == "project_owned"
                and source.is_file()
                and persisted_fingerprint_for_stat(source, parse_source_fingerprint(current)) is not None
            ):
                continue
            fingerprint = _fingerprint(source) if source.is_file() else {
                "path": str(source), "size": 0, "mtime_ns": 0, "sha256": "", "missing": True,
            }
            con.execute(
                "update project_videos set source_fingerprint_json=?, ownership_state='project_owned', migration_generation=coalesce(migration_generation, 0)+1 where project_id=? and project_media_uuid=?",
                (json.dumps(fingerprint, ensure_ascii=False, sort_keys=True), int(project_id), media_id),
            )
            changed = True
    after_rows = [dict(row) for row in project_videos(db, int(project_id))]
    after = {
        str(row["project_media_uuid"]): {
            "source_fingerprint_json": row.get("source_fingerprint_json") or "{}",
            "ownership_state": row.get("ownership_state") or "project_owned",
            "migration_generation": int(row.get("migration_generation") or 0),
        }
        for row in after_rows
        if row.get("project_media_uuid")
    }
    if changed or not marker.exists():
        _atomic_write(marker, {
            "schema_version": PROJECT_MEDIA_SCHEMA_VERSION,
            "project_id": int(project_id),
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "backup": before,
            "current": after,
            "changed": changed,
            "rollback_available": bool(before),
        })
    return {"schema_version": PROJECT_MEDIA_SCHEMA_VERSION, "project_id": int(project_id), "changed": changed, "media": after}


def rollback_project_media_ownership(cfg: dict, db: Path, project_id: int) -> bool:
    """Restore only the metadata captured by the migration marker."""
    marker = Path(cfg["library_root"]) / "08_projects" / f"project_{int(project_id)}" / "validation" / "project_media_migration.json"
    if not marker.is_file():
        return False
    payload = json.loads(marker.read_text(encoding="utf-8"))
    backup = payload.get("backup") or {}
    with connect(db) as con:
        for media_id, row in backup.items():
            con.execute(
                "update project_videos set source_fingerprint_json=?, ownership_state=?, migration_generation=? where project_id=? and project_media_uuid=?",
                (row.get("source_fingerprint_json", "{}"), row.get("ownership_state", "project_owned"), int(row.get("migration_generation", 0)), int(project_id), str(media_id)),
            )
    return True


def _fingerprint(path: Path) -> dict[str, Any]:
    path = path.resolve()
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"path": str(path), "size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns), "sha256": digest.hexdigest()}


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


__all__ = ["PROJECT_MEDIA_SCHEMA_VERSION", "ensure_project_media_ownership", "rollback_project_media_ownership"]

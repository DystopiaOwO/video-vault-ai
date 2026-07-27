"""Reference-aware artifact inventory and conservative cleanup plans."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping
import uuid

from .project import project_dir


INVENTORY_SCHEMA_VERSION = 1
RETENTION_POLICY_VERSION = "retention-v1"
DEFAULT_POLICY: dict[str, Any] = {
    "cache_max_age_days": 30,
    "preview_max_age_days": 14,
    "failed_grace_days": 7,
    "logs_max_age_days": 30,
    "minimum_free_disk_bytes": 0,
    "pinned_exemption": True,
}
PROTECTED_TYPES = {"source_media", "approval_snapshot", "formal_output", "manifest", "runtime_asset"}


class RetentionError(ValueError):
    def __init__(self, code: str, message: str, *, action: str = "", details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.category = "retention"
        self.retryable = code in {"stale_cleanup_plan", "file_locked"}
        self.action = action
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        return {"ok": False, "code": self.code, "error": self.message, "category": self.category, "retryable": self.retryable, "details": self.details, "action": self.action}


def inventory_path(cfg: Mapping[str, Any], project_id: int) -> Path:
    return project_dir(dict(cfg), int(project_id)) / "storage" / "artifact_index.json"


def load_inventory(cfg: Mapping[str, Any], project_id: int) -> dict[str, Any]:
    path = inventory_path(cfg, project_id)
    if not path.exists():
        return {"schema_version": INVENTORY_SCHEMA_VERSION, "project_id": int(project_id), "artifacts": [], "updated_at": None}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RetentionError("inventory_corrupt", f"artifact inventory 無法讀取：{exc}", action="請先執行 inventory reconcile") from exc
    if not isinstance(value, dict) or int(value.get("schema_version") or 0) != INVENTORY_SCHEMA_VERSION:
        raise RetentionError("inventory_schema", "不支援的 artifact inventory schema", action="更新本機資料庫後再試")
    value.setdefault("artifacts", [])
    return value


def save_inventory(cfg: Mapping[str, Any], project_id: int, inventory: Mapping[str, Any]) -> Path:
    path = inventory_path(cfg, project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = dict(inventory)
    payload["schema_version"] = INVENTORY_SCHEMA_VERSION
    payload["project_id"] = int(project_id)
    payload["updated_at"] = _now()
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
    return path


def register_artifact(
    cfg: Mapping[str, Any],
    project_id: int,
    path: str | Path,
    artifact_type: str,
    *,
    state: str = "complete",
    references: list[str] | None = None,
    pinned: bool = False,
    generation: str = "",
    revision: int | None = None,
    producer_job_id: str = "",
    producer_job_status: str = "",
) -> dict[str, Any]:
    target = Path(path).expanduser().resolve()
    if not target.is_file() or target.is_symlink():
        raise RetentionError("artifact_missing", f"artifact 不是可登錄的 regular file：{target}", action="先確認輸出檔案存在")
    stat = target.stat()
    inventory = load_inventory(cfg, project_id)
    artifact_id = _artifact_id(target, stat.st_size, _sha256(target))
    record = {
        "artifact_id": artifact_id,
        "project_id": int(project_id),
        "type": str(artifact_type),
        "path": str(target),
        "size": int(stat.st_size),
        "created_at": _now(),
        "updated_at": _now(),
        "last_accessed_at": _now(),
        "sha256": _sha256(target),
        "references": sorted(set(str(item) for item in references or [])),
        "pinned": bool(pinned),
        "lifecycle_state": str(state),
        "generation": str(generation),
        "revision": revision,
        "producer_job_id": str(producer_job_id),
        "producer_job_status": str(producer_job_status),
        "safe_to_delete_reason": "",
        "deletion_status": "active",
    }
    records = [item for item in inventory.get("artifacts", []) if str(item.get("artifact_id")) != artifact_id and str(item.get("path")) != str(target)]
    records.append(record)
    inventory["artifacts"] = records
    save_inventory(cfg, project_id, inventory)
    return record


def reconcile_inventory(cfg: Mapping[str, Any], project_id: int) -> dict[str, Any]:
    """Register known project artifacts without deleting anything."""

    folder = project_dir(dict(cfg), int(project_id)).resolve()
    inventory = load_inventory(cfg, project_id)
    existing = {str(item.get("path")): item for item in inventory.get("artifacts", [])}
    roots = {
        "source_media": folder / "source",
        "proxy": folder / "proxy",
        "preview": folder / "output",
        "frame": folder / "frames",
        "perception_history": folder / "perception_runs",
        "segment_cache": folder / "cache" / "segments",
        "render_cache": folder / "cache",
        "draft_output": folder / "output",
        "formal_output": folder / "output",
        "handoff_package": folder / "output",
        "approval_snapshot": folder / "approvals",
        "manifest": folder / "render_manifest.json",
        "log": folder / "logs",
    }
    discovered = 0
    for artifact_type, root in roots.items():
        if root.is_file():
            candidates = [root]
        elif root.is_dir():
            candidates = [item for item in root.rglob("*") if item.is_file() and not item.is_symlink()]
        else:
            continue
        for path in candidates:
            resolved = path.resolve()
            if str(resolved) in existing:
                continue
            stat = resolved.stat()
            record = {
                "artifact_id": _artifact_id(resolved, stat.st_size, _sha256(resolved)),
                "project_id": int(project_id),
                "type": _specific_type(artifact_type, resolved, folder),
                "path": str(resolved),
                "size": int(stat.st_size),
                "created_at": datetime.fromtimestamp(stat.st_ctime, timezone.utc).isoformat(timespec="seconds"),
                "updated_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
                "last_accessed_at": datetime.fromtimestamp(stat.st_atime, timezone.utc).isoformat(timespec="seconds"),
                "sha256": _sha256(resolved),
                "references": [],
                "pinned": False,
                "lifecycle_state": "complete",
                "generation": "",
                "revision": None,
                "producer_job_id": "",
                "producer_job_status": "",
                "safe_to_delete_reason": "",
                "deletion_status": "active",
            }
            inventory.setdefault("artifacts", []).append(record)
            existing[str(resolved)] = record
            discovered += 1
    save_inventory(cfg, project_id, inventory)
    return {"ok": True, "project_id": int(project_id), "discovered": discovered, "artifacts": inventory["artifacts"]}


def set_artifact_pinned(cfg: Mapping[str, Any], project_id: int, artifact_id: str, pinned: bool) -> dict[str, Any]:
    inventory = load_inventory(cfg, project_id)
    for record in inventory.get("artifacts", []):
        if str(record.get("artifact_id")) == str(artifact_id):
            record["pinned"] = bool(pinned)
            save_inventory(cfg, project_id, inventory)
            return record
    raise RetentionError("artifact_missing", "找不到指定 artifact", action="重新整理儲存空間清單")


def build_cleanup_plan(cfg: Mapping[str, Any], project_id: int, policy: Mapping[str, Any] | None = None) -> dict[str, Any]:
    reconcile_inventory(cfg, project_id)
    inventory = load_inventory(cfg, project_id)
    rules = {**DEFAULT_POLICY, **dict(policy or {})}
    now = datetime.now(timezone.utc)
    candidates: list[dict[str, Any]] = []
    protected: list[dict[str, Any]] = []
    for record in inventory.get("artifacts", []):
        reason = _protection_reason(record)
        if reason:
            protected.append({"artifact_id": record.get("artifact_id"), "reason": reason, "type": record.get("type")})
            continue
        if _eligible(record, now, rules):
            candidate = {"artifact_id": record.get("artifact_id"), "project_id": int(project_id), "path": record.get("path"), "type": record.get("type"), "size": int(record.get("size") or 0), "reason": _deletion_reason(record, rules)}
            candidates.append(candidate)
    plan = {
        "plan_id": "cleanup-" + uuid.uuid4().hex,
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "policy_version": RETENTION_POLICY_VERSION,
        "project_id": int(project_id),
        "created_at": _now(),
        "expires_at": (now + timedelta(minutes=15)).isoformat(timespec="seconds"),
        "graph_hash": _graph_hash(inventory),
        "policy": rules,
        "candidates": candidates,
        "protected": protected,
        "candidate_count": len(candidates),
        "candidate_size": sum(int(item["size"]) for item in candidates),
        "estimated_free_space": sum(int(item["size"]) for item in candidates),
    }
    plan_path = project_dir(dict(cfg), int(project_id)) / "storage" / f"{plan['plan_id']}.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return plan


def execute_cleanup_plan(cfg: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]:
    project_id = int(plan.get("project_id") or 0)
    inventory = load_inventory(cfg, project_id)
    if plan.get("graph_hash") != _graph_hash(inventory):
        raise RetentionError("stale_cleanup_plan", "cleanup plan 已過期，引用圖已改變", action="重新建立 dry-run cleanup plan")
    if str(plan.get("expires_at") or "") < _now():
        raise RetentionError("stale_cleanup_plan", "cleanup plan 已過期", action="重新建立 dry-run cleanup plan")
    by_id = {str(item.get("artifact_id")): item for item in inventory.get("artifacts", [])}
    root = project_dir(dict(cfg), project_id).resolve()
    results = []
    changed = False
    for candidate in plan.get("candidates", []):
        artifact_id = str(candidate.get("artifact_id") or "")
        record = by_id.get(artifact_id)
        path = Path(str(candidate.get("path") or "")).expanduser()
        result = {"artifact_id": artifact_id, "path": str(path), "status": "deleted"}
        try:
            if record is None or record.get("path") != str(path):
                raise RetentionError("artifact_changed", "artifact identity 已改變", action="重新建立 cleanup plan")
            if _protection_reason(record):
                raise RetentionError("protected_artifact", "artifact 在清理前已受到保護", action="保留 artifact")
            resolved = path.resolve(strict=False)
            if root not in resolved.parents or resolved == root or path.is_symlink():
                raise RetentionError("path_boundary", "artifact path 不在核准 project root 內", action="保留 artifact")
            if not path.exists():
                record["deletion_status"] = "missing"
                result["status"] = "already_missing"
            elif _sha256(path) != record.get("sha256"):
                raise RetentionError("artifact_changed", "artifact hash 已改變", action="重新建立 cleanup plan")
            else:
                path.unlink()
                record["deletion_status"] = "deleted"
                record["lifecycle_state"] = "deleted"
                changed = True
        except RetentionError as exc:
            result.update({"status": "blocked", "code": exc.code, "reason": exc.message})
        except PermissionError as exc:
            result.update({"status": "blocked", "code": "file_locked", "reason": str(exc)})
        except OSError as exc:
            result.update({"status": "blocked", "code": "delete_failed", "reason": str(exc)})
        results.append(result)
    if changed:
        save_inventory(cfg, project_id, inventory)
    return {"ok": True, "plan_id": plan.get("plan_id"), "results": results, "reclaimed_bytes": sum(int(item.get("size") or 0) for item, result in zip(plan.get("candidates", []), results) if result.get("status") in {"deleted", "already_missing"})}


def _protection_reason(record: Mapping[str, Any]) -> str:
    if str(record.get("type")) in PROTECTED_TYPES:
        return "immutable or approval artifact"
    if bool(record.get("pinned")):
        return "user pinned"
    if str(record.get("producer_job_status")) in {"queued", "running", "cancelling"}:
        return "producer job is active"
    if str(record.get("lifecycle_state")) in {"current", "approved", "formal", "active"} and record.get("references"):
        return "referenced by current/approved state"
    if record.get("references"):
        return "referenced artifact"
    return ""


def _eligible(record: Mapping[str, Any], now: datetime, policy: Mapping[str, Any]) -> bool:
    if str(record.get("deletion_status")) == "deleted":
        return False
    try:
        updated = datetime.fromisoformat(str(record.get("updated_at"))).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return False
    artifact_type = str(record.get("type") or "")
    if str(record.get("lifecycle_state")) in {"partial", "failed", "orphan"}:
        return now - updated >= timedelta(days=float(policy["failed_grace_days"]))
    if artifact_type in {"preview", "proxy", "draft_output", "handoff_package"}:
        return now - updated >= timedelta(days=float(policy["preview_max_age_days"]))
    if artifact_type in {"segment_cache", "render_cache", "log"}:
        return now - updated >= timedelta(days=float(policy["cache_max_age_days"]))
    return False


def _deletion_reason(record: Mapping[str, Any], policy: Mapping[str, Any]) -> str:
    return f"unreferenced {record.get('type', 'artifact')} exceeded retention policy {RETENTION_POLICY_VERSION}"


def _specific_type(default: str, path: Path, folder: Path) -> str:
    relative = path.relative_to(folder)
    if relative.parts and relative.parts[0] == "source":
        return "source_media"
    if "approvals" in relative.parts:
        return "approval_snapshot"
    if relative.name == "render_manifest.json":
        return "manifest"
    if "graded_clips" in relative.parts:
        return "handoff_package"
    if "color_previews" in relative.parts or "storyboard_previews" in relative.parts:
        return "preview"
    return default


def _artifact_id(path: Path, size: int, digest: str) -> str:
    return "artifact-" + hashlib.sha256(f"{path}\0{size}\0{digest}".encode("utf-8")).hexdigest()[:24]


def _graph_hash(inventory: Mapping[str, Any]) -> str:
    payload = [{key: item.get(key) for key in ("artifact_id", "path", "sha256", "size", "references", "pinned", "lifecycle_state", "producer_job_status", "deletion_status")} for item in inventory.get("artifacts", [])]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


__all__ = ["DEFAULT_POLICY", "INVENTORY_SCHEMA_VERSION", "RetentionError", "build_cleanup_plan", "execute_cleanup_plan", "inventory_path", "load_inventory", "reconcile_inventory", "register_artifact", "save_inventory", "set_artifact_pinned"]

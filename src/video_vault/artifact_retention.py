"""Reference-aware artifact inventory and conservative cleanup plans."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
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
    "max_cache_size_bytes": 0,
    "keep_last_n": 0,
    "minimum_free_disk_bytes": 0,
    "pinned_exemption": True,
}
PROTECTED_TYPES = {"source_media", "approval_snapshot", "formal_output", "manifest", "runtime_asset", "qa_report", "qa_evidence"}


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
        "frame": folder / "frames",
        "perception_history": folder / "perception_runs",
        "segment_cache": folder / "cache" / "segments",
        "render_cache": folder / "cache",
        "formal_output": folder / "output",
        "approval_snapshot": folder / "approvals",
        "manifest": folder / "render_manifest.json",
        "log": folder / "logs",
        "qa_evidence": folder / "qa",
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
    recovered = 0
    for record in inventory.get("artifacts", []):
        path = Path(str(record.get("path") or ""))
        if (
            str(record.get("deletion_status") or "active") == "active"
            and not path.is_file()
        ):
            record["deletion_status"] = "missing"
            record["lifecycle_state"] = "missing"
            recovered += 1
    save_inventory(cfg, project_id, inventory)
    return {
        "ok": True,
        "project_id": int(project_id),
        "discovered": discovered,
        "recovered": recovered,
        "artifacts": inventory["artifacts"],
    }


def set_artifact_pinned(cfg: Mapping[str, Any], project_id: int, artifact_id: str, pinned: bool) -> dict[str, Any]:
    inventory = load_inventory(cfg, project_id)
    for record in inventory.get("artifacts", []):
        if str(record.get("artifact_id")) == str(artifact_id):
            record["pinned"] = bool(pinned)
            save_inventory(cfg, project_id, inventory)
            return record
    raise RetentionError("artifact_missing", "找不到指定 artifact", action="重新整理儲存空間清單")


def build_cleanup_plan(cfg: Mapping[str, Any], project_id: int, policy: Mapping[str, Any] | None = None, *, active_job_ids: set[str] | None = None) -> dict[str, Any]:
    reconcile_inventory(cfg, project_id)
    inventory = load_inventory(cfg, project_id)
    rules = {**DEFAULT_POLICY, **dict(policy or {})}
    now = datetime.now(timezone.utc)
    candidates: list[dict[str, Any]] = []
    protected: list[dict[str, Any]] = []
    records = list(inventory.get("artifacts", []))
    keep_ids = _keep_last_ids(records, int(rules.get("keep_last_n") or 0))
    capacity_ids = _capacity_candidate_ids(records, rules, project_dir(dict(cfg), int(project_id)))
    for record in inventory.get("artifacts", []):
        reason = _protection_reason(record, active_job_ids=active_job_ids)
        if reason:
            protected.append({"artifact_id": record.get("artifact_id"), "reason": reason, "type": record.get("type")})
            continue
        artifact_id = str(record.get("artifact_id") or "")
        if artifact_id in keep_ids:
            protected.append({"artifact_id": artifact_id, "reason": "retention keep last N", "type": record.get("type")})
            continue
        if _eligible(record, now, rules) or artifact_id in capacity_ids:
            candidate = {"artifact_id": record.get("artifact_id"), "project_id": int(project_id), "path": record.get("path"), "type": record.get("type"), "size": int(record.get("size") or 0), "reason": _deletion_reason(record, rules)}
            candidates.append(candidate)
    free_bytes = shutil.disk_usage(project_dir(dict(cfg), int(project_id))).free
    plan = {
        "plan_id": "cleanup-" + uuid.uuid4().hex,
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "policy_version": RETENTION_POLICY_VERSION,
        "project_id": int(project_id),
        "created_at": _now(),
        "expires_at": (now + timedelta(minutes=15)).isoformat(timespec="seconds"),
        "graph_hash": _graph_hash(inventory),
        "resume_graph_hash": _graph_hash(
            inventory,
            ignore_candidate_state={
                str(item.get("artifact_id") or "") for item in candidates
            },
        ),
        "policy": rules,
        "candidates": candidates,
        "protected": protected,
        "candidate_count": len(candidates),
        "candidate_size": sum(int(item["size"]) for item in candidates),
        "estimated_free_space": sum(int(item["size"]) for item in candidates),
        "current_free_space": int(free_bytes),
        "projected_free_space": int(free_bytes) + sum(int(item["size"]) for item in candidates),
    }
    plan_path = project_dir(dict(cfg), int(project_id)) / "storage" / f"{plan['plan_id']}.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return plan


def execute_cleanup_plan(cfg: Mapping[str, Any], plan: Mapping[str, Any], *, active_job_ids: set[str] | None = None) -> dict[str, Any]:
    project_id = int(plan.get("project_id") or 0)
    inventory = load_inventory(cfg, project_id)
    candidate_ids = {
        str(item.get("artifact_id") or "") for item in plan.get("candidates", [])
    }
    graph_matches = plan.get("graph_hash") == _graph_hash(inventory)
    resume_graph_matches = plan.get("resume_graph_hash") == _graph_hash(
        inventory,
        ignore_candidate_state=candidate_ids,
    )
    if not graph_matches and not resume_graph_matches:
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
            if _protection_reason(record, active_job_ids=active_job_ids):
                raise RetentionError("protected_artifact", "artifact 在清理前已受到保護", action="保留 artifact")
            resolved = path.resolve(strict=False)
            if root not in resolved.parents or resolved == root or path.is_symlink():
                raise RetentionError("path_boundary", "artifact path 不在核准 project root 內", action="保留 artifact")
            deletion_status = str(record.get("deletion_status") or "active")
            if deletion_status in {"deleted", "missing"}:
                if path.exists():
                    raise RetentionError("artifact_changed", "已處理的 artifact 路徑重新出現", action="重新建立 cleanup plan")
                result["status"] = f"already_{deletion_status}"
                results.append(result)
                continue
            if not path.exists():
                record["deletion_status"] = "missing"
                record["lifecycle_state"] = "missing"
                result["status"] = "already_missing"
                changed = True
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
            # Journal every completed item so an interrupted cleanup can be
            # reconciled without claiming that a deleted file is still active.
            save_inventory(cfg, project_id, inventory)
            changed = False
    return {"ok": True, "plan_id": plan.get("plan_id"), "results": results, "reclaimed_bytes": sum(int(item.get("size") or 0) for item, result in zip(plan.get("candidates", []), results) if result.get("status") in {"deleted", "already_missing"})}


def _protection_reason(record: Mapping[str, Any], *, active_job_ids: set[str] | None = None) -> str:
    if str(record.get("type")) in PROTECTED_TYPES:
        return "immutable or approval artifact"
    if bool(record.get("pinned")):
        return "user pinned"
    if str(record.get("producer_job_status")) in {"queued", "running", "cancelling"}:
        return "producer job is active"
    if active_job_ids and str(record.get("producer_job_id") or "") in active_job_ids:
        return "producer job is active"
    if str(record.get("lifecycle_state")) in {"current", "approved", "formal", "active"}:
        return "current/approved/formal lifecycle"
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


def free_disk_bytes(path: str | Path) -> int:
    target = Path(path).expanduser().resolve()
    while not target.exists() and target != target.parent:
        target = target.parent
    return int(shutil.disk_usage(target).free)


def estimate_render_required_bytes(manifest: Mapping[str, Any]) -> int:
    """Conservative temporary + output estimate before starting FFmpeg."""

    source_bytes = 0
    seen: set[str] = set()
    for segment in manifest.get("segments") or []:
        if not isinstance(segment, Mapping):
            continue
        source = str(segment.get("source_file") or "")
        if not source or source in seen:
            continue
        seen.add(source)
        try:
            source_bytes += Path(source).stat().st_size
        except OSError:
            continue
    expected_seconds = sum(
        float(item.get("timeline_duration_seconds") or 0)
        for item in manifest.get("segments") or []
        if isinstance(item, Mapping)
    )
    timeline_floor = int(max(1.0, expected_seconds) * 4 * 1024 * 1024)
    return max(64 * 1024 * 1024, timeline_floor, source_bytes * 2)


def ensure_render_free_space(
    cfg: Mapping[str, Any],
    manifest: Mapping[str, Any],
    output_path: str | Path,
) -> dict[str, int]:
    render = cfg.get("render") if isinstance(cfg.get("render"), Mapping) else {}
    reserve = int(render.get("minimum_free_disk_bytes") or 0)
    required = estimate_render_required_bytes(manifest)
    free = free_disk_bytes(Path(output_path).parent)
    if free < required + reserve:
        raise RetentionError(
            "insufficient_disk_space",
            "磁碟空間不足，已在開始昂貴的正式輸出前停止",
            action="先到儲存空間工作區清理未引用 cache，或改用其他輸出磁碟",
            details={
                "free_bytes": free,
                "required_bytes": required,
                "reserve_bytes": reserve,
            },
        )
    return {"free_bytes": free, "required_bytes": required, "reserve_bytes": reserve}


def _keep_last_ids(records: list[Mapping[str, Any]], keep_last_n: int) -> set[str]:
    if keep_last_n <= 0:
        return set()
    result: set[str] = set()
    cache_types = {"segment_cache", "render_cache", "preview", "proxy", "draft_output"}
    for artifact_type in cache_types:
        eligible = [
            item
            for item in records
            if str(item.get("type") or "") == artifact_type
            and not _protection_reason(item)
            and str(item.get("deletion_status") or "active") == "active"
        ]
        eligible.sort(
            key=lambda item: str(item.get("last_accessed_at") or item.get("updated_at") or ""),
            reverse=True,
        )
        result.update(str(item.get("artifact_id") or "") for item in eligible[:keep_last_n])
    return result


def _capacity_candidate_ids(
    records: list[Mapping[str, Any]],
    policy: Mapping[str, Any],
    root: Path,
) -> set[str]:
    cache_types = {"segment_cache", "render_cache", "preview", "proxy", "draft_output"}
    available = [
        item
        for item in records
        if str(item.get("type") or "") in cache_types
        and not _protection_reason(item)
        and str(item.get("deletion_status") or "active") == "active"
    ]
    available.sort(key=lambda item: str(item.get("last_accessed_at") or item.get("updated_at") or ""))
    total = sum(int(item.get("size") or 0) for item in available)
    max_bytes = int(policy.get("max_cache_size_bytes") or 0)
    free = shutil.disk_usage(root).free
    minimum_free = int(policy.get("minimum_free_disk_bytes") or 0)
    need = max(0, total - max_bytes) if max_bytes > 0 else 0
    need = max(need, max(0, minimum_free - int(free)))
    selected: set[str] = set()
    reclaimed = 0
    for item in available:
        if reclaimed >= need:
            break
        selected.add(str(item.get("artifact_id") or ""))
        reclaimed += int(item.get("size") or 0)
    return selected


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
    if relative.parts and relative.parts[0] == "qa":
        return "qa_report" if relative.name in {"report.json", "REPORT.md"} else "qa_evidence"
    if "graded_clips" in relative.parts or "opencut_handoff" in relative.parts or "hyperframes" in relative.parts:
        return "handoff_package"
    if "color_previews" in relative.parts or "storyboard_previews" in relative.parts:
        return "preview"
    if path.name.startswith("story_draft"):
        return "draft_output"
    return default


def _artifact_id(path: Path, size: int, digest: str) -> str:
    return "artifact-" + hashlib.sha256(f"{path}\0{size}\0{digest}".encode("utf-8")).hexdigest()[:24]


def _graph_hash(
    inventory: Mapping[str, Any],
    *,
    ignore_candidate_state: set[str] | None = None,
) -> str:
    ignored = ignore_candidate_state or set()
    payload = []
    for item in inventory.get("artifacts", []):
        keys = [
            "artifact_id",
            "path",
            "sha256",
            "size",
            "references",
            "pinned",
            "lifecycle_state",
            "producer_job_status",
            "deletion_status",
        ]
        if str(item.get("artifact_id") or "") in ignored:
            keys = [
                key
                for key in keys
                if key not in {"lifecycle_state", "deletion_status"}
            ]
        payload.append({key: item.get(key) for key in keys})
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


__all__ = [
    "DEFAULT_POLICY",
    "INVENTORY_SCHEMA_VERSION",
    "RetentionError",
    "build_cleanup_plan",
    "ensure_render_free_space",
    "estimate_render_required_bytes",
    "execute_cleanup_plan",
    "free_disk_bytes",
    "inventory_path",
    "load_inventory",
    "reconcile_inventory",
    "register_artifact",
    "save_inventory",
    "set_artifact_pinned",
]

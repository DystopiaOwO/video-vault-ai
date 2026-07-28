"""Versioned, approved-snapshot based delivery contracts.

The delivery layer deliberately keeps the approved snapshot as its source of
truth.  Diagnostic exports may inspect the current project, but are marked as
non-formal and can never be used as an approved handoff.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .approval_snapshot import ApprovalSnapshotError, load_approval_snapshot, validate_snapshot
from .project import project_dir, project_detail
from .render_manifest import manifest_hash, validate_render_manifest
from .segment_cache import build_segment_cache_key


HANDOFF_CONTRACT_VERSION = "handoff-v1"
HANDOFF_MODES = {"complete", "representative", "diagnostic_first_n"}


class HandoffError(ValueError):
    """Structured, user-actionable handoff failure."""

    def __init__(self, code: str, message: str, *, action: str = "", details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.category = "handoff"
        self.retryable = code in {"dependency_missing", "file_missing"}
        self.action = action
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "category": self.category,
            "retryable": self.retryable,
            "details": self.details,
            "action": self.action,
        }


def escape_ffconcat_path(path: str | Path) -> str:
    """Escape a path for an ffconcat file without shell interpolation."""

    value = str(Path(path).expanduser().resolve()).replace("\\", "/")
    return value.replace("\\", "\\\\").replace("'", "'\\''")


def load_approved_handoff_snapshot(cfg: Mapping[str, Any], db: Path, project_id: int) -> dict[str, Any]:
    folder = project_dir(dict(cfg), int(project_id))
    review_path = folder / "review_status.json"
    if not review_path.is_file():
        raise HandoffError("approval_required", "找不到核准資料，請先完成人工核准", action="先完成分鏡審核與核准")
    try:
        review = json.loads(review_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HandoffError("approval_invalid", f"核准資料無法讀取：{exc}", action="重新核准專案") from exc
    if review.get("approved_by_user") is not True or review.get("status") != "approved":
        raise HandoffError("approval_required", "專案尚未核准，正式交付已被拒絕", action="先完成人工核准")
    relative = str(review.get("approval_snapshot_path") or "")
    snapshot_id = str(review.get("approval_snapshot_id") or "")
    snapshot_hash = str(review.get("approval_snapshot_hash") or "")
    approvals = (folder / "approvals").resolve()
    path = (folder / relative).resolve() if relative else Path()
    if not relative or approvals not in path.parents or not path.is_file():
        raise HandoffError("approval_required", "缺少不可變核准 snapshot，請重新核准專案", action="重新核准專案")
    try:
        snapshot = load_approval_snapshot(path)
        validation = validate_snapshot(snapshot, check_assets=True)
    except ApprovalSnapshotError as exc:
        raise HandoffError("approval_invalid", f"核准 snapshot 無法驗證：{exc}", action="重新核准專案") from exc
    if not validation["valid"]:
        raise HandoffError("approval_invalid", "核准 snapshot 已失效：" + "; ".join(validation["errors"]), action="重新核准專案", details=validation)
    manifest = snapshot.get("manifest")
    if not isinstance(manifest, Mapping):
        raise HandoffError("approval_invalid", "核准 snapshot 缺少 Render Manifest", action="重新核准專案")
    manifest = dict(manifest)
    manifest_validation = validate_render_manifest(manifest)
    if not manifest_validation["valid"]:
        raise HandoffError("approval_invalid", "核准 Render Manifest 無效：" + "; ".join(manifest_validation["errors"]), action="重新核准專案")
    approved_hash = str(review.get("approved_manifest_hash") or snapshot.get("manifest_hash") or "")
    if not approved_hash or approved_hash != snapshot.get("manifest_hash") or approved_hash != manifest_hash(manifest):
        raise HandoffError("approval_invalid", "approved manifest hash 不一致，請重新核准", action="重新核准專案")
    if snapshot.get("snapshot_id") != snapshot_id or snapshot.get("snapshot_hash") != snapshot_hash:
        raise HandoffError("approval_invalid", "approval snapshot 指標不一致，請重新核准", action="重新核准專案")
    return {"review": review, "snapshot": snapshot, "manifest": manifest, "path": path}


def build_handoff_manifest(
    cfg: Mapping[str, Any],
    db: Path,
    project_id: int,
    *,
    mode: str = "complete",
    first_n: int | None = None,
) -> dict[str, Any]:
    if mode not in HANDOFF_MODES:
        raise HandoffError("invalid_mode", f"不支援的交付模式：{mode}", action="使用 complete、representative 或 diagnostic_first_n")
    approved = None
    if mode != "diagnostic_first_n":
        approved = load_approved_handoff_snapshot(cfg, db, project_id)
        source = approved["manifest"]
        snapshot = approved["snapshot"]
    else:
        try:
            approved = load_approved_handoff_snapshot(cfg, db, project_id)
            source = approved["manifest"]
            snapshot = approved["snapshot"]
        except HandoffError:
            detail = project_detail(dict(cfg), db, int(project_id))
            plan = detail.get("plan") or {}
            if not plan.get("groups"):
                # This compatibility path is deliberately limited to a
                # diagnostic preview. Formal handoffs never rebuild a plan.
                from .project import build_project_plan

                build_project_plan(dict(cfg), db, int(project_id))
                detail = project_detail(dict(cfg), db, int(project_id))
                plan = detail.get("plan") or {}
            source = {
                "project_id": int(project_id),
                "project_name": detail.get("project", {}).get("name", f"project_{project_id}"),
                "profile": {},
                "settings": {},
                "segments": [dict(item) for item in detail.get("segments", []) if item.get("include", True)],
                "visual_items": (plan.get("visual_items") or (plan.get("visual_timeline") or {}).get("items", [])),
                "bgm": [dict(item) for item in detail.get("bgm", [])],
                "manifest_hash": "",
            }
            snapshot = {}

    all_segments = [dict(item) for item in source.get("segments", []) if isinstance(item, Mapping)]
    if mode == "representative" and str(source.get("selection_mode") or "") != "representative":
        raise HandoffError("unsupported_selection", "此核准 snapshot 不是 representative selection，不能重新選片", action="建立 representative approval")
    if mode == "diagnostic_first_n":
        count = max(0, int(first_n if first_n is not None else 5))
        selected = all_segments[:count]
    else:
        selected = all_segments
    selected_ids = [_stable_segment_id(item) for item in selected]
    all_ids = [_stable_segment_id(item) for item in all_segments]
    omitted_ids = [item for item in all_ids if item not in set(selected_ids)]
    groups = [str(item.get("group") or "") for item in all_segments]
    covered_groups = sorted({group for item, group in zip(selected, groups) if group})
    omitted_groups = sorted({group for item, group in zip(all_segments, groups) if group and _stable_segment_id(item) in set(omitted_ids)})
    timeline_items = [_timeline_item(item, index) for index, item in enumerate(selected, 1)]
    cache_keys = []
    if source.get("profile") and source.get("project_id"):
        for item in selected:
            try:
                cache_keys.append({"stable_id": _stable_segment_id(item), "cache_key": build_segment_cache_key(source, item)})
            except (KeyError, OSError):
                cache_keys.append({"stable_id": _stable_segment_id(item), "cache_key": None})
    visual_items = [dict(item) for item in source.get("visual_items", []) if isinstance(item, Mapping)]
    payload: dict[str, Any] = {
        "contract_version": HANDOFF_CONTRACT_VERSION,
        "handoff_id": "handoff-" + _hash({"manifest": source.get("manifest_hash"), "project_id": int(project_id), "mode": mode, "ids": selected_ids})[:24],
        "handoff_type": "diagnostic" if mode == "diagnostic_first_n" else "formal",
        "project_id": int(project_id),
        "approved_snapshot_id": snapshot.get("snapshot_id"),
        "approved_manifest_hash": snapshot.get("manifest_hash") or source.get("manifest_hash") or None,
        "project_revision": (snapshot.get("approved_project_revision") if snapshot else None),
        "approved_timeline_contract_version": source.get("schema_version"),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool_versions": {"video_vault": "handoff-v1"},
        "profile": source.get("profile") or {},
        "timeline_items": timeline_items,
        "visual_items": visual_items,
        "audio": (snapshot.get("effective") or {}).get("audio") if snapshot else source.get("settings", {}).get("audio", {}),
        "bgm": [dict(item) for item in source.get("bgm", []) if isinstance(item, Mapping)],
        "credits": source.get("bgm_credits", []),
        "runtime_assets": snapshot.get("assets", []) if snapshot else [],
        "source_fingerprints": [item for item in (snapshot.get("assets", []) if snapshot else []) if item.get("kind") == "source"],
        "cache_keys": cache_keys,
        "exported_ids": selected_ids,
        "omitted_ids": omitted_ids,
        "covered_groups": covered_groups,
        "omitted_groups": omitted_groups,
        "selection_mode": mode,
        "selection_reason": "approved snapshot complete timeline" if mode == "complete" else ("approved representative selection" if mode == "representative" else f"diagnostic first {len(selected)} items"),
        "locked_conflicts": [],
        "files": [],
        "file_hashes": {},
    }
    if mode == "diagnostic_first_n":
        payload["non_formal_reason"] = "diagnostic_first_n 只供診斷，不代表完整核准時間軸"
    payload["contract_hash"] = _contract_hash(payload)
    return payload


def register_handoff_file(
    manifest: dict[str, Any],
    path: Path,
    *,
    package_root: Path | None = None,
    stable_id: str = "",
) -> None:
    path = path.resolve()
    if package_root is None:
        relative = Path(path.name)
    else:
        root = package_root.resolve()
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise HandoffError(
                "path_boundary",
                "handoff file 不在交付包根目錄內",
                action="只登錄交付包內的檔案",
            ) from exc
    relative_text = relative.as_posix()
    digest = _file_hash(path)
    manifest.setdefault("files", []).append(
        {
            "path": relative_text,
            "stable_id": stable_id,
            "sha256": digest,
            "size": path.stat().st_size,
        }
    )
    manifest.setdefault("file_hashes", {})[relative_text] = digest
    manifest["contract_hash"] = _contract_hash(manifest)


def write_handoff_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    try:
        temp.write_text(json.dumps(dict(manifest), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def import_handoff_package(cfg: Mapping[str, Any], project_id: int, package: str | Path) -> dict[str, Any]:
    """Validate and record a delivery import without changing project state."""

    root = Path(package).expanduser().resolve()
    manifest_path = root / "handoff_manifest.json" if root.is_dir() else root
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HandoffError("invalid_handoff", f"交付 manifest 無法讀取：{exc}", action="重新匯出交付包") from exc
    if not isinstance(manifest, dict) or manifest.get("contract_version") != HANDOFF_CONTRACT_VERSION:
        raise HandoffError("unsupported_handoff", "不支援的 handoff contract", action="使用相同版本重新匯出")
    expected = manifest.get("contract_hash")
    actual = _contract_hash(manifest)
    missing: list[str] = []
    changed: list[str] = []
    unsupported: list[str] = []
    for file_entry in manifest.get("files") or []:
        relative = str(file_entry.get("path") or "") if isinstance(file_entry, Mapping) else ""
        candidate = (root / relative).resolve() if relative else Path()
        if not relative or candidate == root or root not in candidate.parents or candidate.is_symlink() or Path(relative).is_absolute():
            unsupported.append(relative or "<empty>")
            continue
        if not candidate.is_file():
            missing.append(relative)
            continue
        try:
            if int(file_entry.get("size") or -1) != candidate.stat().st_size or str(file_entry.get("sha256") or "") != _file_hash(candidate):
                changed.append(relative)
        except OSError:
            changed.append(relative)
    status = "matched" if expected == actual and not missing and not changed and not unsupported else "needs-review"
    report = {
        "project_id": int(project_id),
        "status": status,
        "matched": list(manifest.get("exported_ids") or []) if status == "matched" else [],
        "changed": changed,
        "missing": missing + (list(manifest.get("omitted_ids") or []) if status != "matched" else []),
        "added": [],
        "ambiguous": [],
        "unsupported": unsupported,
        "manifest_hash": manifest.get("approved_manifest_hash"),
        "source_handoff": str(manifest_path.name),
    }
    report_path = project_dir(dict(cfg), int(project_id)) / "output" / "handoff_imports" / f"import-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    write_handoff_manifest(report_path, report)
    return {"ok": status == "matched", "code": "ok" if status == "matched" else "manifest_conflict", "report": report, "report_path": str(report_path)}


def _stable_segment_id(item: Mapping[str, Any]) -> str:
    return str(item.get("segment_id") or item.get("clip_id") or "").strip()


def _timeline_item(item: Mapping[str, Any], order: int) -> dict[str, Any]:
    result = dict(item)
    result["stable_id"] = _stable_segment_id(item)
    result["approved_order"] = order
    result["source_in_seconds"] = float(item.get("source_in_seconds", item.get("start_seconds", 0)))
    result["source_out_seconds"] = float(item.get("source_out_seconds", item.get("end_seconds", 0)))
    result["timeline_duration_seconds"] = float(item.get("timeline_duration_seconds") or (result["source_out_seconds"] - result["source_in_seconds"]) / float(item.get("speed") or 1))
    return result


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _contract_hash(manifest: Mapping[str, Any]) -> str:
    return _hash(
        {
            key: value
            for key, value in manifest.items()
            if key not in {"contract_hash", "created_at", "handoff_id"}
        }
    )


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "HANDOFF_CONTRACT_VERSION",
    "HANDOFF_MODES",
    "HandoffError",
    "build_handoff_manifest",
    "escape_ffconcat_path",
    "import_handoff_package",
    "load_approved_handoff_snapshot",
    "register_handoff_file",
    "write_handoff_manifest",
]

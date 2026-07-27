"""Immutable approved-render snapshots and asset fingerprint validation.

The snapshot is intentionally file backed.  It lets historical queued jobs
keep rendering the exact approved contract even after the project moves to a
new revision, without adding another mutable database model.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping
import uuid

from .audio_state import effective_project_audio_state, resolve_audio_state_bgm
from .color_consistency import effective_color_settings, load_project_color_state
from .project import project_dir
from .render_manifest import build_render_manifest, manifest_hash, validate_render_manifest


APPROVAL_SNAPSHOT_SCHEMA_VERSION = 1
APPROVAL_CONTRACT_VERSION = "approved-render-v1"


class ApprovalSnapshotError(ValueError):
    pass


def approval_snapshot_dir(cfg: Mapping[str, Any], project_id: int) -> Path:
    return project_dir(dict(cfg), int(project_id)) / "approvals"


def asset_fingerprint(path: str | Path, *, kind: str, asset_id: str = "", metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ApprovalSnapshotError(f"{kind} resource is missing or not a regular file: {source}")
    stat = source.stat()
    return {
        "kind": str(kind),
        "asset_id": str(asset_id or source.name),
        "canonical_path": str(source),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "sha256": _sha256(source),
        "metadata": _json_safe(dict(metadata or {})),
    }


def build_approval_snapshot(
    cfg: Mapping[str, Any],
    db: Path,
    project_id: int,
    *,
    approved_revision: int,
    manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build without writing files or changing project state."""
    manifest = deepcopy(manifest) if manifest is not None else build_render_manifest(dict(cfg), Path(db), int(project_id))
    validation = validate_render_manifest(manifest)
    if validation["errors"]:
        raise ApprovalSnapshotError("manifest is invalid: " + "; ".join(validation["errors"]))
    manifest_id = manifest_hash(manifest)
    if manifest.get("manifest_hash") != manifest_id:
        raise ApprovalSnapshotError("manifest hash is not deterministic")
    if len(manifest.get("bgm") or []) > 1:
        raise ApprovalSnapshotError("Phase 4A 只支援一首有效 BGM；請在音訊工作區明確選擇一首後再核准")
    assets = _manifest_assets(manifest)
    audio = effective_project_audio_state(dict(cfg), int(project_id), Path(db))
    selected_bgm = resolve_audio_state_bgm(Path(db), audio) if audio is not None else None
    if selected_bgm is None and len(manifest.get("bgm") or []) == 1:
        # Legacy project_bgm rows are already resolved into the manifest.
        selected_bgm = manifest["bgm"][0]
    unresolved_license = [
        track for track in manifest.get("bgm") or []
        if str(track.get("license_status") or "unverified") != "verified"
        or str(track.get("attribution_status") or "unknown") == "unknown"
    ]
    invalid_license = [
        track for track in unresolved_license
        if str(track.get("license_status") or "unverified") == "invalid"
    ]
    if invalid_license:
        raise ApprovalSnapshotError("存在無效 BGM 授權；請移除該曲目或補上有效授權證據後再核准")
    acknowledgement = (manifest.get("settings") or {}).get("license_acknowledgement") or {}
    if not isinstance(acknowledgement, Mapping):
        acknowledgement = {}
    acknowledged_ids = {int(value) for value in acknowledgement.get("track_ids", []) if str(value).isdigit()}
    if unresolved_license and not (
        bool(acknowledgement.get("accepted"))
        and all(int(track.get("track_id") or 0) in acknowledged_ids for track in unresolved_license)
    ):
        raise ApprovalSnapshotError("存在未確認 BGM 授權；請補齊授權證據，或以一次性的明確 acknowledgement 允許繼續")
    color_state = load_project_color_state(dict(cfg), int(project_id))
    effective_color = {
        str(item.get("segment_id")): effective_color_settings(color_state, str(item.get("segment_id")))
        for item in manifest.get("segments", [])
        if isinstance(item, Mapping)
    }
    payload = {
        "schema_version": APPROVAL_SNAPSHOT_SCHEMA_VERSION,
        "contract_version": APPROVAL_CONTRACT_VERSION,
        "project_id": int(project_id),
        "approved_project_revision": int(approved_revision),
        "manifest": deepcopy(manifest),
        "manifest_hash": manifest_id,
        "assets": assets,
        "effective": {
            "audio": _json_safe(audio or {"source": "legacy"}),
            "selected_bgm": _public_bgm(selected_bgm),
            "color": _json_safe({"project": color_state.get("applied", {}), "segments": effective_color}),
            "storyboard": _json_safe(manifest.get("storyboard_render_state") or {}),
            "render_profile": _json_safe(manifest.get("profile") or {}),
        },
    }
    snapshot_hash = _hash_payload(payload)
    snapshot_id = f"approval-{manifest_id[:16]}-{snapshot_hash[:12]}"
    return {
        **payload,
        "snapshot_id": snapshot_id,
        "snapshot_hash": snapshot_hash,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "approved",
    }


def publish_approval_snapshot(cfg: Mapping[str, Any], snapshot: Mapping[str, Any]) -> Path:
    project_id = int(snapshot.get("project_id") or 0)
    if project_id <= 0:
        raise ApprovalSnapshotError("snapshot project_id is invalid")
    validate_snapshot(snapshot, check_assets=False)
    folder = approval_snapshot_dir(cfg, project_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{snapshot['snapshot_id']}.json"
    if path.exists():
        existing = load_approval_snapshot(path)
        if existing.get("snapshot_hash") != snapshot.get("snapshot_hash"):
            raise ApprovalSnapshotError("snapshot id collision")
        return path
    temp = folder / f".snapshot-{uuid.uuid4().hex}.tmp"
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(dict(snapshot), stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    return path


def load_approval_snapshot(path: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ApprovalSnapshotError(f"approval snapshot cannot be read: {exc}") from exc
    if not isinstance(value, dict):
        raise ApprovalSnapshotError("approval snapshot must be a JSON object")
    return value


def validate_snapshot(snapshot: Mapping[str, Any], *, check_assets: bool = True) -> dict[str, Any]:
    errors: list[str] = []
    if int(snapshot.get("schema_version") or 0) != APPROVAL_SNAPSHOT_SCHEMA_VERSION:
        errors.append("unsupported approval snapshot schema")
    if str(snapshot.get("contract_version") or "") != APPROVAL_CONTRACT_VERSION:
        errors.append("unsupported approval contract version")
    manifest = snapshot.get("manifest")
    if not isinstance(manifest, Mapping):
        errors.append("snapshot manifest is missing")
    else:
        validation = validate_render_manifest(manifest)
        errors.extend("manifest: " + item for item in validation["errors"])
        current_manifest_hash = manifest_hash(manifest)
        if current_manifest_hash != snapshot.get("manifest_hash"):
            errors.append("snapshot manifest hash mismatch")
    payload = {key: deepcopy(value) for key, value in snapshot.items() if key not in {"snapshot_id", "snapshot_hash", "created_at", "status"}}
    if _hash_payload(payload) != snapshot.get("snapshot_hash"):
        errors.append("snapshot hash mismatch")
    mismatches: list[dict[str, Any]] = []
    if check_assets:
        for asset in snapshot.get("assets") or []:
            if not isinstance(asset, Mapping):
                errors.append("invalid asset fingerprint")
                continue
            mismatch = _asset_mismatch(asset)
            if mismatch:
                mismatches.append(mismatch)
                errors.append(f"asset fingerprint mismatch: {mismatch['asset_id']}")
    return {"valid": not errors, "errors": errors, "asset_mismatches": mismatches}


def _manifest_assets(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for segment in manifest.get("segments") or []:
        if not isinstance(segment, Mapping):
            continue
        source = str(segment.get("source_file") or "")
        if source:
            key = ("source", str(Path(source).expanduser().resolve()))
            if key not in seen:
                seen.add(key)
                assets.append(asset_fingerprint(source, kind="source", asset_id=str(segment.get("video_id") or source)))
        color = segment.get("color") if isinstance(segment.get("color"), Mapping) else {}
        for lut in _lut_paths(color):
            key = ("lut", str(lut))
            if key not in seen:
                seen.add(key)
                assets.append(asset_fingerprint(lut, kind="lut", asset_id=Path(lut).name))
    for track in manifest.get("bgm") or []:
        if not isinstance(track, Mapping) or not track.get("source_path"):
            continue
        source = str(Path(str(track["source_path"])).expanduser().resolve())
        key = ("bgm", source)
        if key not in seen:
            seen.add(key)
            assets.append(asset_fingerprint(source, kind="bgm", asset_id=str(track.get("track_id") or Path(source).name), metadata={key: track.get(key) for key in ("license_name", "license_url", "license_source_url", "source_url", "attribution_text", "attribution_status", "license_status", "verification_source", "verification_provenance")}))
    for item in manifest.get("visual_items") or []:
        if not isinstance(item, Mapping):
            continue
        for asset in item.get("runtime_assets") or []:
            if isinstance(asset, Mapping):
                source = str(asset.get("path") or asset.get("source_path") or "").strip()
            else:
                source = str(asset or "").strip()
            if not source:
                raise ApprovalSnapshotError(f"visual item {item.get('stable_id', '')} 缺少 runtime asset path")
            key = ("visual", str(Path(source).expanduser().resolve()))
            if key not in seen:
                seen.add(key)
                assets.append(asset_fingerprint(source, kind="visual", asset_id=str(item.get("stable_id") or Path(source).name)))
    return assets


def _lut_paths(value: Mapping[str, Any]) -> Iterable[str]:
    mode = str(value.get("mode") or "none")
    path = str(value.get("lut_path") or "").strip()
    if mode in {"dji_lut", "dji_dlog", "dji_dlog_m"}:
        try:
            from .color_pipeline import validate_lut_resource

            lut = validate_lut_resource(value, parse=True)
        except Exception as exc:
            raise ApprovalSnapshotError(str(exc)) from exc
        assert lut is not None
        yield str(lut)


def _asset_mismatch(asset: Mapping[str, Any]) -> dict[str, Any] | None:
    path = Path(str(asset.get("canonical_path") or ""))
    try:
        current = asset_fingerprint(path, kind=str(asset.get("kind") or "asset"), asset_id=str(asset.get("asset_id") or path.name), metadata=asset.get("metadata") if isinstance(asset.get("metadata"), Mapping) else {})
    except ApprovalSnapshotError:
        return {"asset_id": asset.get("asset_id") or path.name, "kind": asset.get("kind"), "reason": "missing"}
    for key in ("canonical_path", "size", "mtime_ns", "sha256"):
        if current.get(key) != asset.get(key):
            return {"asset_id": asset.get("asset_id") or path.name, "kind": asset.get("kind"), "reason": key}
    return None


def _public_bgm(track: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if track is None:
        return None
    allowed = ("track_id", "title", "artist", "license_name", "license_url", "license_source_url", "source_url", "attribution_status", "license_status", "license_verified_at", "verification_source", "verification_provenance", "gain_db", "start_seconds", "loop", "fade_in_seconds", "fade_out_seconds")
    return {key: track.get(key) for key in allowed}


def _hash_payload(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


__all__ = [
    "APPROVAL_CONTRACT_VERSION", "APPROVAL_SNAPSHOT_SCHEMA_VERSION", "ApprovalSnapshotError",
    "approval_snapshot_dir", "asset_fingerprint", "build_approval_snapshot", "load_approval_snapshot",
    "publish_approval_snapshot", "validate_snapshot",
]

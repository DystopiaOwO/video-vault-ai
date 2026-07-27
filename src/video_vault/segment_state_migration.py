"""Migrate project-authored segment state from mutable legacy IDs to stable UUIDs."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Mapping

from .database import connect, project_videos, segments, set_project_status


def migrate_segment_state_for_video(
    cfg: dict,
    db: Path,
    video_id: int,
    identity_report: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Migrate all projects that reference ``video_id`` and return project reports.

    The old project plan is intentionally read before it is rebuilt. This gives
    us the exact legacy ``clip_001_00012000`` keys used by existing user state.
    Unmatched entries are preserved in place and listed as orphans instead of
    being silently discarded.
    """
    with connect(db) as con:
        project_ids = [
            int(row["project_id"])
            for row in con.execute(
                "select project_id from project_videos where video_id=? order by project_id",
                (int(video_id),),
            ).fetchall()
        ]
    reports = []
    for project_id in project_ids:
        report = migrate_project_segment_state(
            cfg,
            db,
            project_id,
            video_id=int(video_id),
            identity_report=identity_report,
        )
        if report:
            reports.append(report)
    return reports


def migrate_project_segment_state(
    cfg: dict,
    db: Path,
    project_id: int,
    *,
    video_id: int | None = None,
    identity_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(cfg["library_root"]) / "08_projects" / f"project_{int(project_id)}"
    plan_path = root / "project_plan.json"
    if not plan_path.is_file():
        return {}
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}

    current_by_video: dict[int, list[dict[str, Any]]] = {}
    media_ids: dict[int, str] = {}
    for video in project_videos(db, int(project_id)):
        current_video_id = int(video["id"])
        current_by_video[current_video_id] = [dict(row) for row in segments(db, current_video_id)]
        media_ids[current_video_id] = str(video["project_media_uuid"] or "")

    alias_map: dict[str, str] = {}
    current_ids = {
        str(row.get("segment_uuid") or "")
        for rows in current_by_video.values()
        for row in rows
        if str(row.get("segment_uuid") or "")
    }
    plan_matches = []
    plan_changed = False
    for group in plan.get("groups", []) or []:
        if not isinstance(group, Mapping):
            continue
        for raw in group.get("segments", []) or []:
            if not isinstance(raw, Mapping):
                continue
            item = dict(raw)
            try:
                item_video_id = int(item.get("video_id") or 0)
            except (TypeError, ValueError):
                continue
            if video_id is not None and item_video_id != int(video_id):
                continue
            candidates = current_by_video.get(item_video_id, [])
            target = _match_plan_segment(item, candidates)
            if not target:
                continue
            stable_id = str(target.get("segment_uuid") or "")
            if not stable_id:
                continue
            aliases = {
                str(item.get("segment_id") or ""),
                _legacy_segment_id(item),
            }
            aliases.discard("")
            for alias in aliases:
                alias_map[alias] = stable_id
            if isinstance(raw, dict):
                revision = int(target.get("revision") or 1)
                project_media_id = media_ids.get(item_video_id, "")
                if str(raw.get("segment_id") or "") != stable_id:
                    raw["segment_id"] = stable_id
                    plan_changed = True
                if int(raw.get("segment_revision") or 0) != revision:
                    raw["segment_revision"] = revision
                    plan_changed = True
                if project_media_id and str(raw.get("project_media_id") or "") != project_media_id:
                    raw["project_media_id"] = project_media_id
                    plan_changed = True
            plan_matches.append(
                {
                    "video_id": item_video_id,
                    "legacy_ids": sorted(aliases),
                    "segment_uuid": stable_id,
                    "score": round(_plan_segment_score(item, target), 6),
                }
            )

    if plan_changed:
        _atomic_write(plan_path, json.dumps(plan, ensure_ascii=False, indent=2) + "\n")

    files = [
        (root / "feedback" / "segment_review.json", "list"),
        (root / "segment_review.json", "list"),
        (root / "storyboard.json", "segments"),
        (root / "audio_settings.json", "segments"),
        (root / "color_consistency.json", "segments"),
    ]
    migrated_files = ["project_plan.json"] if plan_changed else []
    orphaned = []
    conflicts = []
    migrated_keys = []
    for path, shape in files:
        result = _migrate_state_file(path, shape, alias_map, current_ids)
        if not result:
            continue
        migrated_files.append(str(path.relative_to(root)))
        orphaned.extend({"file": str(path.relative_to(root)), **item} for item in result["orphaned"])
        conflicts.extend({"file": str(path.relative_to(root)), **item} for item in result["conflicts"])
        migrated_keys.extend({"file": str(path.relative_to(root)), **item} for item in result["migrated"])

    upstream = dict(identity_report or {})
    identity_changed = bool(
        upstream.get("matched")
        or upstream.get("new")
        or upstream.get("removed")
        or upstream.get("splits")
        or upstream.get("merges")
        or upstream.get("ambiguous")
    )
    requires_review = bool(
        identity_changed
        or upstream.get("requires_review")
        or orphaned
        or conflicts
        or migrated_keys
        or plan_changed
    )
    report = {
        "schema_version": 2,
        "migration_contract": "stable-segment-state-v2",
        "project_id": int(project_id),
        "video_id": int(video_id) if video_id is not None else None,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "alias_map": alias_map,
        "plan_matches": plan_matches,
        "migrated_files": migrated_files,
        "migrated": migrated_keys,
        "orphaned": orphaned,
        "conflicts": conflicts,
        "upstream_identity_report": upstream,
        "requires_review": requires_review,
        "rollback_supported": True,
    }
    if alias_map or upstream or migrated_files:
        validation = root / "validation"
        validation.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        stamped = validation / f"segment_identity_migration_{stamp}.json"
        latest = validation / "segment_identity_migration_latest.json"
        payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        _atomic_write(stamped, payload)
        _atomic_write(latest, payload)
    if requires_review:
        _mark_project_needs_review(root, db, int(project_id))
    return report


def _migrate_state_file(
    path: Path,
    shape: str,
    alias_map: Mapping[str, str],
    current_ids: set[str],
) -> dict[str, list[dict[str, Any]]]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    migrated: list[dict[str, Any]] = []
    orphaned: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    changed = False
    if shape == "list":
        if not isinstance(data, list):
            return {}
        seen: dict[str, dict[str, Any]] = {}
        result = []
        for raw in data:
            if not isinstance(raw, Mapping):
                continue
            item = dict(raw)
            old_id = str(item.get("segment_id") or "")
            new_id = str(alias_map.get(old_id) or old_id)
            if old_id and new_id != old_id:
                item["segment_id"] = new_id
                migrated.append({"from": old_id, "to": new_id})
                changed = True
            if new_id and new_id in seen:
                conflicts.append({"segment_id": new_id, "reason": "duplicate_after_migration"})
                seen[new_id].update(item)
                changed = True
                continue
            if new_id:
                seen[new_id] = item
            if new_id and new_id not in current_ids:
                orphaned.append({"segment_id": new_id, "reason": "no_current_segment"})
            result.append(item)
        data = result
    else:
        if not isinstance(data, dict) or not isinstance(data.get("segments"), dict):
            return {}
        source = dict(data["segments"])
        result: dict[str, Any] = {}
        for old_id, value in source.items():
            old_key = str(old_id)
            new_key = str(alias_map.get(old_key) or old_key)
            if new_key != old_key:
                migrated.append({"from": old_key, "to": new_key})
                changed = True
            if new_key in result:
                conflicts.append({"segment_id": new_key, "reason": "duplicate_after_migration"})
                result[new_key] = _merge_state_values(result[new_key], value)
                changed = True
            else:
                result[new_key] = value
            if new_key and new_key not in current_ids:
                orphaned.append({"segment_id": new_key, "reason": "no_current_segment"})
        data["segments"] = result
    if changed:
        _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return {"migrated": migrated, "orphaned": orphaned, "conflicts": conflicts}


def _merge_state_values(left: Any, right: Any) -> Any:
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        result = dict(left)
        result.update(dict(right))
        return result
    return right


def _match_plan_segment(plan_segment: Mapping[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    requested_id = str(plan_segment.get("segment_id") or plan_segment.get("segment_uuid") or "")
    if requested_id:
        direct = next((row for row in candidates if str(row.get("segment_uuid") or "") == requested_id), None)
        if direct:
            return direct
    ranked = sorted(
        ((_plan_segment_score(plan_segment, candidate), candidate) for candidate in candidates),
        key=lambda pair: pair[0],
        reverse=True,
    )
    if not ranked or ranked[0][0] < 0.45:
        return None
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.08:
        return None
    return ranked[0][1]


def _plan_segment_score(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    left_start = float(left.get("start_seconds") or 0)
    left_end = float(left.get("end_seconds") or left_start)
    right_start = float(right.get("start_seconds") or 0)
    right_end = float(right.get("end_seconds") or right_start)
    overlap = max(0.0, min(left_end, right_end) - max(left_start, right_start))
    union = max(0.001, max(left_end, right_end) - min(left_start, right_start))
    iou = overlap / union
    left_duration = max(0.001, left_end - left_start)
    right_duration = max(0.001, right_end - right_start)
    scale = max(1.0, left_duration, right_duration)
    midpoint_similarity = max(
        0.0,
        1.0 - abs((left_start + left_end) / 2 - (right_start + right_end) / 2) / scale,
    )
    return 0.8 * iou + 0.2 * midpoint_similarity


def _legacy_segment_id(segment: Mapping[str, Any]) -> str:
    return f"{segment.get('clip_id', 'clip')}_{int(float(segment.get('start_seconds') or 0) * 1000):08d}"


def _mark_project_needs_review(root: Path, db: Path, project_id: int) -> None:
    set_project_status(db, project_id, "needs_review")
    review_path = root / "review_status.json"
    if review_path.is_file():
        try:
            review = json.loads(review_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            review = {}
        review.update(
            {
                "status": "needs_review",
                "approved_by_user": False,
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "segment_identity_review_required": True,
            }
        )
        for key in ("approved_manifest_hash", "approved_plan_id", "approved_at"):
            review.pop(key, None)
        _atomic_write(review_path, json.dumps(review, ensure_ascii=False, indent=2) + "\n")
    plan_path = root / "project_plan.json"
    if plan_path.is_file():
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            plan = {}
        if isinstance(plan, dict):
            plan["status"] = "needs_review"
            plan["segment_identity_review_required"] = True
            _atomic_write(plan_path, json.dumps(plan, ensure_ascii=False, indent=2) + "\n")


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)


__all__ = ["migrate_project_segment_state", "migrate_segment_state_for_video"]

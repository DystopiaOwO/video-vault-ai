"""Small, user-owned calibration contract based only on approved outputs."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any, Iterable, Mapping

from .approval_snapshot import load_approval_snapshot
from .database import connect
from .project import list_projects, project_dir
from .story_profiles import load_project_story_settings


CALIBRATION_SCHEMA_VERSION = 1


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def calibration_path(cfg: Mapping[str, Any], profile_id: str) -> Path:
    return Path(str(cfg["library_root"])) / "08_projects" / "calibration" / f"{str(profile_id)}.json"


def compute_calibration(records: Iterable[Mapping[str, Any]], profile_id: str) -> dict[str, Any]:
    approved = [dict(item) for item in records if bool(item.get("approved")) and str(item.get("story_profile") or profile_id) == profile_id]
    if not approved:
        return {"schema_version": CALIBRATION_SCHEMA_VERSION, "profile_id": profile_id, "status": "insufficient_data", "sample_count": 0, "metrics": {}}
    durations = [float(value) for item in approved for value in (item.get("shot_durations") or []) if float(value) > 0]
    chapter_durations = [float(value) for item in approved for value in (item.get("chapter_durations") or []) if float(value) > 0]
    title_cards = [float(item.get("title_card_count") or 0) for item in approved]
    segment_counts = [float(item.get("segment_count") or 0) for item in approved]
    duplicate_reuse = [float(item.get("duplicate_reuse_count") or 0) for item in approved]
    natural_total = sum(float(item.get("natural_audio_total") or 0) for item in approved)
    natural_kept = sum(float(item.get("natural_audio_kept") or 0) for item in approved)
    metrics = {
        "shot_duration_median": median(durations) if durations else None,
        "shot_duration_stddev": pstdev(durations) if len(durations) > 1 else 0.0 if durations else None,
        "chapter_duration_median": median(chapter_durations) if chapter_durations else None,
        "title_card_density": mean(title_cards) / mean(segment_counts) if title_cards and segment_counts and mean(segment_counts) else None,
        "duplicate_reuse_ratio": sum(duplicate_reuse) / sum(segment_counts) if segment_counts and sum(segment_counts) else None,
        "natural_audio_retention_ratio": natural_kept / natural_total if natural_total else None,
    }
    return {
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "profile_id": profile_id,
        "status": "ready",
        "sample_count": len(approved),
        "metrics": metrics,
        "source": "approved_outputs_only",
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def save_calibration(cfg: Mapping[str, Any], profile_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(payload)
    value["schema_version"] = CALIBRATION_SCHEMA_VERSION
    value["profile_id"] = profile_id
    _atomic_json(calibration_path(cfg, profile_id), value)
    return value


def load_calibration(cfg: Mapping[str, Any], profile_id: str) -> dict[str, Any]:
    path = calibration_path(cfg, profile_id)
    if not path.is_file():
        return compute_calibration([], profile_id)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return compute_calibration([], profile_id)


def reset_calibration(cfg: Mapping[str, Any], profile_id: str) -> None:
    calibration_path(cfg, profile_id).unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _generation_for_project(db: Path, project_id: int, generation_uuid: str) -> dict[str, Any]:
    if not generation_uuid:
        return {}
    with connect(db) as con:
        row = con.execute(
            "select normalized_response_json, review_state_json from story_generations where project_id=? and story_generation_uuid=? and status='succeeded'",
            (int(project_id), generation_uuid),
        ).fetchone()
    if not row:
        return {}
    result: dict[str, Any] = {}
    for key in ("normalized_response_json", "review_state_json"):
        try:
            result[key.removesuffix("_json")] = json.loads(str(row[key] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            result[key.removesuffix("_json")] = {}
    return result


def collect_approved_calibration_records(cfg: Mapping[str, Any], db: Path, profile_id: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for project in list_projects(db):
        project_id = int(project.get("id") or 0)
        settings = load_project_story_settings(cfg, db, project_id)
        if str(settings.get("profile_id") or "") != str(profile_id):
            continue
        folder = project_dir(dict(cfg), project_id)
        review = _read_json(folder / "review_status.json")
        if review.get("approved_by_user") is not True or str(review.get("status") or "") != "approved":
            continue
        snapshot_path = str(review.get("approval_snapshot_path") or "")
        if not snapshot_path:
            continue
        snapshot_file = (folder / snapshot_path).resolve()
        if folder.resolve() not in snapshot_file.parents or not snapshot_file.is_file():
            continue
        try:
            approval = load_approval_snapshot(snapshot_file)
        except (OSError, ValueError):
            continue
        manifest = approval.get("manifest") or {}
        manifest_segments = [item for item in manifest.get("segments") or [] if isinstance(item, Mapping)]
        durations = [float(item.get("duration_seconds") or item.get("timeline_duration_seconds") or 0) for item in manifest_segments]
        story = _generation_for_project(db, project_id, str(project.get("current_story_generation_uuid") or ""))
        effective_story = story.get("review_state") or story.get("normalized_response") or {}
        chapters = [item for item in effective_story.get("chapters") or [] if isinstance(item, Mapping)]
        duration_by_id = {str(item.get("segment_id") or ""): float(item.get("duration_seconds") or item.get("timeline_duration_seconds") or 0) for item in manifest_segments}
        chapter_durations = [sum(duration_by_id.get(str(segment_id), 0.0) for segment_id in chapter.get("segment_uuids") or []) for chapter in chapters]
        visual_items = ((manifest.get("visual_timeline") or {}).get("items") or [])
        title_card_count = sum(1 for item in visual_items if isinstance(item, Mapping) and str(item.get("kind") or "").lower() in {"title_card", "title-card", "title"})
        suppressed = effective_story.get("suppressed_segments") or []
        audio_kept = sum(1 for item in manifest_segments if str(item.get("audio_role") or item.get("effective_audio_role") or "keep") not in {"mute", "bgm_only"})
        records.append({
            "approved": True,
            "story_profile": str(profile_id),
            "project_id": project_id,
            "approval_snapshot_id": str(approval.get("snapshot_id") or ""),
            "approval_snapshot_hash": str(approval.get("snapshot_hash") or ""),
            "shot_durations": [value for value in durations if value > 0],
            "chapter_durations": [value for value in chapter_durations if value > 0],
            "title_card_count": title_card_count,
            "segment_count": len(manifest_segments),
            "duplicate_reuse_count": len(suppressed),
            "natural_audio_total": len(manifest_segments),
            "natural_audio_kept": audio_kept,
        })
    return records


def calibration_for_profile(cfg: Mapping[str, Any], db: Path, profile_id: str) -> dict[str, Any]:
    persisted = load_calibration(cfg, profile_id)
    if persisted.get("status") != "insufficient_data" or calibration_path(cfg, profile_id).is_file():
        return persisted
    return compute_calibration(collect_approved_calibration_records(cfg, db, profile_id), profile_id)


def recalculate_calibration(cfg: Mapping[str, Any], db: Path, profile_id: str) -> dict[str, Any]:
    records = collect_approved_calibration_records(cfg, db, profile_id)
    value = compute_calibration(records, profile_id)
    value["record_count"] = len(records)
    value["source"] = "approved_outputs_and_render_metadata"
    return save_calibration(cfg, profile_id, value)


__all__ = [
    "CALIBRATION_SCHEMA_VERSION", "calibration_for_profile", "calibration_path",
    "collect_approved_calibration_records", "compute_calibration", "load_calibration",
    "recalculate_calibration", "reset_calibration", "save_calibration",
]

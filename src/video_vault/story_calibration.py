"""Small, user-owned calibration contract based only on approved outputs."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any, Iterable, Mapping


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


__all__ = ["CALIBRATION_SCHEMA_VERSION", "calibration_path", "compute_calibration", "load_calibration", "reset_calibration", "save_calibration"]

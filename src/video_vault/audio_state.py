"""Project-level audio settings with a server-owned/user-editable boundary."""

from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
from typing import Any, Mapping

from .project import mark_project_needs_review, project_dir


AUDIO_STATE_VERSION = 1
AUDIO_ROLES = frozenset({"keep", "lower", "mute", "bgm_only"})
LEGACY_AUDIO_ROLE_MAP = {
    "keep_original": "keep",
    "lower_original": "lower",
    "mute": "mute",
}


def audio_state_path(cfg: dict, project_id: int) -> Path:
    return project_dir(cfg, project_id) / "audio_settings.json"


def default_audio_state() -> dict[str, Any]:
    return {
        "schema_version": AUDIO_STATE_VERSION,
        "enabled": True,
        "bgm": {
            "bgm_id": None,
            "enabled": False,
            "volume_db": -18.0,
            "start_seconds": 0.0,
            "loop": True,
            "fade_in_seconds": 1.5,
            "fade_out_seconds": 2.0,
        },
        "original_audio": {"default_role": "lower", "default_volume_db": 0.0, "lower_volume_db": -8.0},
        "normalization": {"enabled": True, "target_lufs": -14.0, "true_peak_db": -1.0},
        "segments": {},
    }


def load_audio_state(cfg: dict, project_id: int) -> dict[str, Any]:
    path = audio_state_path(cfg, project_id)
    if not path.is_file():
        return default_audio_state()
    try:
        return normalize_audio_state(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return default_audio_state()


def save_audio_state(cfg: dict, db: Path, project_id: int, state: Mapping[str, Any], *, mark_review: bool = True) -> Path:
    normalized = normalize_audio_state(state)
    path = audio_state_path(cfg, project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)
    if mark_review:
        mark_project_needs_review(cfg, db, project_id)
    return path


def update_audio_state(cfg: dict, db: Path, project_id: int, patch: Mapping[str, Any]) -> dict[str, Any]:
    current = load_audio_state(cfg, project_id)
    updated = _deep_merge(current, editable_audio_patch(patch))
    state = normalize_audio_state(updated)
    save_audio_state(cfg, db, project_id, state)
    return state


def audio_state_for_api(cfg: dict, project_id: int, db: Path | None = None) -> dict[str, Any]:
    """Return editable audio settings without exposing local media paths."""
    state = load_audio_state(cfg, project_id)
    result = deepcopy(state)
    bgm = result.get("bgm")
    if isinstance(bgm, dict):
        for key in ("source_path", "file_path", "path"):
            bgm.pop(key, None)
        if db is not None and bgm.get("bgm_id") is not None:
            from .database import project_bgm_tracks

            track = next((dict(row) for row in project_bgm_tracks(db, project_id) if int(row["id"]) == int(bgm["bgm_id"])), None)
            if track:
                bgm["track"] = {
                    "id": int(track["id"]),
                    "title": str(track.get("title") or ""),
                    "artist": str(track.get("artist") or ""),
                    "source_url": str(track.get("source_url") or ""),
                    "license_name": str(track.get("license_name") or ""),
                    "attribution_text": str(track.get("attribution_text") or ""),
                    "duration_seconds": track.get("duration_seconds"),
                }
    return result


def editable_audio_patch(patch: Mapping[str, Any] | None) -> dict[str, Any]:
    """Extract only fields a browser may change; preserve server-owned BGM metadata."""
    if not isinstance(patch, Mapping):
        return {}
    result: dict[str, Any] = {}
    if "enabled" in patch:
        result["enabled"] = _as_bool(patch["enabled"])
    for section in ("original_audio", "normalization"):
        value = patch.get(section)
        if isinstance(value, Mapping):
            result[section] = deepcopy(dict(value))
    bgm = patch.get("bgm")
    if isinstance(bgm, Mapping):
        allowed = {"bgm_id", "enabled", "volume_db", "start_seconds", "loop", "fade_in_seconds", "fade_out_seconds"}
        result["bgm"] = {key: deepcopy(bgm[key]) for key in allowed if key in bgm}
    segments = patch.get("segments")
    if isinstance(segments, Mapping):
        allowed = {"role", "volume_db", "fade_in_seconds", "fade_out_seconds", "locked"}
        result["segments"] = {
            str(segment_id): {key: deepcopy(value[key]) for key in allowed if key in value}
            for segment_id, value in segments.items()
            if isinstance(value, Mapping)
        }
    return result


def normalize_audio_role(value: Any) -> str:
    role = LEGACY_AUDIO_ROLE_MAP.get(str(value or ""), str(value or "lower"))
    if role not in AUDIO_ROLES:
        raise ValueError(f"unsupported audio role: {role}")
    return role


def normalize_audio_state(state: Mapping[str, Any] | None) -> dict[str, Any]:
    base = default_audio_state()
    source = dict(state or {})
    result = _deep_merge(base, source)
    result["schema_version"] = AUDIO_STATE_VERSION
    result["enabled"] = _as_bool(result.get("enabled", True))

    bgm = dict(result.get("bgm") or {})
    bgm["bgm_id"] = _optional_positive_int(bgm.get("bgm_id"))
    bgm["enabled"] = _as_bool(bgm.get("enabled", False))
    bgm["volume_db"] = _clamped_number(bgm.get("volume_db", -18.0), -60.0, 12.0, "bgm.volume_db")
    bgm["start_seconds"] = _number(bgm.get("start_seconds", 0.0), 0.0, 86400.0, "bgm.start_seconds")
    bgm["loop"] = _as_bool(bgm.get("loop", True))
    bgm["fade_in_seconds"] = _number(bgm.get("fade_in_seconds", 1.5), 0.0, 3600.0, "bgm.fade_in_seconds")
    bgm["fade_out_seconds"] = _number(bgm.get("fade_out_seconds", 2.0), 0.0, 3600.0, "bgm.fade_out_seconds")
    result["bgm"] = bgm

    original = dict(result.get("original_audio") or {})
    original["default_role"] = normalize_audio_role(original.get("default_role", "lower"))
    original["default_volume_db"] = _clamped_number(original.get("default_volume_db", -8.0), -60.0, 12.0, "original_audio.default_volume_db")
    original["lower_volume_db"] = _clamped_number(original.get("lower_volume_db", -8.0), -60.0, 12.0, "original_audio.lower_volume_db")
    result["original_audio"] = original

    normalization = dict(result.get("normalization") or {})
    normalization["enabled"] = _as_bool(normalization.get("enabled", True))
    normalization["target_lufs"] = _number(normalization.get("target_lufs", -14.0), -40.0, 0.0, "normalization.target_lufs")
    normalization["true_peak_db"] = _number(normalization.get("true_peak_db", -1.0), -20.0, 0.0, "normalization.true_peak_db")
    result["normalization"] = normalization

    segments: dict[str, Any] = {}
    for segment_id, value in (result.get("segments") or {}).items():
        if not isinstance(value, Mapping):
            continue
        item = dict(value)
        item["role"] = normalize_audio_role(item.get("role", result["original_audio"]["default_role"]))
        item["volume_db"] = _clamped_number(item.get("volume_db", 0.0), -60.0, 12.0, f"segments.{segment_id}.volume_db")
        item["fade_in_seconds"] = _number(item.get("fade_in_seconds", 0.1), 0.0, 60.0, f"segments.{segment_id}.fade_in_seconds")
        item["fade_out_seconds"] = _number(item.get("fade_out_seconds", 0.1), 0.0, 60.0, f"segments.{segment_id}.fade_out_seconds")
        item["locked"] = _as_bool(item.get("locked", False))
        segments[str(segment_id)] = item
    result["segments"] = segments
    return result


def _number(value: Any, lower: float, upper: float, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(number) or not lower <= number <= upper:
        raise ValueError(f"{field} must be between {lower} and {upper}")
    return round(number, 6)


def _clamped_number(value: Any, lower: float, upper: float, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return round(min(upper, max(lower, number)), 6)


def _optional_positive_int(value: Any) -> int | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("bgm.bgm_id must be a positive integer or null") from exc
    if number <= 0:
        raise ValueError("bgm.bgm_id must be a positive integer or null")
    return number


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off", "none"}
    return bool(value)


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(base))
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge(dict(result[key]), dict(value))
        else:
            result[key] = deepcopy(value)
    return result


__all__ = [
    "AUDIO_ROLES", "AUDIO_STATE_VERSION", "LEGACY_AUDIO_ROLE_MAP", "audio_state_for_api", "audio_state_path",
    "default_audio_state", "editable_audio_patch", "load_audio_state", "normalize_audio_role",
    "normalize_audio_state", "save_audio_state", "update_audio_state",
]

"""Project-level audio settings with a server-owned/user-editable boundary."""

from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
from typing import Any, Mapping

from .project import mark_project_needs_review, project_dir
from .project_lifecycle import project_commit


AUDIO_STATE_VERSION = 1
AUDIO_ROLES = frozenset({"keep", "lower", "mute", "bgm_only"})
LEGACY_AUDIO_ROLE_MAP = {
    "keep_original": "keep",
    "lower_original": "lower",
    "mute": "mute",
}


def audio_state_path(cfg: dict, project_id: int) -> Path:
    return project_dir(cfg, project_id) / "audio_settings.json"


def has_audio_state(cfg: dict, project_id: int) -> bool:
    return audio_state_path(cfg, project_id).is_file()


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
        "original_audio": {
            "default_role": "lower",
            "default_volume_db": 0.0,
            "lower_volume_db": -8.0,
            "fade_in_seconds": 0.1,
            "fade_out_seconds": 0.1,
        },
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


def effective_project_audio_state(cfg: dict, project_id: int) -> dict[str, Any] | None:
    """Return the new project audio state only when its workflow is enabled.

    ``None`` means legacy behavior.  It is deliberately different from a
    mute state: disabling this workflow must not silence the original media.
    """
    if not has_audio_state(cfg, project_id):
        return None
    state = load_audio_state(cfg, project_id)
    return state if state.get("enabled", True) else None


def effective_segment_audio_settings(
    cfg: dict,
    project_id: int,
    segment: Mapping[str, Any],
    *,
    state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    active = dict(state) if state is not None else effective_project_audio_state(cfg, project_id)
    if active is None:
        return {"role": str(segment.get("audio_role") or "lower_original"), "legacy": True}
    configured = dict((active.get("segments") or {}).get(str(segment.get("segment_id")), {}) or {})
    original = dict(active.get("original_audio") or {})
    role_source = configured.get("role") or original.get("default_role") or segment.get("audio_role") or "lower"
    role = normalize_audio_role(role_source)
    default_volume = original.get("lower_volume_db", -8.0) if role == "lower" else original.get("default_volume_db", 0.0)
    return {
        "role": role,
        "volume_db": float(configured.get("volume_db", default_volume if role not in {"mute", "bgm_only"} else 0.0)),
        "fade_in_seconds": float(configured.get("fade_in_seconds", original.get("fade_in_seconds", 0.1))),
        "fade_out_seconds": float(configured.get("fade_out_seconds", original.get("fade_out_seconds", 0.1))),
        "locked": bool(configured.get("locked", False)),
        "legacy": False,
    }


def effective_project_bgm(state: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if state is None:
        return None
    bgm = dict(state.get("bgm") or {})
    if not bgm.get("enabled") or bgm.get("bgm_id") is None:
        return None
    return bgm


def resolve_audio_state_bgm(db: Path, state: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Resolve a new audio state's selected BGM from the global library.

    The new audio workflow intentionally does not consult ``project_bgm``.
    That table remains the compatibility path for projects without an active
    ``audio_settings.json`` workflow.
    """
    selected = effective_project_bgm(state)
    if selected is None:
        return None
    from .database import bgm_tracks

    bgm_id = int(selected["bgm_id"])
    row = next((dict(item) for item in bgm_tracks(db) if int(item["id"]) == bgm_id), None)
    if row is None:
        raise ValueError("找不到指定 BGM")
    return {
        **row,
        "track_id": bgm_id,
        "source_path": str(row.get("file_path") or ""),
        "gain_db": float(selected.get("volume_db", -18.0)),
        "start_seconds": float(selected.get("start_seconds", 0.0)),
        "loop": bool(selected.get("loop", True)),
        "fade_in_seconds": float(selected.get("fade_in_seconds", 1.5)),
        "fade_out_seconds": float(selected.get("fade_out_seconds", 2.0)),
    }


def resolve_legacy_project_bgm(db: Path, project_id: int, settings: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return only the legacy project BGM rows and their legacy overrides."""
    from .database import project_bgm_tracks

    overrides = {
        int(item.get("track_id")): item
        for item in (settings or {}).get("bgm", [])
        if isinstance(item, Mapping) and item.get("track_id") is not None
    }
    rows: list[dict[str, Any]] = []
    for row in project_bgm_tracks(db, project_id):
        item = dict(row)
        item.update(overrides.get(int(item["id"]), {}))
        item["track_id"] = int(item["id"])
        item["source_path"] = str(item.get("file_path") or "")
        rows.append(item)
    return rows


def save_audio_state(cfg: dict, db: Path, project_id: int, state: Mapping[str, Any], *, mark_review: bool = True, base_revision: int | None = None) -> Path:
    with project_commit(db, project_id, base_revision) as commit:
        normalized = normalize_audio_state(state)
        current = normalize_audio_state(load_audio_state(cfg, project_id))
        # Creating the file is itself meaningful: it opts the project into
        # the new audio workflow, even when the saved value equals defaults.
        if normalized == current and audio_state_path(cfg, project_id).is_file():
            commit.record_changed(False)
            return audio_state_path(cfg, project_id)
        path = _save_audio_state(cfg, db, project_id, normalized, mark_review=mark_review)
        commit.record_changed(True)
        return path


def _save_audio_state(cfg: dict, db: Path, project_id: int, state: Mapping[str, Any], *, mark_review: bool = True) -> Path:
    normalized = normalize_audio_state(state)
    path = audio_state_path(cfg, project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)
    if mark_review:
        mark_project_needs_review(cfg, db, project_id)
    return path


def update_audio_state(cfg: dict, db: Path, project_id: int, patch: Mapping[str, Any], *, base_revision: int | None = None) -> dict[str, Any]:
    with project_commit(db, project_id, base_revision) as commit:
        current = load_audio_state(cfg, project_id)
        updated = _apply_audio_patch(current, editable_audio_patch(patch))
        state = normalize_audio_state(updated)
        changed = state != normalize_audio_state(current)
        commit.record_changed(changed)
        if changed:
            _save_audio_state(cfg, db, project_id, state, mark_review=True)
        return state


def _update_audio_state(cfg: dict, db: Path, project_id: int, patch: Mapping[str, Any]) -> dict[str, Any]:
    current = load_audio_state(cfg, project_id)
    updated = _apply_audio_patch(current, editable_audio_patch(patch))
    state = normalize_audio_state(updated)
    save_audio_state(cfg, db, project_id, state)
    return state


def audio_state_for_api(cfg: dict, project_id: int, db: Path | None = None) -> dict[str, Any]:
    """Return editable audio settings without exposing local media paths."""
    state = load_audio_state(cfg, project_id)
    result = deepcopy(state)
    result["settings_exists"] = has_audio_state(cfg, project_id)
    result["source"] = "new" if result["settings_exists"] else "legacy"
    result["migration"] = {"state": "none", "warning": ""}
    bgm = result.get("bgm")
    if isinstance(bgm, dict):
        for key in ("source_path", "file_path", "path"):
            bgm.pop(key, None)
        if db is not None and bgm.get("bgm_id") is not None:
            try:
                track = resolve_audio_state_bgm(db, state)
            except ValueError:
                track = None
            if track:
                bgm["track"] = {
                    "id": int(track["id"]),
                    "title": str(track.get("title") or ""),
                    "artist": str(track.get("artist") or ""),
                    "source_url": str(track.get("source_url") or ""),
                    "license_name": str(track.get("license_name") or ""),
                    "license_url": str(track.get("license_url") or ""),
                    "attribution_required": bool(track.get("attribution_required")),
                    "attribution_text": str(track.get("attribution_text") or ""),
                    "duration_seconds": track.get("duration_seconds"),
                }
                result["effective_selected_track"] = deepcopy(bgm["track"])
        if db is not None and not result["settings_exists"]:
            legacy = resolve_legacy_project_bgm(db, project_id)
            if len(legacy) == 1:
                result["migration"] = {"state": "legacy_single", "warning": "第一次儲存會保留既有單一 BGM 輸出"}
            elif len(legacy) > 1:
                result["migration"] = {"state": "legacy_multiple", "warning": "正式核准前必須選擇單一 BGM"}
            else:
                result["migration"] = {"state": "legacy_empty", "warning": "目前未選擇 BGM"}
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
            str(segment_id): (
                None if value is None else {key: deepcopy(value[key]) for key in allowed if key in value}
            )
            for segment_id, value in segments.items()
            if value is None or isinstance(value, Mapping)
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
    original["fade_in_seconds"] = _number(original.get("fade_in_seconds", 0.1), 0.0, 60.0, "original_audio.fade_in_seconds")
    original["fade_out_seconds"] = _number(original.get("fade_out_seconds", 0.1), 0.0, 60.0, "original_audio.fade_out_seconds")
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
        if "role" in item:
            item["role"] = normalize_audio_role(item["role"])
        if "volume_db" in item:
            item["volume_db"] = _clamped_number(item["volume_db"], -60.0, 12.0, f"segments.{segment_id}.volume_db")
        if "fade_in_seconds" in item:
            item["fade_in_seconds"] = _number(item["fade_in_seconds"], 0.0, 60.0, f"segments.{segment_id}.fade_in_seconds")
        if "fade_out_seconds" in item:
            item["fade_out_seconds"] = _number(item["fade_out_seconds"], 0.0, 60.0, f"segments.{segment_id}.fade_out_seconds")
        if "locked" in item:
            item["locked"] = _as_bool(item["locked"])
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


def _apply_audio_patch(base: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    result = _deep_merge(base, patch)
    segment_patch = patch.get("segments")
    if isinstance(segment_patch, Mapping):
        segments = deepcopy(dict(base.get("segments") or {}))
        for segment_id, value in segment_patch.items():
            key = str(segment_id)
            if value is None:
                segments.pop(key, None)
            elif isinstance(value, Mapping):
                existing = segments.get(key) if isinstance(segments.get(key), Mapping) else {}
                segments[key] = _deep_merge(existing, value)
        result["segments"] = segments
    return result


__all__ = [
    "AUDIO_ROLES", "AUDIO_STATE_VERSION", "LEGACY_AUDIO_ROLE_MAP", "audio_state_for_api", "audio_state_path",
    "effective_project_audio_state", "effective_project_bgm", "effective_segment_audio_settings", "has_audio_state",
    "resolve_audio_state_bgm", "resolve_legacy_project_bgm",
    "default_audio_state", "editable_audio_patch", "load_audio_state", "normalize_audio_role",
    "normalize_audio_state", "save_audio_state", "update_audio_state", "_apply_audio_patch",
]

"""Project-level render settings for the manifest contract phase."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

from .project import mark_project_needs_review, project_dir
from .render_profiles import get_render_profile


def default_render_settings() -> dict[str, Any]:
    return {
        "profile_id": "final_1080p",
        "encoder": "auto",
        "color": {"mode": "none", "lut_path": ""},
        "audio": {
            "original_gain_db": 0.0,
            "lower_original_gain_db": -12.0,
            "bgm_gain_db": -18.0,
        },
        "transition": {"type": "cut", "duration_seconds": 0.0},
        "overlay": {"enabled": False},
    }


def load_render_settings(cfg: dict, project_id: int) -> dict[str, Any]:
    path = project_dir(cfg, project_id) / "render_settings.json"
    if not path.exists():
        return default_render_settings()
    return _normalize(json.loads(path.read_text(encoding="utf-8")))


def save_render_settings(cfg: dict, db: Path, project_id: int, settings: dict[str, Any]) -> Path:
    normalized = _normalize(settings)
    path = project_dir(cfg, project_id) / "render_settings.json"
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)
    mark_project_needs_review(cfg, db, project_id)
    return path


def _normalize(settings: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(settings, dict):
        raise ValueError("render settings must be an object")
    result = _merge(default_render_settings(), settings)
    profile_id = str(result.get("profile_id") or "")
    get_render_profile(profile_id)
    result["profile_id"] = profile_id
    encoder = str(result.get("encoder") or "auto").strip()
    if not encoder:
        raise ValueError("render settings encoder cannot be empty")
    result["encoder"] = encoder

    color = result["color"]
    if not isinstance(color, dict):
        raise ValueError("render settings color must be an object")
    color["mode"] = str(color.get("mode") or "none")
    color["lut_path"] = str(color.get("lut_path") or "")

    audio = result["audio"]
    if not isinstance(audio, dict):
        raise ValueError("render settings audio must be an object")
    for key in ("original_gain_db", "lower_original_gain_db", "bgm_gain_db"):
        audio[key] = _finite_float(audio.get(key), key)

    transition = result["transition"]
    if not isinstance(transition, dict):
        raise ValueError("render settings transition must be an object")
    transition["type"] = str(transition.get("type") or "cut")
    transition["duration_seconds"] = max(0.0, _finite_float(transition.get("duration_seconds"), "duration_seconds"))

    overlay = result["overlay"]
    if not isinstance(overlay, dict):
        raise ValueError("render settings overlay must be an object")
    overlay["enabled"] = _as_bool(overlay.get("enabled", False))
    return result


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _finite_float(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"render settings {field} must be numeric") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"render settings {field} must be finite")
    return round(number, 6)


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off", "none"}
    return bool(value)


__all__ = ["default_render_settings", "load_render_settings", "save_render_settings"]

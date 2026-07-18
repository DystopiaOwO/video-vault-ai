"""Render-time color filter decisions for normalized segment outputs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .render_types import ColorSettings


COLOR_MODES = frozenset({"none", "safe_restore", "warm_food", "dji_lut"})


class ColorPipelineError(ValueError):
    pass


def validate_color_settings(settings: ColorSettings | Mapping[str, Any] | None) -> ColorSettings:
    data = _data(settings)
    mode = str(data.get("mode", "none"))
    if mode not in COLOR_MODES:
        raise ColorPipelineError(f"Unsupported color mode: {mode}")
    lut = data.get("lut_path") or data.get("dji_lut_path")
    if mode == "dji_lut":
        if not lut:
            raise ColorPipelineError("dji_lut requires lut_path")
        if not Path(lut).expanduser().is_file():
            raise ColorPipelineError(f"LUT file does not exist: {lut}")
    return ColorSettings(
        mode=mode, lut_path=str(lut) if lut else None,
        decision=str(data.get("decision", "")), reference_clip_id=data.get("reference_clip_id"),
        brightness=float(data.get("brightness", 0.0)), saturation=float(data.get("saturation", 1.0)),
        gamma=float(data.get("gamma", 1.0)),
    )


def color_filter(settings: ColorSettings | Mapping[str, Any] | None, *, lut_already_applied: bool = False) -> str:
    """Build a single FFmpeg video filter chain; LUT application is guarded."""

    color = validate_color_settings(settings)
    if lut_already_applied and color.mode == "dji_lut":
        raise ColorPipelineError("LUT must not be applied more than once")
    filters: list[str] = []
    if color.mode == "dji_lut":
        filters.append(f"lut3d=file='{_escape_filter_path(color.lut_path or '')}'")
    elif color.mode == "safe_restore":
        filters.append("eq=brightness=0.02:gamma=1.04:saturation=1.02")
    elif color.mode == "warm_food":
        filters.append("eq=brightness=0.01:gamma=1.02:saturation=1.08")
    if color.brightness or color.saturation != 1.0 or color.gamma != 1.0:
        filters.append(f"eq=brightness={color.brightness:g}:saturation={color.saturation:g}:gamma={color.gamma:g}")
    return ",".join(filters)


def check_color_metadata(metadata: Mapping[str, Any] | None) -> list[str]:
    """Return explicit warnings for HDR/HLG/PQ sources needing a project decision."""

    data = {str(k): str(v).lower() for k, v in (metadata or {}).items()}
    transfer = data.get("color_transfer", "")
    warnings: list[str] = []
    if any(token in transfer for token in ("smpte2084", "pq", "arib-std-b67", "hlg")):
        warnings.append(f"HDR transfer metadata detected: {transfer}; verify the color transform before rendering")
    return warnings


def _data(settings: Any) -> dict[str, Any]:
    if settings is None:
        return {}
    if isinstance(settings, Mapping):
        return dict(settings)
    return {name: getattr(settings, name) for name in settings.__dataclass_fields__}


def _escape_filter_path(path: str) -> str:
    return path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


__all__ = ["COLOR_MODES", "ColorPipelineError", "check_color_metadata", "color_filter", "validate_color_settings"]

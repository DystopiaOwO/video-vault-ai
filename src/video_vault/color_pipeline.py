"""Pure color filter builders for normalized segment rendering."""

from __future__ import annotations

from pathlib import Path
import math
from typing import Any, Mapping


COLOR_MODES = frozenset({"none", "safe_restore", "warm_food", "manual", "dji_lut", "dji_dlog", "dji_dlog_m"})
LUT_MODES = frozenset({"dji_lut", "dji_dlog", "dji_dlog_m"})


class ColorPipelineError(ValueError):
    pass


def build_color_filter(settings: Mapping[str, Any] | None, *, lut_already_applied: bool = False) -> str:
    data = dict(settings or {})
    mode = str(data.get("mode") or "none")
    if mode not in COLOR_MODES:
        raise ColorPipelineError(f"unsupported color mode: {mode}")
    if mode == "none":
        return ""
    if lut_already_applied:
        raise ColorPipelineError("LUT must not be applied more than once")
    parts: list[str] = []
    if mode in LUT_MODES:
        lut = Path(str(data.get("lut_path") or "")).expanduser().resolve()
        if not lut.is_file():
            raise ColorPipelineError(f"LUT file does not exist: {lut}")
        parts.append(build_lut3d_filter(lut))

    values = {
        "exposure": _number(data.get("exposure"), 0.0),
        "contrast": _number(data.get("contrast"), 1.0),
        "saturation": _number(data.get("saturation"), 1.0),
        "gamma": _number(data.get("gamma"), 1.0),
    }
    if mode == "safe_restore":
        values.update(exposure=-0.08, contrast=0.98, saturation=0.96, gamma=0.94)
    elif mode == "warm_food":
        values.update(contrast=1.06, saturation=1.12, gamma=0.98, exposure=-0.05)
    parts.append(_eq_filter(values))

    temperature = _number(data.get("temperature"), 0.0)
    tint = _number(data.get("tint"), 0.0)
    if temperature or tint:
        warm = _clamp(temperature / 30.0 * 0.15, -0.15, 0.15)
        green = _clamp(tint / 20.0 * 0.1, -0.1, 0.1)
        parts.append(f"colorbalance=rs={warm:.6f}:gs={green:.6f}:bs={-warm:.6f}")
    return ",".join(part for part in parts if part)


def _number(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(upper, max(lower, value))


def _eq_filter(values: Mapping[str, float]) -> str:
    exposure = _clamp(float(values["exposure"]), -1.5, 1.0)
    contrast = _clamp(float(values["contrast"]), 0.85, 1.15)
    saturation = _clamp(float(values["saturation"]), 0.8, 1.2)
    gamma = _clamp(float(values["gamma"]), 0.85, 1.15)
    brightness = _clamp(exposure * 0.12, -0.2, 0.2)
    return f"eq=contrast={contrast:.6f}:saturation={saturation:.6f}:gamma={gamma:.6f}:brightness={brightness:.6f}"


def color_filter(settings: Mapping[str, Any] | None, *, lut_already_applied: bool = False) -> str:
    return build_color_filter(settings, lut_already_applied=lut_already_applied)


def escape_filter_option_value(value: Path | str) -> str:
    """Escape a path for FFmpeg's option and filtergraph parsing levels."""
    normalized = str(value).replace("\\", "/")
    escaped: list[str] = []
    for char in normalized:
        if char == ":":
            escaped.append(r"\\:")
        elif char == "'":
            escaped.append(r"\\\'")
        elif char in ",;[]":
            escaped.append("\\" + char)
        else:
            escaped.append(char)
    return "".join(escaped)


def escape_filtergraph_value(value: Path | str) -> str:
    """Escape characters that have meaning at the filtergraph level."""
    return escape_filter_option_value(value)


def build_lut3d_filter(path: Path | str) -> str:
    lut = Path(path).expanduser().resolve()
    if not lut.is_file():
        raise ColorPipelineError(f"LUT file does not exist: {lut}")
    # Keep the value unquoted so apostrophes remain valid filenames.
    return f"lut3d=file={escape_filtergraph_value(lut)}"


def escape_filter_path(path: Path | str) -> str:
    """Backward-compatible alias for the filtergraph-safe path escaping."""
    return escape_filtergraph_value(path)


__all__ = [
    "COLOR_MODES",
    "LUT_MODES",
    "ColorPipelineError",
    "build_color_filter",
    "build_lut3d_filter",
    "color_filter",
    "escape_filter_option_value",
    "escape_filter_path",
    "escape_filtergraph_value",
]

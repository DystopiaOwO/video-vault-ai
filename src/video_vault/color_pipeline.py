"""Pure color filter builders for normalized segment rendering."""

from __future__ import annotations

from pathlib import Path
import math
import subprocess
from typing import Any, Mapping


COLOR_MODES = frozenset({"none", "safe_restore", "warm_food", "manual", "dji_lut", "dji_dlog", "dji_dlog_m"})
LUT_MODES = frozenset({"dji_lut", "dji_dlog", "dji_dlog_m"})
LUT_PRODUCT_CONTRACT = {
    "version": "1",
    "strategy": "user_managed",
    "modes": {mode: {"requires_lut": True, "extension": ".cube"} for mode in sorted(LUT_MODES)},
}


class ColorPipelineError(ValueError):
    pass


def color_mode_contract(mode: str) -> dict[str, Any]:
    token = str(mode or "none")
    return {"mode": token, "requires_lut": token in LUT_MODES, "strategy": LUT_PRODUCT_CONTRACT["strategy"], "version": LUT_PRODUCT_CONTRACT["version"]}


def validate_lut_resource(settings: Mapping[str, Any] | None, *, ffmpeg_path: str = "ffmpeg", parse: bool = False) -> Path | None:
    data = dict(settings or {})
    mode = str(data.get("mode") or "none")
    if mode not in LUT_MODES:
        return None
    raw = str(data.get("lut_path") or "").strip()
    if not raw:
        raise ColorPipelineError(f"{mode} requires a user-managed .cube LUT")
    lut = Path(raw).expanduser().resolve()
    if not lut.is_file():
        raise ColorPipelineError(f"LUT file does not exist: {lut}")
    if lut.suffix.lower() != ".cube":
        raise ColorPipelineError(f"{mode} requires a .cube LUT")
    if parse:
        command = [str(ffmpeg_path), "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "lavfi", "-i", "color=c=black:s=16x16:d=0.04", "-frames:v", "1", "-vf", build_lut3d_filter(lut), "-f", "null", "-"]
        try:
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ColorPipelineError(f"unable to validate LUT with FFmpeg: {exc}") from exc
        if result.returncode != 0:
            raise ColorPipelineError("FFmpeg cannot parse LUT: " + (result.stderr or result.stdout or "unknown error")[-500:])
    return lut


def build_color_filter(settings: Mapping[str, Any] | None, *, lut_already_applied: bool = False) -> str:
    data = dict(settings or {})
    mode = str(data.get("mode") or "none")
    if mode not in COLOR_MODES:
        raise ColorPipelineError(f"unsupported color mode: {mode}")
    if mode == "none":
        return ""
    if lut_already_applied:
        raise ColorPipelineError("LUT must not be applied more than once")
    lut_parts: list[str] = []
    if mode in LUT_MODES:
        lut = validate_lut_resource(data)
        assert lut is not None
        lut_parts.append(build_lut3d_filter(lut))

    values = {key: _number(data.get(key), default) for key, default in (("exposure", 0.0), ("contrast", 1.0), ("saturation", 1.0), ("gamma", 1.0), ("highlights", 0.0), ("shadows", 0.0))}
    if mode == "safe_restore":
        values.update(exposure=-0.08, contrast=0.98, saturation=0.96, gamma=0.94)
    elif mode == "warm_food":
        values.update(contrast=1.06, saturation=1.12, gamma=0.98, exposure=-0.05)
    adjustment_filters = _adjustment_filters(values)

    temperature = _number(data.get("temperature"), 0.0)
    tint = _number(data.get("tint"), 0.0)
    if temperature or tint:
        warm = _clamp(temperature / 30.0 * 0.15, -0.15, 0.15)
        green = _clamp(tint / 20.0 * 0.1, -0.1, 0.1)
        white_balance = f"colorbalance=rs={warm:.6f}:gs={green:.6f}:bs={-warm:.6f}"
        exposure_filters = [item for item in adjustment_filters if item.startswith("eq=brightness=")]
        adjustment_filters = [item for item in adjustment_filters if item not in exposure_filters]
        parts = [*lut_parts, *exposure_filters, white_balance, *adjustment_filters]
    else:
        parts = [*lut_parts, *adjustment_filters]
    return ",".join(part for part in parts if part)


def _number(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(upper, max(lower, value))


def _adjustment_filters(values: Mapping[str, float]) -> list[str]:
    exposure = _clamp(float(values["exposure"]), -1.5, 1.0)
    contrast = _clamp(float(values["contrast"]), 0.85, 1.15)
    saturation = _clamp(float(values["saturation"]), 0.8, 1.2)
    gamma = _clamp(float(values["gamma"]), 0.85, 1.15)
    brightness = _clamp(exposure * 0.12, -0.2, 0.2)
    filters: list[str] = []
    if exposure:
        filters.append(f"eq=brightness={brightness:.6f}")
    if contrast != 1.0 or gamma != 1.0:
        filters.append(f"eq=contrast={contrast:.6f}:gamma={gamma:.6f}")
    highlights = _clamp(float(values.get("highlights", 0.0)), -1.0, 1.0)
    shadows = _clamp(float(values.get("shadows", 0.0)), -1.0, 1.0)
    if highlights or shadows:
        filters.append(f"curves=all='0/{_curve_low(shadows):.4f} 0.5/{_curve_midpoint(shadows, highlights):.4f} 1/{_curve_high(highlights):.4f}'")
    if saturation != 1.0:
        filters.append(f"eq=saturation={saturation:.6f}")
    return filters


def _curve_low(shadows: float) -> float:
    return _clamp(shadows * 0.12, 0.0, 1.0)


def _curve_high(highlights: float) -> float:
    return _clamp(1.0 - highlights * 0.12, 0.0, 1.0)


def _curve_midpoint(shadows: float, highlights: float) -> float:
    return _clamp(0.5 + shadows * 0.08 - highlights * 0.08, 0.0, 1.0)


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
    "LUT_PRODUCT_CONTRACT",
    "LUT_MODES",
    "ColorPipelineError",
    "color_mode_contract",
    "build_color_filter",
    "build_lut3d_filter",
    "color_filter",
    "escape_filter_option_value",
    "escape_filter_path",
    "escape_filtergraph_value",
    "validate_lut_resource",
]

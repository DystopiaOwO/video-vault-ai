"""Pure color filter builders for normalized segment rendering."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping


COLOR_MODES = frozenset({"none", "dji_lut"})


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
    lut = Path(str(data.get("lut_path") or "")).expanduser().resolve()
    if not lut.is_file():
        raise ColorPipelineError(f"LUT file does not exist: {lut}")
    return build_lut3d_filter(lut)


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
    "ColorPipelineError",
    "build_color_filter",
    "build_lut3d_filter",
    "color_filter",
    "escape_filter_option_value",
    "escape_filter_path",
    "escape_filtergraph_value",
]

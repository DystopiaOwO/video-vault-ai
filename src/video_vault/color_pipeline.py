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
    lut = Path(str(data.get("lut_path") or "")).expanduser()
    if not lut.is_file():
        raise ColorPipelineError(f"LUT file does not exist: {lut}")
    return f"lut3d=file='{escape_filter_path(lut)}'"


def color_filter(settings: Mapping[str, Any] | None, *, lut_already_applied: bool = False) -> str:
    return build_color_filter(settings, lut_already_applied=lut_already_applied)


def escape_filter_path(path: Path | str) -> str:
    value = str(path).replace("\\", "/")
    return value.replace(":", r"\:").replace("'", r"\'")


__all__ = ["COLOR_MODES", "ColorPipelineError", "build_color_filter", "color_filter", "escape_filter_path"]

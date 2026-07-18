"""Errors and narrowly-scoped encoder fallback rules for Phase 3."""

from __future__ import annotations


class RenderError(RuntimeError):
    pass


class MediaProbeError(RenderError):
    pass


class SegmentRenderError(RenderError):
    pass


FALLBACK_MARKERS = (
    "unknown encoder",
    "encoder not found",
    "cannot load nvcuda",
    "cannot load nvenc",
    "no capable devices found",
    "nvenc initialization failed",
    "no nvenc capable devices found",
    "gpu encoder unavailable",
    "unsupported encoder",
)


def is_encoder_fallback_error(error: BaseException | str) -> bool:
    text = error if isinstance(error, str) else f"{error} {getattr(error, 'stderr', '')}"
    return any(marker in text.casefold() for marker in FALLBACK_MARKERS)


__all__ = ["FALLBACK_MARKERS", "MediaProbeError", "RenderError", "SegmentRenderError", "is_encoder_fallback_error"]

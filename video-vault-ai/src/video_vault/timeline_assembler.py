"""Manifest-ordered timeline assembly helpers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .render_types import RenderManifest, RenderSegment


@dataclass(frozen=True)
class AssemblyItem:
    segment_id: str
    source_file: str
    source_in_ms: int
    source_out_ms: int
    speed: float
    timeline_start_ms: int
    timeline_duration_ms: int
    audio_role: str


def ordered_segments(manifest: RenderManifest) -> tuple[RenderSegment, ...]:
    """Return the manifest's immutable order; never sort here."""
    return tuple(manifest.segments)


def assembly_items(manifest: RenderManifest) -> tuple[AssemblyItem, ...]:
    cursor = 0
    items: list[AssemblyItem] = []
    starts: list[int] = []
    for segment in ordered_segments(manifest):
        duration = max(0, int(segment.timeline_duration_ms or round((segment.source_out_ms - segment.source_in_ms) / segment.speed)))
        start = int(segment.timeline_start_ms)
        # The manifest list is authoritative; do not reorder it. A normal
        # forward overlap is invalid, while review lists may be non-monotonic.
        if starts and start >= starts[-1] and start < cursor:
            raise ValueError(f"segment {segment.segment_id} has overlapping timeline position")
        items.append(AssemblyItem(segment.segment_id, segment.source_file, segment.source_in_ms, segment.source_out_ms, segment.speed, start, duration, segment.audio_role))
        starts.append(start)
        cursor = start + duration
    return tuple(items)


def concat_file_lines(manifest: RenderManifest, rendered_paths: Sequence[str]) -> str:
    """Create a safe concat demuxer list in manifest order."""
    items = assembly_items(manifest)
    if len(items) != len(rendered_paths):
        raise ValueError("rendered path count does not match manifest segments")
    lines = []
    for path in rendered_paths:
        escaped = str(path).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    return "\n".join(lines) + "\n"


def validate_timeline(manifest: RenderManifest) -> list[str]:
    errors: list[str] = []
    previous_end = 0
    for item in assembly_items(manifest):
        if item.source_in_ms < 0 or item.source_out_ms < item.source_in_ms:
            errors.append(f"invalid source range for {item.segment_id}")
        if item.timeline_start_ms < 0:
            errors.append(f"negative timeline position for {item.segment_id}")
        previous_end = item.timeline_start_ms + item.timeline_duration_ms
    if previous_end != manifest.timeline_duration_ms and manifest.segments:
        errors.append("timeline duration does not match manifest")
    return errors


__all__ = ["AssemblyItem", "assembly_items", "concat_file_lines", "ordered_segments", "validate_timeline"]

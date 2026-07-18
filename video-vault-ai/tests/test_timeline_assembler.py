import pytest

from video_vault.render_types import RenderManifest, RenderSegment
from video_vault.timeline_assembler import assembly_items, validate_timeline


def test_assembly_uses_manifest_order_without_sorting():
    manifest = RenderManifest(
        segments=[
            RenderSegment("second", "b.mp4", 0, 1000, manual_order=2, timeline_start_ms=0, timeline_duration_ms=1000),
            RenderSegment("first", "a.mp4", 0, 1000, manual_order=1, timeline_start_ms=1000, timeline_duration_ms=1000),
        ], timeline_duration_ms=2000,
    )
    assert [item.segment_id for item in assembly_items(manifest)] == ["second", "first"]
    assert validate_timeline(manifest) == []


def test_overlapping_manifest_is_rejected():
    manifest = RenderManifest(
        segments=[RenderSegment("a", "a.mp4", 0, 1000, timeline_start_ms=0, timeline_duration_ms=1000),
                  RenderSegment("b", "b.mp4", 0, 1000, timeline_start_ms=500, timeline_duration_ms=1000)],
        timeline_duration_ms=1500,
    )
    with pytest.raises(ValueError):
        assembly_items(manifest)

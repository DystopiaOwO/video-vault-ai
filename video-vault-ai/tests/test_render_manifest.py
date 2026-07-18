from video_vault.render_manifest import compile_manifest, manifest_hash
from video_vault.render_types import RenderKind


def test_manifest_orders_reviews_excludes_segments_and_hashes_stably():
    plan = {
        "project_id": 7,
        "plan_id": "plan_v001",
        "clips": [{"clip_id": "clip_001", "duration_seconds": 20}],
        "groups": [{"segments": [
            {"segment_id": "b", "clip_id": "clip_001", "source_file": "b.mp4", "start_seconds": 5.125, "end_seconds": 7.125, "title": "B"},
            {"segment_id": "a", "clip_id": "clip_001", "source_file": "a.mp4", "start_seconds": 1, "end_seconds": 3, "title": "A"},
        ]}],
    }
    review = [{"segment_id": "b", "manual_order": 1, "speed": 2}, {"segment_id": "a", "manual_order": 2, "include": False}]
    first = compile_manifest(plan, review, {"profile": "final_1080p30", "kind": "final"})
    second = compile_manifest(plan, review, {"profile": "final_1080p30", "kind": "final"})
    assert first.render_kind is RenderKind.FINAL
    assert [item.segment_id for item in first.segments] == ["b"]
    assert first.segments[0].source_in_ms == 5125
    assert first.segments[0].timeline_duration_ms == 1000
    assert first.timeline_duration_ms == 1000
    assert first.manifest_hash == second.manifest_hash == manifest_hash(first)


from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from video_vault.visual_compositor import (
    VisualCompositionError,
    render_visual_cards,
    resolve_visual_timeline,
    stable_visual_hash,
)


PROFILE = {
    "profile_id": "preview_1080p30",
    "width": 320,
    "height": 180,
    "fps": 30,
    "video_codec": "libx264",
    "pixel_format": "yuv420p",
    "audio_codec": "aac",
    "audio_sample_rate": 48000,
    "audio_channels": 2,
    "color_primaries": "bt709",
    "color_transfer": "bt709",
    "color_matrix": "bt709",
    "color_range": "tv",
}


def _segments():
    return [
        {"segment_id": "a", "group_id": "morning", "order": 1, "timeline_duration_seconds": 2.0},
        {"segment_id": "b", "group_id": "afternoon", "order": 2, "timeline_duration_seconds": 3.0},
    ]


def test_chapter_card_uses_actual_group_boundary_after_speed_change():
    timeline = {
        "schema_version": 1,
        "contract_version": "visual-timeline-v1",
        "items": [
            {
                "stable_id": "chapter-afternoon",
                "type": "chapter_card",
                "group_id": "afternoon",
                "start_seconds": 999,
                "duration_seconds": 1,
                "text": "Afternoon",
                "style_id": "location-lower-left",
                "animation_id": "static",
            }
        ],
    }
    resolved = resolve_visual_timeline(timeline, _segments(), PROFILE, require_assets=False)
    item = resolved["resolved_items"][0]
    assert item["resolved_start_seconds"] == 2.0
    assert resolved["resolved_duration_seconds"] == 6.0
    assert resolved["sequence"][1]["kind"] == "visual"


def test_lower_third_is_overlay_and_does_not_extend_duration():
    timeline = {
        "schema_version": 1,
        "items": [
            {
                "stable_id": "location",
                "type": "lower_third",
                "segment_id": "b",
                "offset_seconds": 0.25,
                "duration_seconds": 1,
                "text": "Station",
                "style_id": "lower-third",
                "animation_id": "static",
            }
        ],
    }
    resolved = resolve_visual_timeline(timeline, _segments(), PROFILE, require_assets=False)
    item = resolved["resolved_items"][0]
    assert item["resolved_start_seconds"] == 2.25
    assert resolved["resolved_duration_seconds"] == 5.0
    assert all(entry["kind"] == "segment" for entry in resolved["sequence"])


def test_lower_third_negative_offset_fails_closed():
    timeline = {
        "schema_version": 1,
        "items": [{
            "stable_id": "location",
            "type": "lower_third",
            "segment_id": "b",
            "offset_seconds": -0.5,
            "duration_seconds": 1,
            "text": "Station",
            "style_id": "lower-third",
            "animation_id": "static",
        }],
    }
    with pytest.raises(VisualCompositionError, match="visual range"):
        resolve_visual_timeline(timeline, _segments(), PROFILE, require_assets=False)


def test_missing_runtime_asset_fails_closed(tmp_path: Path):
    timeline = {
        "schema_version": 1,
        "items": [
            {
                "stable_id": "intro",
                "type": "intro",
                "duration_seconds": 1,
                "text": "Title",
                "style_id": "title-center",
                "animation_id": "static",
                "runtime_assets": [{"path": str(tmp_path / "missing.png")}],
            }
        ],
    }
    with pytest.raises(VisualCompositionError, match="visual asset"):
        resolve_visual_timeline(timeline, _segments(), PROFILE, require_assets=False)


def test_runtime_asset_fingerprint_change_fails_closed(tmp_path: Path):
    asset = tmp_path / "overlay.png"
    asset.write_bytes(b"v1")
    timeline = {
        "schema_version": 1,
        "items": [{
            "stable_id": "intro",
            "type": "intro",
            "duration_seconds": 1,
            "text": "Title",
            "style_id": "title-center",
            "animation_id": "static",
            "runtime_assets": [{"path": str(asset)}],
        }],
    }
    resolved = resolve_visual_timeline(timeline, _segments(), PROFILE, require_assets=False)
    asset.write_bytes(b"v2 changed")
    with pytest.raises(VisualCompositionError, match="fingerprint"):
        resolve_visual_timeline(resolved, _segments(), PROFILE, require_assets=False)


def test_visual_hash_changes_when_text_or_duration_changes():
    base = {"items": [{"stable_id": "intro", "type": "intro", "text": "A", "duration_seconds": 1}]}
    changed = {"items": [{"stable_id": "intro", "type": "intro", "text": "B", "duration_seconds": 1}]}
    assert stable_visual_hash(base) != stable_visual_hash(changed)


def test_visual_card_render_uses_managed_runner(tmp_path: Path):
    timeline = resolve_visual_timeline(
        {"items": [{"stable_id": "intro", "type": "intro", "duration_seconds": 1, "text": "Title", "style_id": "title-center", "animation_id": "fade-in-out"}]},
        _segments(),
        PROFILE,
        require_assets=True,
    )
    calls = []

    class Runner:
        def run(self, command, **kwargs):
            calls.append((command, kwargs))
            Path(command[-1]).write_bytes(b"managed visual")
            return SimpleNamespace(returncode=0, stderr="")

    paths, _, _ = render_visual_cards(
        {**timeline, "sequence": [entry for entry in timeline["sequence"] if entry["kind"] == "visual"]},
        {},
        tmp_path / "cache",
        tmp_path / "work",
        PROFILE,
        "ffmpeg",
        runner=Runner(),
    )
    assert paths[0].is_file()
    assert calls and calls[0][1]["expected_duration_seconds"] == 1.0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required for visual card media test")
def test_render_visual_card_writes_cache_and_report_evidence(tmp_path: Path):
    timeline = resolve_visual_timeline(
        {
            "schema_version": 1,
            "items": [{
                "stable_id": "intro",
                "type": "intro",
                "duration_seconds": 1,
                "text": "Title",
                "style_id": "title-center",
                "animation_id": "static",
            }],
        },
        _segments(),
        PROFILE,
        require_assets=True,
    )
    source = tmp_path / "segment.mp4"
    source.write_bytes(b"not a media file")
    # The card is first in the sequence, so this test isolates card rendering
    # and does not require a synthetic segment fixture.
    paths, evidence, overlays = render_visual_cards(
        {**timeline, "sequence": [entry for entry in timeline["sequence"] if entry["kind"] == "visual"]},
        {},
        tmp_path / "cache",
        tmp_path / "work",
        PROFILE,
        "ffmpeg",
    )
    assert paths and paths[0].is_file()
    assert evidence[0]["cache_hit"] is False
    assert overlays == []
    second_paths, second_evidence, _ = render_visual_cards(
        {**timeline, "sequence": [entry for entry in timeline["sequence"] if entry["kind"] == "visual"]},
        {},
        tmp_path / "cache",
        tmp_path / "work",
        PROFILE,
        "ffmpeg",
    )
    assert second_paths == paths
    assert second_evidence[0]["cache_hit"] is True

    paths[0].write_bytes(b"tampered")
    _, third_evidence, _ = render_visual_cards(
        {**timeline, "sequence": [entry for entry in timeline["sequence"] if entry["kind"] == "visual"]},
        {},
        tmp_path / "cache",
        tmp_path / "work",
        PROFILE,
        "ffmpeg",
    )
    assert third_evidence[0]["cache_hit"] is False

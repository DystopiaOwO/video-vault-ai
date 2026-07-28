from pathlib import Path

import pytest

from video_vault.timeline_assembler import build_timeline_command
from video_vault.visual_renderer import (
    VisualRenderError,
    cleanup_visual_filter,
    prepare_visual_filter,
)
from video_vault.visual_timeline import (
    reconcile_visual_timeline_with_segments,
    resolve_visual_runtime_assets,
    validate_visual_timeline,
)


def _font() -> Path:
    candidates = [
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
    ]
    return next(path.resolve() for path in candidates if path.is_file())


def _item(font: Path) -> dict:
    return {
        "stable_id": "chapter-card-001",
        "type": "chapter_card",
        "start_seconds": 0,
        "duration_seconds": 1,
        "text": "Travel 'Day 1' / 中文",
        "style_id": "location-lower-left",
        "style_version": 1,
        "animation_id": "fade-in-out",
        "font": {"family": "system-ui", "weight": 600},
        "runtime_assets": [{"kind": "font", "path": str(font)}],
    }


def test_runtime_font_is_pinned_into_manifest_timeline():
    font = _font()
    timeline = {
        "schema_version": 1,
        "contract_version": "visual-timeline-v1",
        "items": [{**_item(font), "runtime_assets": []}],
    }
    resolved = resolve_visual_runtime_assets(
        timeline,
        {"render": {"visual_font_path": str(font)}},
    )
    assert resolved["items"][0]["runtime_assets"] == [{
        "kind": "font",
        "path": str(font),
        "asset_id": f"font:{font.name}",
    }]
    assert timeline["items"][0]["runtime_assets"] == []


def test_visual_filter_uses_textfile_and_cleans_transient_text(tmp_path):
    manifest = {
        "segments": [{"timeline_duration_seconds": 2}],
        "visual_items": [_item(_font())],
    }
    prepared = prepare_visual_filter(manifest, tmp_path)
    assert prepared is not None
    assert "drawtext=" in prepared.expression
    assert "textfile=" in prepared.expression
    assert "enable='between" in prepared.expression
    assert prepared.text_files[0].read_text(encoding="utf-8") == "Travel 'Day 1' / 中文"
    cleanup_visual_filter(prepared)
    assert not prepared.text_files[0].exists()


def test_visual_item_cannot_extend_beyond_approved_media(tmp_path):
    item = _item(_font())
    item["start_seconds"] = 1.5
    with pytest.raises(VisualRenderError, match="exceeds"):
        prepare_visual_filter(
            {
                "segments": [{"timeline_duration_seconds": 2}],
                "visual_items": [item],
            },
            tmp_path,
        )


def test_unknown_style_is_rejected_before_approval():
    item = _item(_font())
    item["style_id"] = "unversioned-style"
    result = validate_visual_timeline({"schema_version": 1, "items": [item]})
    assert result["valid"] is False
    assert "unsupported style_id" in result["errors"][0]


def test_chapter_cards_follow_included_storyboard_groups_and_titles():
    first = _item(_font())
    second = {**_item(_font()), "stable_id": "chapter-card-002", "group_id": "old-2"}
    timeline = reconcile_visual_timeline_with_segments(
        {"schema_version": 1, "items": [first, second]},
        [{
            "group": "抵達車站",
            "group_id": "storyboard-group-1",
            "timeline_duration_seconds": 2.0,
        }],
    )

    assert len(timeline["items"]) == 1
    assert timeline["items"][0]["group_id"] == "storyboard-group-1"
    assert timeline["items"][0]["text"] == "抵達車站"


def test_timeline_with_visual_filter_reencodes_video(tmp_path):
    command = build_timeline_command(
        "ffmpeg",
        tmp_path / "timeline.ffconcat",
        tmp_path / "out.mp4",
        profile={"pixel_format": "yuv420p"},
        video_filter="drawtext=...",
        encoder_contract={
            "implementation": "libx264",
            "h264_profile": "high",
            "h264_level": "4.2",
            "gop": 60,
            "bf": 2,
            "preset": "medium",
            "rate_control": "crf",
            "quality": 23,
        },
    )
    assert command[command.index("-vf") + 1] == "drawtext=..."
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-c:a") + 1] == "copy"

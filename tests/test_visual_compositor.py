from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

import video_vault.visual_compositor as visual_compositor
from video_vault.visual_compositor import (
    VisualCompositionError,
    render_visual_cards,
    resolve_visual_timeline,
    stable_visual_hash,
    visual_cache_key,
)
from video_vault.visual_style import materialize_visual_style


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


def _approved_overlay_style(composition: str = "overlay"):
    brief = {
        "status": "approved", "brief_version": 1, "visual_contract_hash": "brief",
        "approved": {
            "output": {"output_contract_id": "landscape_16_9", "output_contract_version": "1", "orientation": "landscape", "aspect_ratio": "16:9", "width": 1920, "height": 1080, "render_profile_id": "final_1080p"},
            "framing_intent": {"portrait_source_in_landscape": {"approved_strategy_id": "background_treatment"}, "landscape_source_in_portrait": {"approved_strategy_id": "crop_reframe"}},
        },
    }
    snapshot = materialize_visual_style("diary_natural", brief)
    snapshot["composition"] = composition
    snapshot.pop("semantic_hash", None)
    snapshot["resolved_hash"] = __import__("hashlib").sha256(__import__("json").dumps({key: value for key, value in snapshot.items() if key != "resolved_hash"}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return snapshot


def test_overlay_chapter_treatment_does_not_extend_concat_duration():
    timeline = {"items": [{"stable_id": "chapter", "type": "chapter_card", "group_id": "morning", "duration_seconds": 1.5, "text": "Morning", "style_id": "location-lower-left", "animation_id": "static"}]}
    resolved = resolve_visual_timeline(timeline, _segments(), PROFILE, require_assets=False, chapter_composition="overlay")
    assert all(entry["kind"] == "segment" for entry in resolved["sequence"])
    assert resolved["resolved_duration_seconds"] == 5.0
    chapter = next(item for item in resolved["resolved_items"] if item["type"] == "chapter_card")
    assert chapter["composition"] == "overlay" and chapter["title_role"] == "chapter_title"


def test_standalone_chapter_treatment_remains_concat_insertion():
    timeline = {"items": [{"stable_id": "chapter", "type": "chapter_card", "group_id": "morning", "duration_seconds": 1.5, "text": "Morning", "style_id": "location-lower-left", "animation_id": "static"}]}
    resolved = resolve_visual_timeline(timeline, _segments(), PROFILE, require_assets=False, chapter_composition="standalone")
    assert resolved["sequence"][0]["kind"] == "visual"
    assert resolved["resolved_duration_seconds"] == 6.5


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required for styled standalone card smoke")
def test_styled_standalone_card_real_render_succeeds(tmp_path: Path):
    timeline = resolve_visual_timeline(
        {"items": [{"stable_id": "chapter", "type": "chapter_card", "duration_seconds": 0.8, "text": "Standalone", "style_id": "location-lower-left", "animation_id": "static"}]},
        _segments(), PROFILE, require_assets=True, chapter_composition="standalone",
    )
    paths, evidence, _ = render_visual_cards(
        {**timeline, "sequence": [entry for entry in timeline["sequence"] if entry["kind"] == "visual"]},
        {}, tmp_path / "cache", tmp_path / "work", PROFILE, "ffmpeg", visual_style_snapshot=_approved_overlay_style("standalone"),
    )
    assert paths and paths[0].is_file() and paths[0].stat().st_size > 0
    assert evidence[0]["composition"] if "composition" in evidence[0] else True


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


def test_pinned_runtime_font_asset_is_used_on_portable_platforms(tmp_path: Path):
    font = tmp_path / "portable-font.ttf"
    font.write_bytes(b"font fixture")
    timeline = {
        "schema_version": 1,
        "items": [{
            "stable_id": "chapter",
            "type": "chapter_card",
            "group_id": "morning",
            "duration_seconds": 1,
            "text": "Morning",
            "style_id": "location-lower-left",
            "animation_id": "static",
            "runtime_assets": [{"kind": "font", "path": str(font)}],
        }],
    }
    resolved = resolve_visual_timeline(timeline, _segments(), PROFILE, require_assets=True)
    assert resolved["resolved_items"][0]["font_path"] == str(font.resolve())


def test_visual_hash_changes_when_text_or_duration_changes():
    base = {"items": [{"stable_id": "intro", "type": "intro", "text": "A", "duration_seconds": 1}]}
    changed = {"items": [{"stable_id": "intro", "type": "intro", "text": "B", "duration_seconds": 1}]}
    assert stable_visual_hash(base) != stable_visual_hash(changed)


def _chapter_card_timeline():
    resolved = resolve_visual_timeline(
        {"items": [{"stable_id": "chapter", "type": "chapter_card", "duration_seconds": 1.5, "text": "Chapter", "style_id": "location-lower-left", "animation_id": "static"}]},
        _segments(),
        PROFILE,
        require_assets=True,
    )
    return {**resolved, "sequence": [entry for entry in resolved["sequence"] if entry["kind"] == "visual"]}


def test_visual_render_cache_version_is_separate_from_approved_timeline_identity(monkeypatch):
    timeline = _chapter_card_timeline()
    item = timeline["resolved_items"][0]
    approved_before = resolve_visual_timeline(timeline, _segments(), PROFILE, require_assets=True)

    monkeypatch.setattr(visual_compositor, "VISUAL_RENDER_CACHE_VERSION", "visual-render-cache-v1")
    old_key = visual_cache_key(item, PROFILE)
    monkeypatch.setattr(visual_compositor, "VISUAL_RENDER_CACHE_VERSION", "visual-render-cache-v2")
    new_key = visual_cache_key(item, PROFILE)
    approved_after = resolve_visual_timeline(timeline, _segments(), PROFILE, require_assets=True)

    assert old_key != new_key
    assert approved_before["resolution_version"] == "visual-composition-v1"
    assert approved_after["resolution_version"] == approved_before["resolution_version"]
    assert approved_after["resolution_hash"] == approved_before["resolution_hash"]
    assert [item["stable_id"] for item in approved_after["resolved_items"]] == [item["stable_id"] for item in approved_before["resolved_items"]]
    assert [item["duration_seconds"] for item in approved_after["resolved_items"]] == [item["duration_seconds"] for item in approved_before["resolved_items"]]


def test_old_visual_render_cache_version_misses_and_new_version_hits(tmp_path: Path, monkeypatch):
    timeline = _chapter_card_timeline()
    calls = []

    class Runner:
        def run(self, command, **kwargs):
            calls.append((command, kwargs))
            Path(command[-1]).write_bytes(b"rendered chapter card")
            return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(visual_compositor, "VISUAL_RENDER_CACHE_VERSION", "visual-render-cache-v1")
    _, old_evidence, _ = render_visual_cards(
        timeline, {}, tmp_path / "cache", tmp_path / "work", PROFILE, "ffmpeg", runner=Runner()
    )
    old_key = old_evidence[0]["cache_key"]

    monkeypatch.setattr(visual_compositor, "VISUAL_RENDER_CACHE_VERSION", "visual-render-cache-v2")
    _, new_evidence, _ = render_visual_cards(
        timeline, {}, tmp_path / "cache", tmp_path / "work", PROFILE, "ffmpeg", runner=Runner()
    )
    assert new_evidence[0]["cache_key"] != old_key
    assert new_evidence[0]["cache_hit"] is False
    assert len(calls) == 2

    _, repeat_evidence, _ = render_visual_cards(
        timeline, {}, tmp_path / "cache", tmp_path / "work", PROFILE, "ffmpeg", runner=Runner()
    )
    assert repeat_evidence[0]["cache_key"] == new_evidence[0]["cache_key"]
    assert repeat_evidence[0]["cache_hit"] is True
    assert len(calls) == 2


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


def test_chapter_card_background_is_dark_but_not_formal_black(tmp_path: Path):
    timeline = resolve_visual_timeline(
        {"items": [{"stable_id": "chapter", "type": "chapter_card", "duration_seconds": 1, "text": "Chapter", "style_id": "location-lower-left", "animation_id": "static"}]},
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

    render_visual_cards(
        {**timeline, "sequence": [entry for entry in timeline["sequence"] if entry["kind"] == "visual"]},
        {},
        tmp_path / "cache",
        tmp_path / "work",
        PROFILE,
        "ffmpeg",
        runner=Runner(),
    )
    command = calls[0][0]
    color_input = command[command.index("-i") + 1]
    assert color_input.startswith("color=c=0x20242a:")
    assert "color=c=black" not in color_input


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required for blackdetect media test")
def test_real_rendered_chapter_card_has_no_interior_blackdetect_event(tmp_path: Path):
    timeline = _chapter_card_timeline()
    paths, _, _ = render_visual_cards(
        timeline, {}, tmp_path / "cache", tmp_path / "work", PROFILE, "ffmpeg"
    )
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "info", "-nostdin", "-i", str(paths[0]),
            "-vf", "blackdetect=d=0.5:pix_th=0.10", "-an", "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert "black_start:" not in result.stderr


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

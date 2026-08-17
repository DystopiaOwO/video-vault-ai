from pathlib import Path
from types import SimpleNamespace

import pytest

from video_vault.database import init_db
from video_vault.visual_style import (
    TITLE_STYLES,
    VISUAL_STYLES,
    TitleStyleRegistry,
    VisualStyleError,
    VisualStyleRegistry,
    build_preview_filter,
    ensure_visual_style_state,
    materialize_visual_style,
    render_true_frame_preview,
    validate_materialized_visual_style,
    visual_style_options,
)
from video_vault.visual_compositor import visual_cache_key


def _brief(orientation="landscape"):
    return {
        "status": "approved",
        "brief_version": 3,
        "visual_contract_hash": "brief-hash",
        "approved": {
            "output": {
                "output_contract_id": "landscape_16_9" if orientation == "landscape" else "portrait_9_16",
                "output_contract_version": "1",
                "orientation": orientation,
                "aspect_ratio": "16:9" if orientation == "landscape" else "9:16",
                "width": 1920 if orientation == "landscape" else 1080,
                "height": 1080 if orientation == "landscape" else 1920,
                "render_profile_id": "final_1080p" if orientation == "landscape" else "final_1080p_portrait",
            },
            "framing_intent": {
                "portrait_source_in_landscape": {"approved_strategy_id": "background_treatment", "approved_strategy_version": "1"},
                "landscape_source_in_portrait": {"approved_strategy_id": "crop_reframe", "approved_strategy_version": "1"},
            },
        },
    }


def test_round1_styles_are_distinct_and_registry_lists_only_public_variants():
    options = visual_style_options()
    assert {item["style_id"] for item in options["styles"]} == {"diary_natural", "clean_minimal", "cinematic"}
    snapshots = [materialize_visual_style(style_id, _brief()) for style_id in sorted({"diary_natural", "clean_minimal", "cinematic"})]
    assert len({item["resolved_hash"] for item in snapshots}) == 3
    assert {item["grading"]["look_id"] for item in snapshots} == {"diary-warm-neutral", "clean-neutral", "cinematic-teal-gold"}


def test_synthetic_style_and_title_use_common_resolvers_without_special_case():
    styles = VisualStyleRegistry(VISUAL_STYLES.list())
    titles = TitleStyleRegistry(TITLE_STYLES.list())
    styles.register("synthetic_panel", {**styles.resolve("test_soft_panel"), "style_id": "synthetic_panel", "label": "Synthetic Panel"})
    titles.register("synthetic_title", {**titles.resolve("test_soft_panel"), "title_style_id": "synthetic_title", "label": "Synthetic Title"})
    snapshot = materialize_visual_style("synthetic_panel", _brief(), title_style_id="synthetic_title", registry=styles, title_registry=titles)
    assert snapshot["visual_style_id"] == "synthetic_panel"
    assert snapshot["title_style"]["title_style_id"] == "synthetic_title"
    assert snapshot["registry_hash"] == styles.hash()
    with pytest.raises(VisualStyleError, match="unknown visual style"):
        materialize_visual_style("does-not-exist", _brief())


def test_materialized_snapshot_is_immutable_against_registry_default_change():
    snapshot = materialize_visual_style("diary_natural", _brief())
    old_hash = snapshot["resolved_hash"]
    updated = VisualStyleRegistry(VISUAL_STYLES.list())
    changed = updated.resolve("diary_natural")
    changed["label"] = "Changed default"
    updated.register("diary_natural", changed)
    assert snapshot["resolved_hash"] == old_hash
    assert snapshot["label"] == "Diary Natural"


def test_unknown_framing_and_title_role_fail_closed():
    brief = _brief()
    brief["approved"]["framing_intent"]["portrait_source_in_landscape"]["approved_strategy_id"] = "future_magic"
    with pytest.raises(VisualStyleError, match="unknown approved framing"):
        materialize_visual_style("diary_natural", brief)
    with pytest.raises(VisualStyleError, match="does not support role"):
        materialize_visual_style("diary_natural", _brief(), title_role="unknown_role")


def test_preview_filter_has_display_safe_framing_actual_grading_and_title():
    snapshot = materialize_visual_style("cinematic", _brief())
    graph = build_preview_filter(snapshot, width=1920, height=1080, title_text="咖啡日記 / Coffee Diary")
    assert "force_original_aspect_ratio" in graph
    assert "setsar=1" in graph
    assert "eq=brightness=" in graph
    assert "drawtext=" in graph


def test_visual_style_changes_render_artifact_cache_identity_not_approval_contract():
    item = {"stable_id": "card-1", "type": "chapter_card", "text": "Coffee"}
    profile = {"profile_id": "test", "width": 1920, "height": 1080, "fps": 30, "video_codec": "h264", "pixel_format": "yuv420p", "audio_codec": "aac", "audio_sample_rate": 48000, "audio_channels": 2}
    assert visual_cache_key(item, profile, "style-a") != visual_cache_key(item, profile, "style-b")


def test_true_frame_preview_records_resolved_contract_and_does_not_need_real_ffmpeg(tmp_path: Path):
    output = tmp_path / "frame.png"
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    snapshot = materialize_visual_style("clean_minimal", _brief())

    def runner(command):
        output.write_bytes(b"rendered-frame")
        return SimpleNamespace(returncode=0, stderr="")

    result = render_true_frame_preview({"ffmpeg_path": "ffmpeg"}, 1, source, 0.5, snapshot, output, runner=runner)
    assert result["width"] == 1920
    assert result["height"] == 1080
    assert result["visual_style_hash"] == snapshot["resolved_hash"]
    assert result["title_text"]


def test_unapproved_project_state_is_needs_confirmation(tmp_path: Path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    state = ensure_visual_style_state({"library_root": str(tmp_path)}, db, 99)
    assert state["status"] == "needs_confirmation"
    assert state["approved"] == {}
    assert state["recommendation"]["visual_style_id"] == "diary_natural"

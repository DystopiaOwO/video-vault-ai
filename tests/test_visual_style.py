from pathlib import Path
import shutil
import subprocess
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
    resolve_visual_render_plan,
    _refresh_visual_style_currentity,
    _select_representative_frames,
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


def test_shared_render_plan_changes_pixel_contract_for_framing_and_title_tokens():
    snapshot = materialize_visual_style("diary_natural", _brief())
    first = resolve_visual_render_plan(snapshot, width=1920, height=1080, title_text="A")
    changed = dict(snapshot)
    changed["framing"] = {**snapshot["framing"], "strategy_id": "crop_reframe"}
    changed["title_style"] = {**snapshot["title_style"], "responsive": {**snapshot["title_style"]["responsive"], "landscape": {"anchor": "top-left", "size_ratio": 0.08}}}
    changed["resolved_hash"] = __import__("hashlib").sha256(__import__("json").dumps({key: value for key, value in changed.items() if key != "resolved_hash"}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    second = resolve_visual_render_plan(changed, width=1920, height=1080, title_text="B")
    assert first["resolved_hash"] != second["resolved_hash"]
    assert first["framing"]["strategy_id"] != second["framing"]["strategy_id"]
    assert "x=w*0.050000" in first["title"]["filter"]
    assert "y=h*0.060000" in second["title"]["filter"]


def test_title_motion_and_roles_are_real_resolved_semantics():
    snapshot = materialize_visual_style("cinematic", _brief())
    none_style = dict(snapshot)
    none_title = dict(snapshot["title_style"])
    none_title["motion"] = {**none_title["motion"], "preset": "none"}
    none_style["title_style"] = none_title
    none_style["resolved_hash"] = __import__("hashlib").sha256(__import__("json").dumps({key: value for key, value in none_style.items() if key != "resolved_hash"}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    rise = resolve_visual_render_plan(snapshot, width=1920, height=1080, title_text="A", title_role="chapter_title")
    lower = resolve_visual_render_plan(snapshot, width=1920, height=1080, title_text="A", title_role="lower_third")
    none = resolve_visual_render_plan(none_style, width=1920, height=1080, title_text="A")
    assert "min(t/0.280000" in rise["title"]["filter"]
    assert "((1-min(t/0.280000" in rise["title"]["filter"]
    assert rise["title"]["role"] == "chapter_title"
    assert lower["title"]["role"] == "lower_third"
    assert lower["title"]["max_width_pixels"] < rise["title"]["max_width_pixels"]
    assert "alpha=" not in none["title"]["filter"]


def test_background_treatment_is_not_preserve_full_frame():
    background = materialize_visual_style("diary_natural", _brief())
    preserve_brief = _brief()
    preserve_brief["approved"]["framing_intent"]["portrait_source_in_landscape"]["approved_strategy_id"] = "preserve_full_frame"
    preserve = materialize_visual_style("diary_natural", preserve_brief)
    assert resolve_visual_render_plan(background, width=1920, height=1080)["framing"]["filter"] != resolve_visual_render_plan(preserve, width=1920, height=1080)["framing"]["filter"]


def test_framing_policy_is_resolved_per_source_orientation():
    snapshot = materialize_visual_style("diary_natural", _brief("landscape"))
    landscape = resolve_visual_render_plan(snapshot, width=1920, height=1080, source_geometry={"display_ratio": 16 / 9, "source_orientation": "landscape", "sample_aspect_ratio": "1:1"})
    portrait = resolve_visual_render_plan(snapshot, width=1920, height=1080, source_geometry={"display_ratio": 9 / 16, "source_orientation": "portrait", "sample_aspect_ratio": "1:1"})
    assert landscape["framing"]["direction_id"] == "same_orientation"
    assert landscape["framing"]["strategy_id"] == "preserve_full_frame"
    assert portrait["framing"]["direction_id"] == "portrait_source_in_landscape"
    assert portrait["framing"]["strategy_id"] == "background_treatment"
    assert landscape["resolved_hash"] != portrait["resolved_hash"]


def test_visual_plan_preserves_rotated_non_square_sar_before_framing():
    snapshot = materialize_visual_style("diary_natural", _brief("landscape"))
    plan = resolve_visual_render_plan(
        snapshot,
        width=1920,
        height=1080,
        source_geometry={
            "display_ratio": 9 / 16,
            "source_orientation": "portrait",
            "sample_aspect_ratio": "4:3",
            "rotation_degrees": 90,
            "display_matrix": "synthetic-display-matrix",
        },
    )
    assert "scale=ceil(iw*3/4/2)*2:ih" in plan["filter_complex"]
    assert plan["source_geometry"]["display_matrix"] == "synthetic-display-matrix"
    assert plan["framing"]["direction_id"] == "portrait_source_in_landscape"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required for visual graph smoke")
def test_real_background_graph_keeps_foreground_aspect_safe(tmp_path: Path):
    source = tmp_path / "portrait.mp4"
    output = tmp_path / "preview.png"
    generated = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "lavfi", "-i", "testsrc=size=64x128:rate=2", "-t", "0.5", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert generated.returncode == 0, generated.stderr
    snapshot = materialize_visual_style("diary_natural", _brief("landscape"))
    result = render_true_frame_preview(
        {"ffmpeg_path": "ffmpeg"}, 1, source, 0.0, snapshot, output,
        source_geometry={"display_ratio": 0.5, "source_orientation": "portrait", "sample_aspect_ratio": "1:1"},
    )
    assert output.is_file() and output.stat().st_size > 0
    assert result["visual_render_plan"]["graph_type"] == "split_background_overlay"
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height,sample_aspect_ratio,display_aspect_ratio", "-of", "json", str(output)], capture_output=True, text=True, encoding="utf-8", check=False)
    assert probe.returncode == 0
    assert '"width": 1920' in probe.stdout
    assert '"height": 1080' in probe.stdout


def test_dji_lut_missing_resource_fails_closed(tmp_path: Path):
    snapshot = materialize_visual_style("cinematic", _brief())
    with pytest.raises(Exception, match="LUT file does not exist"):
        resolve_visual_render_plan(snapshot, width=1920, height=1080, color_settings={"mode": "dji_dlog_m", "lut_path": str(tmp_path / "missing.cube")})


def test_dji_lut_uses_existing_color_pipeline_as_separate_technical_transform(tmp_path: Path):
    lut = tmp_path / "coffee.cube"
    lut.write_text("LUT_3D_SIZE 2\n", encoding="utf-8")
    snapshot = materialize_visual_style("cinematic", _brief())
    plan = resolve_visual_render_plan(snapshot, width=1920, height=1080, color_settings={"mode": "dji_dlog_m", "lut_path": str(lut), "source_colorspace": "dji_dlog_m"})
    assert plan["technical_transform"]["mode"] == "dji_dlog_m"
    assert plan["technical_transform"]["applied_once"] is True
    assert "lut3d=file=" in plan["color_filter"]
    assert plan["creative_look"]["contrast"] != 1.0


def test_representative_selector_uses_real_media_candidates_and_returns_bright_dark_pair(monkeypatch, tmp_path: Path):
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"real-media-a")
    second.write_bytes(b"real-media-b")
    levels = iter([0.15, 0.85, 0.42, 0.55])
    monkeypatch.setattr("video_vault.visual_style._measure_source_luma", lambda *args: next(levels))
    sources = [
        {"project_media_uuid": "pm-a", "path": str(first), "duration_seconds": 10, "display_geometry": {"display_ratio": 1.78}},
        {"project_media_uuid": "pm-b", "path": str(second), "duration_seconds": 10, "display_geometry": {"display_ratio": 1.78}},
    ]
    frames = _select_representative_frames({"ffmpeg_path": "ffmpeg"}, sources)
    assert len(frames) == 2
    assert {item["selection_reason"] for item in frames} == {"bright_high_luma_representative", "dark_complex_low_luma_representative"}
    assert all(item["source"]["project_media_uuid"] in {"pm-a", "pm-b"} for item in frames)


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


def test_creative_brief_visual_hash_change_stales_approved_style_but_recommendation_refresh_does_not(tmp_path: Path, monkeypatch):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    approved = {"technical_transform": {"mode": "none", "requires_lut": False, "strategy": "user_managed", "version": "1", "source_colorspace": "unknown", "lut_identity": {}, "applied_once": False}}
    state = {"status": "approved", "approved": approved, "creative_brief_hash": "brief-v1", "project_id": 7}
    monkeypatch.setattr("video_vault.visual_style._load_brief", lambda *_args: {"status": "approved", "visual_contract_hash": "brief-v1"})
    current = _refresh_visual_style_currentity({"library_root": str(tmp_path)}, db, 7, state)
    assert current["status"] == "approved"
    monkeypatch.setattr("video_vault.visual_style._load_brief", lambda *_args: {"status": "approved", "visual_contract_hash": "brief-v2"})
    stale = _refresh_visual_style_currentity({"library_root": str(tmp_path)}, db, 7, state)
    assert stale["status"] == "stale"
    assert stale["stale_reason"] == "creative_brief_visual_contract_changed"

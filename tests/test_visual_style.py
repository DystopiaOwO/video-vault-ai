from pathlib import Path
import shutil
import subprocess
from copy import deepcopy
from types import SimpleNamespace

import pytest

from video_vault.database import init_db
from video_vault.project_lifecycle import ProjectRevisionConflict
from video_vault.visual_style import (
    CHAPTER_TITLE_SIZE_SCALE,
    TITLE_ANCHORS,
    TITLE_LAYOUT_CONTRACT_VERSION,
    TITLE_MOTION_PRESETS,
    TITLE_STYLES,
    PREVIEW_TITLE_SAMPLE_SECONDS,
    VISUAL_STYLES,
    TitleStyleRegistry,
    VisualStyleError,
    VisualStyleRegistry,
    build_preview_filter,
    ensure_visual_style_state,
    materialize_visual_style,
    render_animated_title_preview,
    render_true_frame_preview,
    resolve_visual_render_plan,
    _refresh_visual_style_currentity,
    _preview_seek_args,
    _select_representative_frames,
    _validate_preview_evidence,
    _wrap_title_text,
    validate_materialized_visual_style,
    visual_style_control_defaults,
    visual_style_options,
    visual_style_api_payload,
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


def _fake_font_identity(*_args, **_kwargs):
    return {
        "path": Path("synthetic-test-font.ttf"),
        "resolved_family": "Synthetic Test Font",
        "resolved_weight": 500,
        "fallback_index": 0,
        "reason": "test_fixture",
        "sha256": "synthetic-test-font",
        "coverage_checked": True,
        "coverage_contract": "font-cmap-v1",
    }


def test_round1_styles_are_distinct_and_registry_lists_only_public_variants():
    options = visual_style_options()
    assert {item["style_id"] for item in options["styles"]} == {"diary_natural", "clean_minimal", "cinematic", "standalone_card_compare"}
    snapshots = [materialize_visual_style(style_id, _brief()) for style_id in sorted({"diary_natural", "clean_minimal", "cinematic"})]
    assert len({item["resolved_hash"] for item in snapshots}) == 3
    assert {item["grading"]["look_id"] for item in snapshots} == {"diary-warm-neutral", "clean-neutral", "cinematic-teal-gold"}


def test_title_wrapper_keeps_latin_words_intact_when_display_width_allows():
    wrapped, line_count = _wrap_title_text("咖啡日記 / Coffee Diary", 842, 67)
    assert wrapped == "咖啡日記 /\nCoffee Diary"
    assert line_count == 2


def test_chapter_title_refinement_scales_shared_layout_for_portrait_and_landscape():
    expected = {
        "portrait": {"old_ratio": 0.046, "width": 1080, "height": 1920},
        "landscape": {"old_ratio": 0.052, "width": 1920, "height": 1080},
    }
    for orientation, values in expected.items():
        snapshot = materialize_visual_style("diary_natural", _brief(orientation))
        title = snapshot["title_style"]
        responsive = title["responsive"]
        assert title["layout_contract_version"] == TITLE_LAYOUT_CONTRACT_VERSION
        assert title["surface_padding"] == 14
        assert title["line_height"] == 1.12
        assert title["line_spacing_ratio"] == 0.12
        assert responsive["size_ratio"] == pytest.approx(values["old_ratio"] * CHAPTER_TITLE_SIZE_SCALE)

        plan = resolve_visual_render_plan(snapshot, width=values["width"], height=values["height"], title_text="咖啡日記 / Coffee Diary")
        assert plan["title"]["font_size"] == int(values["height"] * responsive["size_ratio"])
        assert plan["title"]["surface_padding"] == 14
        assert plan["title"]["line_spacing_pixels"] < plan["title"]["font_size"] * 0.18
        assert plan["title"]["bbox"]["in_frame"] is True
        assert plan["title"]["wrapped_text"] == ("咖啡日記 /\nCoffee Diary" if orientation == "portrait" else "咖啡日記 / Coffee Diary")
        assert "Coffee Diary" in plan["title"]["wrapped_text"]

        legacy = deepcopy(snapshot)
        legacy_title = legacy["title_style"]
        legacy_title["responsive"] = {**legacy_title["responsive"], "size_ratio": values["old_ratio"]}
        legacy_title["line_height"] = 1.18
        legacy_title.pop("line_spacing_ratio", None)
        legacy_title["surface_padding"] = 18
        legacy_title["layout_contract_version"] = "title-layout-v1"
        legacy_hash = __import__("hashlib").sha256(__import__("json").dumps({key: value for key, value in legacy.items() if key not in {"resolved_hash", "semantic_hash"}}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        legacy["semantic_hash"] = legacy_hash
        legacy["resolved_hash"] = legacy_hash
        legacy_plan = resolve_visual_render_plan(legacy, width=values["width"], height=values["height"], title_text="咖啡日記 / Coffee Diary")
        assert plan["title"]["font_size"] / legacy_plan["title"]["font_size"] == pytest.approx(CHAPTER_TITLE_SIZE_SCALE, abs=0.02)
        assert plan["title"]["bbox"]["height"] < legacy_plan["title"]["bbox"]["height"]
        assert plan["semantic_hash"] != legacy_plan["semantic_hash"]


def test_chapter_title_refinement_changes_preview_plan_identity_without_special_case():
    portrait = materialize_visual_style("diary_natural", _brief("portrait"))
    landscape = materialize_visual_style("diary_natural", _brief("landscape"))
    portrait_plan = resolve_visual_render_plan(portrait, width=1080, height=1920, title_text="咖啡日記 / Coffee Diary")
    landscape_plan = resolve_visual_render_plan(landscape, width=1920, height=1080, title_text="咖啡日記 / Coffee Diary")
    assert portrait["title_style"]["layout_contract_version"] == landscape["title_style"]["layout_contract_version"]
    assert portrait_plan["semantic_hash"] != landscape_plan["semantic_hash"]
    assert portrait_plan["title"]["layout_contract_version"] == TITLE_LAYOUT_CONTRACT_VERSION
    assert landscape_plan["title"]["layout_contract_version"] == TITLE_LAYOUT_CONTRACT_VERSION


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
    graph = build_preview_filter(snapshot, width=1920, height=1080, title_text="Coffee Diary")
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
    changed.pop("semantic_hash", None)
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
    none_style.pop("semantic_hash", None)
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


def test_static_preview_uses_a_deterministic_visible_sample_without_changing_formal_clock():
    snapshot = materialize_visual_style("diary_natural", _brief())
    formal = resolve_visual_render_plan(snapshot, width=1920, height=1080, title_text="Coffee")
    preview = resolve_visual_render_plan(snapshot, width=1920, height=1080, title_text="Coffee", title_time_offset_seconds=PREVIEW_TITLE_SAMPLE_SECONDS)
    assert "min(t/0.280000" in formal["title"]["filter"]
    assert "0.350000" in preview["title"]["filter"]
    assert formal["title"]["filter"] != preview["title"]["filter"]
    assert preview["title"]["bbox"]["in_frame"] is True


def test_title_font_resolution_fails_closed_without_required_glyph_coverage(monkeypatch):
    monkeypatch.setattr("video_vault.visual_style._font_supports_text", lambda *_args, **_kwargs: False)
    snapshot = materialize_visual_style("diary_natural", _brief())
    with pytest.raises(VisualStyleError, match="title font"):
        resolve_visual_render_plan(snapshot, width=1920, height=1080, title_text="咖啡日記")


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


def test_true_frame_preview_records_resolved_contract_and_does_not_need_real_ffmpeg(tmp_path: Path, monkeypatch):
    output = tmp_path / "frame.png"
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    snapshot = materialize_visual_style("clean_minimal", _brief())
    monkeypatch.setattr("video_vault.visual_style._resolve_font", _fake_font_identity)

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


def test_preview_evidence_rejects_arbitrary_plan_hash_and_stale_variants(tmp_path: Path, monkeypatch):
    from video_vault.visual_style import _technical_transform_snapshot

    preview = tmp_path / "preview.png"
    preview.write_bytes(b"preview-pixels")
    brief = _brief()
    snapshot = materialize_visual_style("diary_natural", brief)
    evidence = {
        "preview_plan_hash": "plan-current",
        "visual_style_id": snapshot["visual_style_id"],
        "visual_style_version": snapshot["visual_style_version"],
        "visual_style_hash": snapshot["resolved_hash"],
        "title_style_identity": __import__("hashlib").sha256(__import__("json").dumps(snapshot["title_style"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "creative_brief_hash": brief["visual_contract_hash"],
        "technical_transform": _technical_transform_snapshot({}),
        "source_media_uuid": "media-1",
        "source_fingerprint": {"sha256": "source-sha"},
        "preview_filename": preview.name,
        "preview_image_sha256": __import__("hashlib").sha256(preview.read_bytes()).hexdigest(),
        "title_render_evidence": {"version": "title-pixel-evidence-v1", "status": "pass", "changed_pixels": 12, "in_frame": True, "bbox": {"x": 10, "y": 10, "width": 100, "height": 40}},
        "generated_at": "2026-08-18T00:00:00+00:00",
    }
    monkeypatch.setattr("video_vault.visual_style.visual_style_preview_path", lambda *_args: preview)
    monkeypatch.setattr("video_vault.visual_style._source_provenance", lambda *_args: [{"project_media_uuid": "media-1", "fingerprint": {"sha256": "source-sha"}}])
    _validate_preview_evidence({"ffmpeg_path": "ffmpeg"}, tmp_path / "db.sqlite3", 1, evidence, snapshot, brief, {}, "plan-current")
    with pytest.raises(VisualStyleError, match="preview plan hash"):
        _validate_preview_evidence({"ffmpeg_path": "ffmpeg"}, tmp_path / "db.sqlite3", 1, evidence, snapshot, brief, {}, "arbitrary-plan")
    with pytest.raises(VisualStyleError, match="visual style"):
        _validate_preview_evidence({"ffmpeg_path": "ffmpeg"}, tmp_path / "db.sqlite3", 1, {**evidence, "visual_style_id": "clean_minimal"}, snapshot, brief, {}, "plan-current")
    with pytest.raises(VisualStyleError, match="Creative Brief"):
        _validate_preview_evidence({"ffmpeg_path": "ffmpeg"}, tmp_path / "db.sqlite3", 1, evidence, snapshot, {**brief, "visual_contract_hash": "old-brief"}, {}, "plan-current")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required for title motion smoke")
def test_title_anchors_wrap_and_supported_motion_real_ffmpeg(tmp_path: Path):
    source = tmp_path / "title-source.mp4"
    generated = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10", "-t", "0.8", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)], capture_output=True, text=True, encoding="utf-8", check=False)
    assert generated.returncode == 0, generated.stderr
    base = materialize_visual_style("diary_natural", _brief())
    plans = []
    for preset in ("none", "fade", "fade_rise", "slide_fade"):
        snapshot = deepcopy(base)
        title = deepcopy(snapshot["title_style"])
        title["motion"] = {**title["motion"], "preset": preset}
        snapshot["title_style"] = title
        snapshot.pop("semantic_hash", None)
        snapshot["resolved_hash"] = __import__("hashlib").sha256(__import__("json").dumps({key: value for key, value in snapshot.items() if key != "resolved_hash"}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        plan = resolve_visual_render_plan(snapshot, width=1920, height=1080, title_text="這是一個很長的繁體中文標題 Traditional English mixed title", title_role="caption_subtitle", title_duration_seconds=0.8)
        plans.append(plan)
        output = tmp_path / f"{preset}.png"
        rendered = render_true_frame_preview({"ffmpeg_path": "ffmpeg"}, 1, source, 0.0, snapshot, output, runner=None, title_text="這是一個很長的繁體中文標題 Traditional English mixed title", title_role="caption_subtitle", title_duration_seconds=0.8)
        assert output.is_file() and rendered["visual_render_plan"]["title"]["motion"]["preset"] == preset
        assert rendered["visual_render_plan"]["title"]["anchor"] == "bottom-center"
        assert rendered["visual_render_plan"]["title"]["wrap_lines"] <= 3
        assert "\n" in rendered["visual_render_plan"]["title"]["filter"]
        assert r"\n" not in rendered["visual_render_plan"]["title"]["filter"]
        assert rendered["title_render_evidence"]["status"] == "pass"
        assert rendered["title_render_evidence"]["changed_pixels"] > 0
        assert rendered["title_render_evidence"]["in_frame"] is True
    assert len({plan["title"]["filter"] for plan in plans}) == 4
    unsupported = deepcopy(base)
    unsupported_title = deepcopy(unsupported["title_style"])
    unsupported_title["motion"] = {**unsupported_title["motion"], "preset": "bounce"}
    unsupported["title_style"] = unsupported_title
    unsupported.pop("semantic_hash", None)
    unsupported["resolved_hash"] = __import__("hashlib").sha256(__import__("json").dumps({key: value for key, value in unsupported.items() if key != "resolved_hash"}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with pytest.raises(VisualStyleError, match="unsupported title motion"):
        resolve_visual_render_plan(unsupported, width=1920, height=1080, title_text="Unsupported")


def test_approval_envelope_does_not_mutate_pixel_semantic_snapshot():
    snapshot = materialize_visual_style("diary_natural", _brief())
    semantic_before = snapshot["semantic_hash"]
    plan_before = resolve_visual_render_plan(snapshot, width=1920, height=1080, title_text="Coffee", title_role="chapter_title")
    approved = deepcopy(snapshot)
    approved.update({
        "approved_preview_variant_id": "variant-1",
        "approved_preview_plan_hash": "plan-1",
        "approved_preview_evidence_identity": {"preview_image_sha256": "pixels"},
        "approval_envelope": {"schema_version": "visual-style-approval-v1", "approved_at": "now"},
    })
    assert approved["semantic_hash"] == semantic_before
    plan_after = resolve_visual_render_plan(approved, width=1920, height=1080, title_text="Coffee", title_role="chapter_title")
    assert plan_after["visual_style_hash"] == plan_before["visual_style_hash"] == semantic_before
    assert plan_after["filter_graph"] == plan_before["filter_graph"]
    assert plan_after["resolved_hash"] == plan_before["resolved_hash"]


def test_title_registry_inheritance_materializes_and_fails_closed():
    titles = TitleStyleRegistry(TITLE_STYLES.list())
    parent = next(item for item in titles.list() if item["title_style_id"] == "test_soft_panel")
    parent["title_style_id"] = "base_test_style"
    parent["version"] = "1"
    child = {"title_style_id": "child_test_style", "version": "1", "extends": {"title_style_id": "base_test_style", "version": "1"}, "label": "Child", "responsive": {"landscape": {"anchor": "top-center", "size_ratio": 0.04}}}
    titles.register("base_test_style", parent)
    titles.register("child_test_style", child)
    resolved = titles.resolve("child_test_style", role="chapter_title", aspect="landscape")
    assert resolved["responsive"]["anchor"] == "top-center"
    assert resolved["motion"]["preset"] == parent["motion"]["preset"]
    assert resolved["resolved_parent_chain"][-1]["title_style_id"] == "child_test_style"
    with pytest.raises(VisualStyleError, match="parent"):
        titles.register("missing_parent", {"title_style_id": "missing_parent", "version": "1", "extends": {"title_style_id": "absent", "version": "1"}, "label": "Missing"})
    version_parent = deepcopy(parent)
    version_parent["title_style_id"] = "version_parent"
    version_parent["version"] = "2"
    titles.register("version_parent", version_parent)
    with pytest.raises(VisualStyleError, match="version"):
        titles.register("version_child", {"title_style_id": "version_child", "version": "1", "extends": {"title_style_id": "version_parent", "version": "1"}, "label": "Version child"})
    cycle_a = {"title_style_id": "cycle_a", "version": "1", "extends": {"title_style_id": "cycle_b", "version": "1"}}
    cycle_b = {"title_style_id": "cycle_b", "version": "1", "extends": {"title_style_id": "cycle_a", "version": "1"}}
    cycle_registry = TitleStyleRegistry({"cycle_a": cycle_a, "cycle_b": cycle_b})
    with pytest.raises(VisualStyleError, match="cycle"):
        cycle_registry.resolve("cycle_a")


def test_role_tokens_materialize_once_and_preview_formal_share_exact_role_contract():
    snapshots = {role: materialize_visual_style("diary_natural", _brief(), title_role=role) for role in ("chapter_title", "location_title", "lower_third")}
    assert all(item["title_style"]["role_materialized_for"] == role for role, item in snapshots.items())
    assert snapshots["location_title"]["title_style"]["responsive"]["anchor"] == "top-left"
    assert snapshots["location_title"]["title_style"]["max_width_ratio"] == 0.68
    assert snapshots["lower_third"]["title_style"]["responsive"]["anchor"] == "bottom-left"
    assert snapshots["lower_third"]["title_style"]["responsive"]["size_ratio"] == 0.034
    assert len({item["semantic_hash"] for item in snapshots.values()}) == 3
    plans = {role: resolve_visual_render_plan(item, width=1920, height=1080, title_text="Coffee") for role, item in snapshots.items()}
    assert plans["location_title"]["title"]["role"] == "location_title"
    assert plans["location_title"]["title"]["anchor"] == "top-left"
    assert plans["location_title"]["title"]["max_width_ratio"] == 0.68
    assert len({plan["semantic_hash"] for plan in plans.values()}) == 3
    assert resolve_visual_render_plan(snapshots["location_title"], width=1920, height=1080, title_text="Coffee")["title"] == plans["location_title"]["title"]


def test_visual_style_options_are_registry_metadata_not_raw_capability_lists():
    options = visual_style_options()
    for key in ("title_roles", "title_anchors", "title_motion_presets", "title_weight_values", "title_size_presets", "palette_variants", "readability_surfaces", "title_font_families"):
        assert options[key] and all(set(item) >= {"id", "label", "enabled", "capability"} for item in options[key])


def test_control_defaults_are_resolved_by_backend_for_role_and_sparse_child():
    defaults = visual_style_control_defaults(_brief(), style_id="diary_natural", title_style_id="diary_natural_overlay", title_role="location_title", aspect="landscape")
    assert len(defaults) == 1
    assert defaults[0]["anchor"] == "top-left"
    assert defaults[0]["max_width_ratio"] == 0.68
    assert defaults[0]["readability"]["surface"] == "translucent"
    assert defaults[0]["motion"]["preset"] == "fade"
    assert defaults[0]["is_default_title_style"] is True

    titles = TitleStyleRegistry(TITLE_STYLES.list())
    parent = next(item for item in titles.list() if item["title_style_id"] == "test_soft_panel")
    parent["title_style_id"] = "base_title"
    titles.register("base_title", parent)
    titles.register("child_title", {"title_style_id": "child_title", "version": "1", "extends": {"title_style_id": "base_title", "version": "1"}, "label": "Child", "overrides": {"motion": {"preset": "fade_rise"}}})
    child_defaults = visual_style_control_defaults(_brief(), style_id="diary_natural", title_style_id="child_title", title_role="location_title", aspect="landscape", title_registry=titles)
    assert child_defaults[0]["font_family"] == parent["font_family"]
    assert child_defaults[0]["weight"] == parent["weight"]
    assert child_defaults[0]["anchor"] == "top-left"
    assert child_defaults[0]["motion"]["preset"] == "fade_rise"


def test_unfiltered_sparse_child_defaults_use_inherited_roles_and_api_payload():
    styles = VisualStyleRegistry(VISUAL_STYLES.list())
    titles = TitleStyleRegistry(TITLE_STYLES.list())
    parent = next(item for item in titles.list() if item["title_style_id"] == "test_soft_panel")
    parent["title_style_id"] = "base_title"
    titles.register("base_title", parent)
    titles.register("child_title", {"title_style_id": "child_title", "version": "1", "extends": {"title_style_id": "base_title", "version": "1"}, "label": "Child", "overrides": {"motion": {"preset": "fade_rise"}}})
    visual = styles.resolve("test_soft_panel")
    visual.update({"style_id": "test_inherited_visual", "label": "Inherited Visual", "default_title_style_id": "child_title", "enabled_for_round1_ui": True})
    styles.register("test_inherited_visual", visual)

    defaults = visual_style_control_defaults(_brief(), registry=styles, title_registry=titles)
    child = [item for item in defaults if item["visual_style_id"] == "test_inherited_visual" and item["title_style_id"] == "child_title"]
    assert {item["role"] for item in child} >= {"chapter_title", "location_title", "lower_third"}
    location = next(item for item in child if item["role"] == "location_title")
    assert location["font_family"] == parent["font_family"]
    assert location["weight"] == parent["weight"]
    assert location["anchor"] == "top-left"
    assert location["readability"]["surface"] == parent["readability"]["surface"]
    assert location["motion"]["preset"] == "fade_rise"

    payload = visual_style_api_payload({"project_id": 1, "status": "approved", "approved": {}}, approved_brief=_brief(), registry=styles, title_registry=titles)
    payload_child = [item for item in payload["options"]["control_defaults"] if item["visual_style_id"] == "test_inherited_visual" and item["title_style_id"] == "child_title"]
    assert {item["role"] for item in payload_child} >= {"chapter_title", "location_title", "lower_third"}


def test_visual_style_api_payload_exposes_resolved_control_defaults():
    payload = visual_style_api_payload({"project_id": 1, "status": "approved", "approved": {}}, approved_brief=_brief())
    defaults = payload["options"]["control_defaults"]
    assert defaults
    assert all(item.get("font_family") and item.get("anchor") and item.get("readability") and item.get("motion") for item in defaults)


def test_title_capability_contract_exposes_all_anchors_and_rejects_unrendered_spacing():
    assert set(TITLE_ANCHORS) == {"top-left", "top-center", "top-right", "center", "bottom-left", "bottom-center", "bottom-right"}
    assert set(TITLE_MOTION_PRESETS) == {"none", "fade", "fade_rise", "slide_fade"}
    snapshot = materialize_visual_style("diary_natural", _brief())
    title = deepcopy(snapshot["title_style"])
    title["letter_spacing"] = 1
    invalid = {**snapshot, "title_style": title}
    invalid.pop("semantic_hash", None)
    invalid["resolved_hash"] = __import__("hashlib").sha256(__import__("json").dumps({key: value for key, value in invalid.items() if key != "resolved_hash"}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with pytest.raises(VisualStyleError, match="letter_spacing"):
        resolve_visual_render_plan(invalid, width=1920, height=1080, title_text="Unsupported")
    top_center = deepcopy(snapshot["title_style"])
    top_center["responsive"] = {**top_center["responsive"], "anchor": "top-center"}
    centered = {**snapshot, "title_style": top_center}
    centered.pop("semantic_hash", None)
    centered["resolved_hash"] = __import__("hashlib").sha256(__import__("json").dumps({key: value for key, value in centered.items() if key != "resolved_hash"}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert "(w-text_w)/2" in resolve_visual_render_plan(centered, width=1920, height=1080, title_text="Top") ["title"]["filter"]


def test_bounded_visual_overrides_are_materialized_and_unknown_values_fail_closed():
    snapshot = materialize_visual_style("diary_natural", _brief(), overrides={"composition": "standalone", "anchor": "top-center", "weight": 700, "size_preset": "large", "palette_variant": "high_contrast", "motion": {"preset": "slide_fade"}})
    assert snapshot["composition"] == "standalone"
    assert snapshot["overrides"]["size_preset"] == "large"
    assert snapshot["title_style"]["weight"] == 700
    assert snapshot["title_style"]["motion"]["preset"] == "slide_fade"
    with pytest.raises(VisualStyleError, match="unknown visual override"):
        materialize_visual_style("diary_natural", _brief(), overrides={"raw_ffmpeg": "drawtext=text=unsafe"})
    with pytest.raises(VisualStyleError, match="unsupported title anchor"):
        materialize_visual_style("diary_natural", _brief(), overrides={"anchor": "diagonal"})


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required for animated preview smoke")
def test_animated_preview_uses_shared_motion_plan_and_is_bounded(tmp_path: Path):
    source = tmp_path / "animated-source.mp4"
    output = tmp_path / "animated-preview.mp4"
    generated = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10", "-t", "0.8", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)], capture_output=True, text=True, encoding="utf-8", check=False)
    assert generated.returncode == 0, generated.stderr
    snapshot = materialize_visual_style("diary_natural", _brief(), overrides={"motion": {"preset": "fade_rise"}})
    result = render_animated_title_preview({"ffmpeg_path": "ffmpeg"}, 1, source, 0, snapshot, output, title_role="location_title", duration_seconds=4)
    assert result["preview_kind"] == "animated"
    assert result["duration_seconds"] == 2.5
    assert output.is_file() and result["visual_render_plan"]["title"]["role"] == "location_title"


def test_visual_style_approval_enforces_base_revision_without_advancing_story_revision(tmp_path: Path, monkeypatch):
    from video_vault.visual_style import save_visual_style_approval
    from video_vault.database import connect

    db = tmp_path / "approval.sqlite3"
    init_db(db)
    with connect(db) as con:
        con.execute("insert into projects(id, name, project_revision) values(1, 'Visual', 1)")
        con.execute("insert into visual_style_states(project_id, status, recommendation_json, approved_json) values(1, 'needs_confirmation', '{}', '{}')")
    monkeypatch.setattr("video_vault.visual_style._load_brief", lambda *_args: _brief())
    monkeypatch.setattr("video_vault.visual_style._load_preview_evidence", lambda *_args: {"preview_image_sha256": "pixels", "source_media_uuid": "media", "generated_at": "now"})
    monkeypatch.setattr("video_vault.visual_style._validate_preview_evidence", lambda *args, **kwargs: None)
    monkeypatch.setattr("video_vault.visual_style._source_provenance", lambda *_args: [])
    monkeypatch.setattr("video_vault.color_consistency.load_project_color_state", lambda *_args: {})
    monkeypatch.setattr("video_vault.color_consistency.effective_color_settings", lambda *_args: {})
    saved = save_visual_style_approval({"library_root": str(tmp_path)}, db, 1, {"visual_style_id": "diary_natural", "preview_variant_id": "v1", "preview_plan_hash": "p1"}, base_revision=1)
    assert saved["status"] == "approved"
    with connect(db) as con:
        assert int(con.execute("select project_revision from projects where id=1").fetchone()[0]) == 1
    with pytest.raises(ProjectRevisionConflict):
        save_visual_style_approval({"library_root": str(tmp_path)}, db, 1, {"visual_style_id": "diary_natural", "preview_variant_id": "v2", "preview_plan_hash": "p2"}, base_revision=0)


def test_preview_surface_contains_roles_and_bounded_animation_evidence(tmp_path: Path, monkeypatch):
    from video_vault.visual_style import preview_visual_styles
    from video_vault.database import connect

    db = tmp_path / "preview.sqlite3"
    init_db(db)
    with connect(db) as con:
        con.execute("insert into projects(id, name, project_revision) values(1, 'Preview', 1)")
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    source_data = {"project_media_uuid": "media-1", "path": str(source), "fingerprint": {"sha256": "source"}, "duration_seconds": 10.0, "display_geometry": {"display_ratio": 16 / 9, "source_orientation": "landscape", "sample_aspect_ratio": "1:1"}}
    monkeypatch.setattr("video_vault.visual_style._load_brief", lambda *_args: _brief())
    monkeypatch.setattr("video_vault.visual_style.ensure_visual_style_state", lambda *_args: {"project_id": 1, "status": "approved", "preview_revision": 1})
    monkeypatch.setattr("video_vault.visual_style._source_provenance", lambda *_args: [source_data])
    monkeypatch.setattr("video_vault.visual_style._measure_source_luma", lambda *_args: 0.8)

    def fake_static(_cfg, _project_id, _source, _timestamp, _snapshot, output, **_kwargs):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"png")
        return {"file": str(output), "sha256": __import__("hashlib").sha256(b"png").hexdigest(), "title_render_evidence": {"version": "title-pixel-evidence-v1", "status": "pass", "changed_pixels": 12, "in_frame": True, "bbox": {"x": 10, "y": 10, "width": 100, "height": 40}}}

    def fake_animated(_cfg, _project_id, _source, _timestamp, _snapshot, output, **_kwargs):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"mp4")
        return {"file": str(output), "sha256": __import__("hashlib").sha256(b"mp4").hexdigest(), "duration_seconds": 2.0, "preview_kind": "animated"}

    monkeypatch.setattr("video_vault.visual_style._resolve_font", _fake_font_identity)
    monkeypatch.setattr("video_vault.visual_style.render_true_frame_preview", fake_static)
    monkeypatch.setattr("video_vault.visual_style.render_animated_title_preview", fake_animated)
    result = preview_visual_styles({"ffmpeg_path": "ffmpeg", "ffprobe_path": "ffprobe", "library_root": str(tmp_path)}, db, 1, force=True, overrides={"anchor": "top-center"}, scope="extended")
    assert result["ok"] is True
    assert {str(item["title_role"]) for item in result["variants"] if item.get("preview_kind") == "static"} == {"chapter_title", "location_title"}
    assert any(item.get("preview_kind") == "animated" for item in result["variants"])
    assert all(item.get("preview_plan_hash") for item in result["variants"])


def test_primary_scope_requires_registry_public_primary_heroes_and_excludes_extended(tmp_path: Path, monkeypatch):
    from video_vault.visual_style import preview_visual_styles
    from video_vault.database import connect

    db = tmp_path / "primary.sqlite3"
    init_db(db)
    with connect(db) as con:
        con.execute("insert into projects(id, name, project_revision) values(1, 'Primary', 1)")
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    source_data = {"project_media_uuid": "media-1", "path": str(source), "fingerprint": {"sha256": "source"}, "duration_seconds": 10.0, "display_geometry": {"display_ratio": 16 / 9, "source_orientation": "landscape", "sample_aspect_ratio": "1:1"}}
    monkeypatch.setattr("video_vault.visual_style._load_brief", lambda *_args: _brief())
    monkeypatch.setattr("video_vault.visual_style.ensure_visual_style_state", lambda *_args: {"project_id": 1, "status": "approved", "preview_revision": 1})
    monkeypatch.setattr("video_vault.visual_style._source_provenance", lambda *_args: [source_data])
    monkeypatch.setattr("video_vault.visual_style._measure_source_luma", lambda *_args: 0.8)

    def fake_static(_cfg, _project_id, _source, _timestamp, _snapshot, output, **_kwargs):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"png")
        return {"file": str(output), "sha256": __import__("hashlib").sha256(b"png").hexdigest(), "title_render_evidence": {"version": "title-pixel-evidence-v1", "status": "pass", "changed_pixels": 12, "in_frame": True, "bbox": {"x": 10, "y": 10, "width": 100, "height": 40}}}

    monkeypatch.setattr("video_vault.visual_style.render_true_frame_preview", fake_static)
    registry = VisualStyleRegistry(VISUAL_STYLES.list())
    future = registry.resolve("diary_natural")
    future.update({"style_id": "future_public", "label": "Future Public", "default_title_style_id": "diary_natural_overlay", "preview_scope": "primary", "public_primary": True})
    registry.register("future_public", future)
    monkeypatch.setattr("video_vault.visual_style._resolve_font", _fake_font_identity)
    result = preview_visual_styles({"ffmpeg_path": "ffmpeg", "ffprobe_path": "ffprobe", "library_root": str(tmp_path)}, db, 1, force=True, registry=registry)
    assert result["ok"] is True
    assert {item["visual_style"]["visual_style_id"] for item in result["variants"]} == {"diary_natural", "clean_minimal", "cinematic", "future_public"}
    assert all(item["title_role"] == "chapter_title" and item["preview_kind"] == "static" for item in result["variants"])


def test_preview_render_contract_version_invalidates_legacy_cache_and_then_hits(tmp_path: Path, monkeypatch):
    from video_vault.visual_style import preview_visual_styles
    from video_vault.database import connect

    db = tmp_path / "preview-cache.sqlite3"
    init_db(db)
    with connect(db) as con:
        con.execute("insert into projects(id, name, project_revision) values(1, 'Preview Cache', 1)")
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    source_data = {"project_media_uuid": "media-1", "path": str(source), "fingerprint": {"sha256": "source"}, "duration_seconds": 10.0, "display_geometry": {"display_ratio": 16 / 9, "source_orientation": "landscape", "sample_aspect_ratio": "1:1"}}
    monkeypatch.setattr("video_vault.visual_style._load_brief", lambda *_args: _brief())
    monkeypatch.setattr("video_vault.visual_style.ensure_visual_style_state", lambda *_args: {"project_id": 1, "status": "approved", "preview_revision": 1})
    monkeypatch.setattr("video_vault.visual_style._source_provenance", lambda *_args: [source_data])
    monkeypatch.setattr("video_vault.visual_style._measure_source_luma", lambda *_args: 0.8)
    monkeypatch.setattr("video_vault.visual_style._resolve_font", _fake_font_identity)

    def fake_static(_cfg, _project_id, _source, _timestamp, _snapshot, output, **_kwargs):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"png")
        return {"file": str(output), "sha256": __import__("hashlib").sha256(b"png").hexdigest(), "title_render_evidence": {"version": "title-pixel-evidence-v1", "status": "pass", "changed_pixels": 12, "in_frame": True, "bbox": {"x": 10, "y": 10, "width": 100, "height": 40}}}

    monkeypatch.setattr("video_vault.visual_style.render_true_frame_preview", fake_static)
    monkeypatch.setattr("video_vault.visual_style.VISUAL_PREVIEW_RENDER_CONTRACT_VERSION", "visual-preview-render-v1")
    legacy = preview_visual_styles({"ffmpeg_path": "ffmpeg", "ffprobe_path": "ffprobe", "library_root": str(tmp_path)}, db, 1, force=True)
    monkeypatch.setattr("video_vault.visual_style.VISUAL_PREVIEW_RENDER_CONTRACT_VERSION", "visual-preview-render-v2")
    current = preview_visual_styles({"ffmpeg_path": "ffmpeg", "ffprobe_path": "ffprobe", "library_root": str(tmp_path)}, db, 1, force=False)
    repeat = preview_visual_styles({"ffmpeg_path": "ffmpeg", "ffprobe_path": "ffprobe", "library_root": str(tmp_path)}, db, 1, force=False)
    assert {item["preview_render_contract_version"] for item in legacy["variants"]} == {"visual-preview-render-v1"}
    assert {item["preview_render_contract_version"] for item in current["variants"]} == {"visual-preview-render-v2"}
    assert {item["file"] for item in legacy["variants"]}.isdisjoint({item["file"] for item in current["variants"]})
    assert all(item["cache_hit"] is False for item in current["variants"])
    assert all(item["cache_hit"] is True for item in repeat["variants"])


def test_bounded_seek_contract_is_shared_and_preserves_near_beginning_timestamp():
    assert _preview_seek_args(0.25) == ["-ss", "0.000000", "-i", "__SOURCE__", "-ss", "0.250000"]
    assert _preview_seek_args(32.675531) == ["-ss", "30.675531", "-i", "__SOURCE__", "-ss", "2.000000"]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required for media smoke")
def test_bounded_seek_matches_full_decode_on_long_gop_fixture(tmp_path: Path):
    source = tmp_path / "long-gop.mp4"
    generated = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30", "-t", "8", "-c:v", "libx264", "-g", "60", "-keyint_min", "60", "-sc_threshold", "0", "-pix_fmt", "yuv420p", str(source)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert generated.returncode == 0, generated.stderr

    def frame(command: list[str]) -> bytes:
        result = subprocess.run(command, capture_output=True, check=False)
        assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
        assert len(result.stdout) == 160 * 90 * 3
        return result.stdout

    target = 5.25
    reference = frame(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(source), "-ss", f"{target:.6f}", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
    seek = _preview_seek_args(target)
    candidate = frame(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", *[str(source) if item == "__SOURCE__" else item for item in seek], "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
    assert candidate == reference

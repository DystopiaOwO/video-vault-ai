from pathlib import Path
import json
import subprocess

import pytest

from video_vault.color_consistency import (
    analyze_project_color,
    ColorReferenceError,
    color_state_for_api,
    default_color_state,
    ensure_reference_frame,
    effective_color_settings,
    normalize_color_state,
    preview_file_path,
    preview_cache_key,
    render_project_color_previews,
    save_project_color_state,
)
from video_vault.database import connect, init_db, upsert_video
from video_vault.project import create_project, project_dir


def _project(tmp_path: Path):
    cfg = {"library_root": str(tmp_path), "ffmpeg_path": "ffmpeg"}
    db = tmp_path / "05_index" / "video_vault.sqlite3"
    init_db(db)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    video_id = upsert_video(db, {"original_path": str(source), "current_path": str(source), "filename": source.name, "category": "travel"})
    project_id = create_project(db, "travel", [video_id], category="travel", content_type="travel_diary")
    return cfg, db, project_id, source, video_id


def test_color_state_keeps_suggested_and_applied_separate_and_clamps():
    state = normalize_color_state({"suggested": {"exposure": 2}, "applied": {"exposure": -2, "saturation": 5}})
    assert state["suggested"]["exposure"] == 1.0
    assert state["applied"]["exposure"] == -1.5
    assert state["applied"]["saturation"] == 1.2
    assert state["suggested"] is not state["applied"]


def test_analysis_selects_highest_scoring_reference_and_creates_suggestions(tmp_path, monkeypatch):
    cfg, db, project_id, source, video_id = _project(tmp_path)
    with connect(db) as con:
        con.execute("insert into segments(video_id, start_seconds, end_seconds, title, score) values(?, ?, ?, ?, ?)", (video_id, 0, 5, "車站", 0.4))
        con.execute("insert into segments(video_id, start_seconds, end_seconds, title, score) values(?, ?, ?, ?, ?)", (video_id, 20, 30, "核心畫面", 0.95))
    monkeypatch.setattr("video_vault.color_consistency._reference_luma", lambda cfg, reference: {"average": 190, "highlight_ratio": 0.2, "sampled_frames": 1})
    monkeypatch.setattr("video_vault.color_consistency.ensure_reference_frame", lambda cfg, db, project_id, reference: dict(reference, frame_name="reference.jpg"))
    state = analyze_project_color(cfg, db, project_id)
    assert state["reference"]["label"] == "核心畫面"
    assert state["suggested"]["exposure"] < 0
    assert state["applied"] == state["suggested"]


def test_effective_color_settings_respects_enable_and_exclude():
    state = default_color_state()
    state["enabled"] = False
    state["applied"].update({"mode": "manual", "exposure": 0.4})
    assert effective_color_settings(state)["mode"] == "none"
    state["enabled"] = True
    state["segments"]["seg-1"] = {"enabled": True, "locked": False, "excluded": True}
    assert effective_color_settings(state, "seg-1")["mode"] == "none"


def test_preview_cache_key_changes_when_applied_color_changes(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    state = default_color_state()
    first = preview_cache_key(source, state)
    changed = normalize_color_state({**state, "applied": {**state["applied"], "exposure": 0.3}})
    assert preview_cache_key(source, changed) != first


def test_project_before_after_preview_reuses_cache(tmp_path, monkeypatch):
    cfg, db, project_id, source, _ = _project(tmp_path)
    state = default_color_state()
    save_project_color_state(cfg, db, project_id, {**state, "analysis": {"basis_text": "test"}}, mark_review=False)
    calls = []

    def fake_preview(source_path, output, cfg, *args, **kwargs):
        calls.append((str(output), kwargs.get("color_settings")))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"preview")
        return output

    monkeypatch.setattr("video_vault.color.render_color_preview", fake_preview)
    first = render_project_color_previews(cfg, db, project_id)
    second = render_project_color_previews(cfg, db, project_id)
    assert len(calls) == 2
    assert first["previews"][0]["cache_hit"] is False
    assert second["previews"][0]["cache_hit"] is True


def test_locked_segment_keeps_applied_settings_and_disabled_renders_none():
    state = default_color_state()
    state["applied"].update({"mode": "manual", "exposure": 0.1})
    state["segments"]["seg-1"] = {"enabled": True, "locked": True, "excluded": False, "applied": {"mode": "manual", "exposure": -0.4}}
    effective = effective_color_settings(state, "seg-1")
    assert effective["mode"] == "manual"
    assert effective["exposure"] == -0.4
    state["segments"]["seg-1"]["enabled"] = False
    assert effective_color_settings(state, "seg-1")["mode"] == "none"


def test_excluded_segment_is_not_selected_as_reference(tmp_path, monkeypatch):
    cfg, db, project_id, source, video_id = _project(tmp_path)
    folder = project_dir(cfg, project_id)
    plan = {"groups": [{"label": "旅程", "activity": "風景", "segments": [{"clip_id": "clip_001", "video_id": video_id, "start_seconds": 0, "end_seconds": 1, "title": "排除", "score": 0.99}]}]}
    (folder / "project_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    state = default_color_state()
    state["segments"]["clip_001_00000000"] = {"enabled": True, "locked": False, "excluded": True, "applied": state["applied"]}
    save_project_color_state(cfg, db, project_id, {**state, "analysis": {"old": True}}, mark_review=False)
    monkeypatch.setattr("video_vault.color_consistency._reference_luma", lambda cfg, reference: {"average": 128, "highlight_ratio": 0, "sampled_frames": 1})
    result = analyze_project_color(cfg, db, project_id, force=True)
    assert result["reference"] == {}
    assert result["segments"]["clip_001_00000000"]["excluded"] is True


def test_force_analysis_preserves_locked_segment(tmp_path, monkeypatch):
    cfg, db, project_id, source, video_id = _project(tmp_path)
    folder = project_dir(cfg, project_id)
    plan = {"groups": [{"label": "旅程", "activity": "風景", "segments": [{"clip_id": "clip_001", "video_id": video_id, "start_seconds": 0, "end_seconds": 1, "title": "鎖定", "score": 0.9}]}]}
    (folder / "project_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    locked = {"enabled": True, "locked": True, "excluded": False, "suggested": {"exposure": 0.2}, "applied": {"mode": "manual", "exposure": -0.45}, "confidence": 0.8, "warnings": ["人工鎖定"]}
    save_project_color_state(cfg, db, project_id, {**default_color_state(), "analysis": {"old": True}, "segments": {"clip_001_00000000": locked}}, mark_review=False)
    monkeypatch.setattr("video_vault.color_consistency._reference_luma", lambda cfg, reference: {"average": 190, "highlight_ratio": 0.2, "sampled_frames": 1})
    result = analyze_project_color(cfg, db, project_id, force=True)
    assert result["segments"]["clip_001_00000000"]["applied"]["exposure"] == -0.45
    assert result["segments"]["clip_001_00000000"]["suggested"]["exposure"] == 0.2


def test_reference_frame_is_extracted_and_api_has_thumbnail_url(tmp_path):
    cfg, db, project_id, source, video_id = _project(tmp_path)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=320x240:d=1", "-y", str(source)], check=True)
    reference = ensure_reference_frame(cfg, db, project_id, {"id": "frame:red", "video_id": video_id, "source_file": str(source), "timestamp_seconds": 0.2, "type": "frame", "score": 1})
    assert Path(reference["frame_path"]).is_file()
    state = color_state_for_api(cfg, project_id, {**default_color_state(), "reference": reference, "references": [reference]})
    assert state["reference"]["frame_url"].startswith("/api/project/color-reference-file?")
    assert "frame_path" not in state["reference"]
    assert "source_file" not in state["reference"]
    assert state["reference"]["source_name"] == source.name


def test_reference_frame_rejects_missing_source(tmp_path):
    cfg, db, project_id, _, _ = _project(tmp_path)
    with pytest.raises(ColorReferenceError, match="原始素材不存在"):
        ensure_reference_frame(cfg, db, project_id, {"id": "missing", "source_file": str(tmp_path / "missing.mp4"), "timestamp_seconds": 0})


def test_preview_media_endpoint_token_blocks_path_traversal(tmp_path):
    cfg, db, project_id, _, _ = _project(tmp_path)
    preview_root = project_dir(cfg, project_id) / "output" / "color_previews"
    preview_root.mkdir(parents=True, exist_ok=True)
    (preview_root / "safe.mp4").write_bytes(b"x")
    assert preview_file_path(cfg, project_id, "safe.mp4").name == "safe.mp4"
    with pytest.raises(FileNotFoundError):
        preview_file_path(cfg, project_id, "..\\source.mp4")


def test_force_preview_bypasses_cache(tmp_path, monkeypatch):
    cfg, db, project_id, source, _ = _project(tmp_path)
    save_project_color_state(cfg, db, project_id, {**default_color_state(), "analysis": {"basis_text": "test"}}, mark_review=False)
    calls = []

    def fake_preview(source_path, output, cfg, *args, **kwargs):
        calls.append(output.name)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"preview")
        return output

    monkeypatch.setattr("video_vault.color.render_color_preview", fake_preview)
    render_project_color_previews(cfg, db, project_id)
    render_project_color_previews(cfg, db, project_id, force=True)
    assert len(calls) == 4


def test_real_ffmpeg_color_preview_writes_before_after(tmp_path):
    cfg, db, project_id, source, _ = _project(tmp_path)
    cfg["color"] = {"video_encoder": "libx264"}
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24", "-f", "lavfi", "-i", "sine=frequency=880", "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-y", str(source)], check=True)
    save_project_color_state(cfg, db, project_id, {**default_color_state(), "analysis": {"basis_text": "synthetic"}}, mark_review=False)
    result = render_project_color_previews(cfg, db, project_id, force=True, seconds=1)
    assert result["previews"]
    preview = result["previews"][0]
    before = project_dir(cfg, project_id) / "output" / "color_previews" / preview["before"]
    after = project_dir(cfg, project_id) / "output" / "color_previews" / preview["after"]
    assert before.is_file() and before.stat().st_size > 0
    assert after.is_file() and after.stat().st_size > 0


def test_missing_lut_is_reported_as_structured_analysis_warning(tmp_path):
    cfg, db, project_id, _, _ = _project(tmp_path)
    cfg["color"] = {"default_mode": "dji_dlog_m", "dji_lut_path": str(tmp_path / "missing.cube")}
    state = analyze_project_color(cfg, db, project_id)
    assert state["analysis"]["warnings"]
    assert "LUT" in state["analysis"]["warnings"][0]


def test_preview_cache_key_changes_when_lut_contents_change(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    lut = tmp_path / "look.cube"
    lut.write_bytes(b"lut-a")
    state = default_color_state()
    settings = {"mode": "dji_lut", "lut_path": str(lut), "exposure": 0}
    first = preview_cache_key(source, state, settings)
    lut.write_bytes(b"lut-b")
    assert preview_cache_key(source, state, settings) != first

from pathlib import Path
import json
import subprocess
from unittest.mock import Mock

import pytest

from video_vault.color_consistency import (
    _reference_candidates,
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
    set_color_reference,
    update_color_state,
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


def _write_project_plan(cfg, project_id, segments):
    plan = {"groups": [{"label": "test", "activity": "test", "segments": segments}]}
    (project_dir(cfg, project_id) / "project_plan.json").write_text(json.dumps(plan), encoding="utf-8")


def _insert_reference_rows(db, video_id, segments, frames):
    with connect(db) as con:
        for start, end, title, score in segments:
            con.execute(
                "insert into segments(video_id, start_seconds, end_seconds, title, score) values(?, ?, ?, ?, ?)",
                (video_id, start, end, title, score),
            )
        for index, (timestamp, score) in enumerate(frames):
            con.execute(
                "insert into frames(video_id, timestamp_seconds, frame_path, score_usefulness) values(?, ?, ?, ?)",
                (video_id, timestamp, f"frame-{index}.jpg", score),
            )


def _frame_candidates(cfg, db, project_id):
    return [item for item in _reference_candidates(db, project_id, cfg) if item["type"] == "frame"]


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


def test_analysis_only_segment_uses_project_applied_and_project_source():
    state = default_color_state()
    state["applied"].update({"mode": "warm_food", "exposure": -0.3})
    state["segment_analysis"]["seg-1"] = {
        "suggested": {"mode": "manual", "exposure": 0.8},
        "confidence": 0.9,
        "warnings": [],
    }
    state["segment_overrides"]["seg-1"] = {"enabled": True, "locked": False, "excluded": False}

    effective = effective_color_settings(state, "seg-1")

    assert effective["mode"] == "warm_food"
    assert effective["exposure"] == -0.3
    assert effective["effective_source"] == "project"
    assert "applied" not in normalize_color_state(state)["segment_overrides"]["seg-1"]


def test_enabled_segment_override_can_opt_in_when_project_default_is_disabled():
    state = default_color_state()
    state["enabled"] = False
    state["applied"]["mode"] = "manual"
    state["segments"]["seg-1"] = {"enabled": True, "locked": False, "excluded": False}
    assert effective_color_settings(state, "seg-1")["mode"] == "manual"


def test_removing_segment_color_override_restores_project_default(tmp_path):
    cfg, db, project_id, _, _ = _project(tmp_path)
    state = default_color_state()
    state["segments"]["seg-1"] = {"enabled": False, "locked": False, "excluded": False}
    save_project_color_state(cfg, db, project_id, state, mark_review=False)
    updated = update_color_state(cfg, db, project_id, {"segments": {"seg-1": None}})
    assert "seg-1" not in updated["segments"]
    assert effective_color_settings(updated, "seg-1")["mode"] == "none" if not updated["enabled"] else effective_color_settings(updated, "seg-1")["mode"] == updated["applied"]["mode"]


def test_resetting_segment_applied_override_does_not_revive_old_value(tmp_path):
    cfg, db, project_id, _, _ = _project(tmp_path)
    state = default_color_state()
    state["applied"].update({"mode": "safe_restore", "exposure": 0.1})
    state["segments"]["seg-1"] = {
        "enabled": True,
        "locked": False,
        "excluded": False,
        "applied": {"mode": "manual", "exposure": -0.7},
    }
    save_project_color_state(cfg, db, project_id, state, mark_review=False)

    reset = update_color_state(cfg, db, project_id, {"segments": {"seg-1": {"applied": None}}})
    assert "applied" not in reset["segment_overrides"]["seg-1"]
    assert effective_color_settings(reset, "seg-1")["exposure"] == 0.1
    assert effective_color_settings(reset, "seg-1")["effective_source"] == "project"

    tombstoned = update_color_state(cfg, db, project_id, {"segments": {"seg-1": None}})
    assert "seg-1" not in tombstoned["segment_overrides"]
    assert effective_color_settings(tombstoned, "seg-1")["exposure"] == 0.1


def test_force_reanalysis_keeps_analysis_only_segment_out_of_manual_overrides(tmp_path, monkeypatch):
    cfg, db, project_id, _, video_id = _project(tmp_path)
    segment_id = "analysis_only_00000000"
    _write_project_plan(cfg, project_id, [{"clip_id": "analysis_only", "video_id": video_id, "start_seconds": 0, "end_seconds": 1, "title": "analysis-only"}])
    state = default_color_state()
    state["applied"].update({"mode": "manual", "exposure": 0.25})
    state["analysis"] = {"old": True}
    state["segment_analysis"][segment_id] = {"suggested": {"exposure": -0.5}, "confidence": 0.8, "warnings": []}
    state["segment_overrides"][segment_id] = {"enabled": True, "locked": False, "excluded": False}
    save_project_color_state(cfg, db, project_id, state, mark_review=False)
    monkeypatch.setattr("video_vault.color_consistency._reference_luma", lambda cfg, reference: {"average": 190, "highlight_ratio": 0.2, "sampled_frames": 1})

    result = analyze_project_color(cfg, db, project_id, force=True)

    assert "applied" not in result["segment_overrides"][segment_id]
    assert result["segments"][segment_id]["applied"]["exposure"] == 0.25
    assert effective_color_settings(result, segment_id)["exposure"] == 0.25
    assert effective_color_settings(result, segment_id)["effective_source"] == "project"


def test_color_api_separates_analysis_and_manual_override_without_paths(tmp_path):
    cfg, db, project_id, _, _ = _project(tmp_path)
    lut_path = tmp_path / "private" / "look.cube"
    state = default_color_state()
    state["suggested"].update({"mode": "dji_lut", "lut_path": str(lut_path)})
    state["applied"].update({"mode": "manual", "lut_path": str(lut_path)})
    state["segment_analysis"]["seg-1"] = {"suggested": {"lut_path": str(lut_path)}, "warnings": [f"LUT 檔案不存在：{lut_path}"]}
    state["segment_overrides"]["seg-1"] = {"enabled": True, "locked": True, "excluded": False, "applied": {"mode": "manual", "lut_path": str(lut_path)}}

    api_state = color_state_for_api(cfg, project_id, state)
    payload = json.dumps(api_state, ensure_ascii=False)

    assert "suggested" in api_state["segment_analysis"]["seg-1"]
    assert "applied" in api_state["segment_overrides"]["seg-1"]
    assert api_state["suggested"]["lut_name"] == lut_path.name
    assert api_state["segment_overrides"]["seg-1"]["applied"]["lut_name"] == lut_path.name
    assert "lut_path" not in api_state["suggested"]
    assert str(lut_path) not in payload


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


def test_frame_in_excluded_project_segment_is_not_a_reference_candidate(tmp_path, monkeypatch):
    cfg, db, project_id, _, video_id = _project(tmp_path)
    segments = [
        {"clip_id": "excluded", "video_id": video_id, "start_seconds": 0, "end_seconds": 10, "title": "excluded"},
        {"clip_id": "normal", "video_id": video_id, "start_seconds": 20, "end_seconds": 30, "title": "normal"},
    ]
    _write_project_plan(cfg, project_id, segments)
    _insert_reference_rows(db, video_id, [(0, 10, "excluded", 0.2), (20, 30, "normal", 0.1)], [(5, 1.0), (25, 0.4)])
    state = default_color_state()
    state["segments"]["excluded_00000000"] = {"enabled": True, "excluded": True}
    save_project_color_state(cfg, db, project_id, state, mark_review=False)
    monkeypatch.setattr("video_vault.color_consistency._reference_luma", lambda cfg, reference: {"average": 128, "highlight_ratio": 0, "sampled_frames": 1})
    monkeypatch.setattr("video_vault.color_consistency.ensure_reference_frame", lambda cfg, db, project_id, reference: dict(reference))

    candidates = _frame_candidates(cfg, db, project_id)
    assert [item["id"] for item in candidates] == [f"frame:{video_id}:25000"]
    assert analyze_project_color(cfg, db, project_id, force=True)["reference"]["timestamp_seconds"] == 25


def test_frame_in_disabled_project_segment_is_not_a_reference_candidate(tmp_path):
    cfg, db, project_id, _, video_id = _project(tmp_path)
    segment = {"clip_id": "disabled", "video_id": video_id, "start_seconds": 0, "end_seconds": 10, "title": "disabled"}
    _write_project_plan(cfg, project_id, [segment])
    _insert_reference_rows(db, video_id, [(0, 10, "disabled", 0.2)], [(5, 1.0)])
    state = default_color_state()
    state["segments"]["disabled_00000000"] = {"enabled": False, "excluded": False}
    save_project_color_state(cfg, db, project_id, state, mark_review=False)

    assert _frame_candidates(cfg, db, project_id) == []


def test_frame_outside_excluded_project_segment_remains_a_reference_candidate(tmp_path, monkeypatch):
    cfg, db, project_id, _, video_id = _project(tmp_path)
    segments = [
        {"clip_id": "excluded", "video_id": video_id, "start_seconds": 0, "end_seconds": 1, "title": "excluded"},
        {"clip_id": "included", "video_id": video_id, "start_seconds": 2, "end_seconds": 3, "title": "included"},
    ]
    _write_project_plan(cfg, project_id, segments)
    _insert_reference_rows(db, video_id, [(0, 1, "excluded", 0.2), (2, 3, "included", 0.1)], [(2.5, 1.0)])
    state = default_color_state()
    state["segments"]["excluded_00000000"] = {"enabled": True, "excluded": True}
    save_project_color_state(cfg, db, project_id, state, mark_review=False)
    monkeypatch.setattr("video_vault.color_consistency._reference_luma", lambda cfg, reference: {"average": 128, "highlight_ratio": 0, "sampled_frames": 1})
    monkeypatch.setattr("video_vault.color_consistency.ensure_reference_frame", lambda cfg, db, project_id, reference: dict(reference))

    candidates = _frame_candidates(cfg, db, project_id)
    assert [item["id"] for item in candidates] == [f"frame:{video_id}:2500"]
    assert candidates[0]["segment_id"] == "included_00002000"
    result = analyze_project_color(cfg, db, project_id, force=True)
    assert result["reference"]["id"] == f"frame:{video_id}:2500"


def test_overlapping_project_segments_map_frames_by_shortest_interval_and_stable_tiebreak(tmp_path):
    cfg, db, project_id, _, video_id = _project(tmp_path)
    segments = [
        {"clip_id": "wide", "video_id": video_id, "start_seconds": 0, "end_seconds": 10, "title": "wide"},
        {"clip_id": "narrow", "video_id": video_id, "start_seconds": 4, "end_seconds": 6, "title": "narrow"},
        {"clip_id": "tie_first", "video_id": video_id, "start_seconds": 12, "end_seconds": 14, "title": "tie first"},
        {"clip_id": "tie_second", "video_id": video_id, "start_seconds": 12, "end_seconds": 14, "title": "tie second"},
    ]
    _write_project_plan(cfg, project_id, segments)
    rows = [(0, 10, "wide", 0.1), (4, 6, "narrow", 0.1), (12, 14, "tie first", 0.1), (12, 14, "tie second", 0.1)]
    _insert_reference_rows(db, video_id, list(reversed(rows)), [(5, 1.0), (13, 0.9)])

    first_mapping = {item["id"]: item.get("segment_id") for item in _frame_candidates(cfg, db, project_id)}
    with connect(db) as con:
        con.execute("delete from segments where video_id=?", (video_id,))
    _insert_reference_rows(db, video_id, rows, [])
    second_mapping = {item["id"]: item.get("segment_id") for item in _frame_candidates(cfg, db, project_id)}

    assert first_mapping == second_mapping == {
        f"frame:{video_id}:5000": "narrow_00004000",
        f"frame:{video_id}:13000": "tie_first_00012000",
    }


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


@pytest.mark.media_e2e
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


def test_sanitized_api_state_round_trip_keeps_internal_reference_paths(tmp_path):
    cfg, db, project_id, source, _ = _project(tmp_path)
    frame_path = tmp_path / "reference.jpg"
    frame_path.write_bytes(b"frame")
    reference = {
        "id": "frame:internal",
        "source_file": str(source),
        "frame_path": str(frame_path),
        "frame_name": frame_path.name,
        "timestamp_seconds": 0.2,
        "type": "frame",
    }
    save_project_color_state(
        cfg,
        db,
        project_id,
        {
            **default_color_state(),
            "reference": reference,
            "references": [reference],
            "analysis": {"reference": reference},
        },
        mark_review=False,
    )

    api_state = color_state_for_api(cfg, project_id)
    for item in [api_state["reference"], api_state["references"][0], api_state["analysis"]["reference"]]:
        assert "source_file" not in item
        assert "frame_path" not in item
    assert api_state["reference"]["source_name"] == source.name
    assert api_state["reference"]["frame_url"].startswith("/api/project/color-reference-file?")

    round_tripped = update_color_state(cfg, db, project_id, api_state)
    assert round_tripped["reference"]["source_file"] == str(source)
    assert round_tripped["reference"]["frame_path"] == str(frame_path)
    assert round_tripped["references"][0]["source_file"] == str(source)
    assert round_tripped["references"][0]["frame_path"] == str(frame_path)
    assert round_tripped["analysis"]["reference"]["source_file"] == str(source)
    assert round_tripped["analysis"]["reference"]["frame_path"] == str(frame_path)


def test_locked_segment_can_be_unlocked_and_edit_applied_settings(tmp_path):
    cfg, db, project_id, _, _ = _project(tmp_path)
    locked = {
        "enabled": True,
        "locked": True,
        "excluded": False,
        "suggested": {"mode": "manual", "exposure": 0.2},
        "applied": {"mode": "manual", "exposure": -0.4},
    }
    save_project_color_state(cfg, db, project_id, {**default_color_state(), "segments": {"seg-1": locked}}, mark_review=False)

    updated = update_color_state(
        cfg,
        db,
        project_id,
        {"segments": {"seg-1": {"locked": False, "applied": {"mode": "manual", "exposure": 0.7}}}},
    )
    assert updated["segments"]["seg-1"]["locked"] is False
    assert updated["segments"]["seg-1"]["applied"]["exposure"] == 0.7


def test_force_analysis_preserves_locked_and_updates_unlocked_after_unlock(tmp_path, monkeypatch):
    cfg, db, project_id, _, video_id = _project(tmp_path)
    folder = project_dir(cfg, project_id)
    plan = {
        "groups": [
            {
                "label": "旅程",
                "activity": "風景",
                "segments": [
                    {"clip_id": "clip_locked", "video_id": video_id, "start_seconds": 0, "end_seconds": 1, "title": "鎖定", "score": 0.9},
                    {"clip_id": "clip_editable", "video_id": video_id, "start_seconds": 1, "end_seconds": 2, "title": "可編輯", "score": 0.8},
                ],
            }
        ]
    }
    (folder / "project_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    locked_id = "clip_locked_00000000"
    editable_id = "clip_editable_00001000"
    locked = {"enabled": True, "locked": True, "excluded": False, "suggested": {"exposure": 0.2}, "applied": {"mode": "manual", "exposure": -0.45}}
    editable = {"enabled": True, "locked": False, "excluded": False, "suggested": {"exposure": 0.2}, "applied": {"mode": "manual", "exposure": 0.2}}
    save_project_color_state(cfg, db, project_id, {**default_color_state(), "analysis": {"old": True}, "segments": {locked_id: locked, editable_id: editable}}, mark_review=False)
    monkeypatch.setattr("video_vault.color_consistency._reference_luma", lambda cfg, reference: {"average": 190, "highlight_ratio": 0.2, "sampled_frames": 1})
    monkeypatch.setattr("video_vault.color_consistency.ensure_reference_frame", lambda cfg, db, project_id, reference: dict(reference, frame_name="reference.jpg"))

    after_force = analyze_project_color(cfg, db, project_id, force=True)
    assert after_force["segments"][locked_id]["applied"]["exposure"] == -0.45
    assert after_force["segments"][locked_id]["suggested"]["exposure"] == 0.2
    assert after_force["segments"][editable_id]["suggested"]["exposure"] == -0.5

    unlocked = update_color_state(cfg, db, project_id, {"segments": {locked_id: {"locked": False}}})
    assert unlocked["segments"][locked_id]["locked"] is False
    after_unlock = analyze_project_color(cfg, db, project_id, force=True)
    assert after_unlock["segments"][locked_id]["locked"] is False
    assert after_unlock["segments"][locked_id]["suggested"]["exposure"] == -0.5


def test_excluded_segment_analysis_does_not_call_reference_luma(tmp_path, monkeypatch):
    cfg, db, project_id, _, video_id = _project(tmp_path)
    folder = project_dir(cfg, project_id)
    plan = {"groups": [{"label": "旅程", "activity": "風景", "segments": [{"clip_id": "clip_001", "video_id": video_id, "start_seconds": 0, "end_seconds": 1, "title": "排除", "score": 0.99}]}]}
    (folder / "project_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    segment_id = "clip_001_00000000"
    state = default_color_state()
    state["segments"][segment_id] = {"enabled": True, "locked": False, "excluded": True}
    save_project_color_state(cfg, db, project_id, {**state, "analysis": {"old": True}}, mark_review=False)
    reference_luma = Mock(side_effect=AssertionError("excluded segment must not be analyzed"))
    monkeypatch.setattr("video_vault.color_consistency._reference_luma", reference_luma)

    result = analyze_project_color(cfg, db, project_id, force=True)
    assert result["reference"] == {}
    assert result["segments"][segment_id]["excluded"] is True
    reference_luma.assert_not_called()


def test_switching_reference_recalculates_unlocked_and_preserves_locked_excluded(tmp_path, monkeypatch):
    cfg, db, project_id, source, _ = _project(tmp_path)
    folder = project_dir(cfg, project_id)
    frame_a = tmp_path / "reference-a.jpg"
    frame_b = tmp_path / "reference-b.jpg"
    frame_a.write_bytes(b"a")
    frame_b.write_bytes(b"b")
    reference_a = {"id": "frame:a", "source_file": str(source), "frame_path": str(frame_a), "frame_name": frame_a.name, "timestamp_seconds": 0.1, "type": "frame", "label": "A"}
    reference_b = {"id": "frame:b", "source_file": str(source), "frame_path": str(frame_b), "frame_name": frame_b.name, "timestamp_seconds": 0.2, "type": "frame", "label": "B"}
    (folder / "project_plan.json").write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "label": "旅程",
                        "activity": "風景",
                        "segments": [
                            {"clip_id": "locked", "video_id": 1, "start_seconds": 0, "end_seconds": 1, "title": "鎖定"},
                            {"clip_id": "excluded", "video_id": 1, "start_seconds": 1, "end_seconds": 2, "title": "排除"},
                            {"clip_id": "unlocked", "video_id": 1, "start_seconds": 2, "end_seconds": 3, "title": "未鎖定"},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    locked = {"enabled": True, "locked": True, "excluded": False, "suggested": {"exposure": 0.2}, "applied": {"mode": "manual", "exposure": -0.4}}
    excluded = {"enabled": True, "locked": False, "excluded": True, "suggested": {"exposure": 0.3}, "applied": {"mode": "manual", "exposure": -0.2}}
    unlocked = {"enabled": True, "locked": False, "excluded": False, "suggested": {"exposure": 0.2}, "applied": {"mode": "manual", "exposure": 0.2}}
    segment_states = {"locked_00000000": locked, "excluded_00001000": excluded, "unlocked_00002000": unlocked}
    save_project_color_state(
        cfg,
        db,
        project_id,
        {**default_color_state(), "reference": reference_a, "references": [reference_a, reference_b], "suggested": {"exposure": 0.2}, "segments": segment_states},
        mark_review=False,
    )
    monkeypatch.setattr("video_vault.color_consistency.ensure_reference_frame", lambda cfg, db, project_id, reference: dict(reference))
    monkeypatch.setattr(
        "video_vault.color_consistency._reference_luma",
        lambda cfg, reference: {"average": 190, "highlight_ratio": 0.2, "sampled_frames": 1},
    )

    result = set_color_reference(cfg, db, project_id, "frame:b")
    assert result["reference"]["id"] == "frame:b"
    assert result["segments"]["unlocked_00002000"]["suggested"]["exposure"] == -0.5
    assert result["segments"]["locked_00000000"]["locked"] is True
    assert result["segments"]["locked_00000000"]["suggested"]["exposure"] == 0.2
    assert result["segments"]["locked_00000000"]["applied"]["exposure"] == -0.4
    assert result["segments"]["excluded_00001000"]["excluded"] is True
    assert result["segments"]["excluded_00001000"]["suggested"]["exposure"] == 0.3
    assert result["segments"]["excluded_00001000"]["applied"]["exposure"] == -0.2


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


@pytest.mark.media_e2e
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
    lut.write_text("TITLE \"identity\"\nLUT_3D_SIZE 2\n0 0 0\n0 0 1\n0 1 0\n0 1 1\n1 0 0\n1 0 1\n1 1 0\n1 1 1\n", encoding="ascii")
    state = default_color_state()
    settings = {"mode": "dji_lut", "lut_path": str(lut), "exposure": 0}
    first = preview_cache_key(source, state, {"effective_settings": settings})
    lut.write_bytes(b"lut-b")
    assert preview_cache_key(source, state, {"effective_settings": settings}) != first


@pytest.mark.media_e2e
def test_project_preview_cache_misses_after_same_path_lut_replacement(tmp_path, monkeypatch):
    cfg, db, project_id, source, _ = _project(tmp_path)
    lut = tmp_path / "look.cube"
    lut.write_text("TITLE \"identity\"\nLUT_3D_SIZE 2\n0 0 0\n0 0 1\n0 1 0\n0 1 1\n1 0 0\n1 0 1\n1 1 0\n1 1 1\n", encoding="ascii")
    state = default_color_state()
    state["applied"].update({"mode": "dji_lut", "lut_path": str(lut), "lut_kind": "cube"})
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

    lut.write_bytes(b"lut-b")
    third = render_project_color_previews(cfg, db, project_id)
    assert len(calls) == 4
    assert third["previews"][0]["cache_hit"] is False
    assert third["previews"][0]["cache_key"] != second["previews"][0]["cache_key"]

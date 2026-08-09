import json
from pathlib import Path

import pytest

from video_vault.database import add_analysis, add_bgm_track, add_project_bgm, init_db, upsert_video
from video_vault.color_consistency import default_color_state, save_project_color_state
from video_vault.project import build_project_plan, create_project, project_detail, save_segment_review, update_segment_evidence
from video_vault.render_manifest import _manifest_bgm, build_render_manifest, compile_render_manifest, manifest_hash, validate_render_manifest


def _project(tmp_path: Path, count: int = 2) -> tuple[dict, Path, int]:
    db = tmp_path / "db.sqlite3"
    init_db(db)
    videos = []
    for index in range(count):
        source = tmp_path / f"20260718_081500_coffee_{index}.mp4"
        source.write_bytes(b"video")
        video_id = upsert_video(db, {"original_path": str(source), "current_path": str(source), "filename": source.name, "category": "coffee", "duration_seconds": 10})
        add_analysis(
            db,
            video_id,
            "mock",
            "rules",
            {
                "segments": [
                    {"start_seconds": 1, "end_seconds": 5, "segment_type": "key_action", "title": f"first-{index}", "reason": "ok", "tags": ["coffee"], "score": 0.9, "suggested_use": "main"},
                    {"start_seconds": 6, "end_seconds": 8, "segment_type": "detail", "title": f"second-{index}", "reason": "ok", "tags": ["closeup"], "score": 0.8, "suggested_use": "B-roll"},
                ]
            },
            tmp_path / f"raw-{index}.json",
        )
        videos.append(video_id)
    cfg = {"library_root": str(tmp_path)}
    project_id = create_project(db, "契約測試", videos, category="coffee")
    build_project_plan(cfg, db, project_id)
    return cfg, db, project_id


def test_manifest_uses_review_override_exclude_and_manual_order(tmp_path):
    cfg, db, project_id = _project(tmp_path, count=1)
    rows = project_detail(cfg, db, project_id)["segments"]
    original_ids = {row["segment_id"] for row in rows}
    rows[0].update({"start_seconds": 1.25, "end_seconds": 3.875, "speed": 2.0})
    rows[1]["include"] = False
    save_segment_review(cfg, db, project_id, [rows[1], rows[0]])

    manifest = compile_render_manifest(cfg, db, project_id)

    assert len(manifest["segments"]) == 1
    segment = manifest["segments"][0]
    assert segment["segment_id"] in original_ids
    assert segment["source_in_seconds"] == 1.25
    assert segment["source_out_seconds"] == 3.875
    assert segment["source_duration_seconds"] == 2.625
    assert segment["timeline_duration_seconds"] == 1.3125
    assert segment["order"] == 1
    assert Path(tmp_path, "08_projects", f"project_{project_id}", "render_manifest.json").exists()


def test_manifest_carries_duplicate_group_for_delivery_qa_without_filename_semantics(tmp_path):
    cfg, db, project_id = _project(tmp_path, count=1)
    rows = project_detail(cfg, db, project_id)["segments"]
    update_segment_evidence(cfg, db, project_id, rows[0]["segment_id"], {"duplicate_group": "perception-duplicate-group-1"})
    manifest = compile_render_manifest(cfg, db, project_id)
    assert manifest["segments"][0]["duplicate_group"] == "perception-duplicate-group-1"


def test_manifest_carries_bgm_duration_for_delivery_coverage_audit(tmp_path):
    cfg, db, project_id = _project(tmp_path, count=1)
    bgm = tmp_path / "bgm.mp3"
    bgm.write_bytes(b"fixture")
    track_id = add_bgm_track(db, {"title": "fixture", "file_path": str(bgm), "duration_seconds": 12.5})
    add_project_bgm(db, project_id, track_id)
    tracks = _manifest_bgm(cfg, db, project_id, {"audio": {}}, validate_selected_bgm=False)
    assert tracks[0]["duration_seconds"] == 12.5


def test_manual_order_does_not_change_stable_segment_ids(tmp_path):
    cfg, db, project_id = _project(tmp_path, count=1)
    before = {row["title"]: row["segment_id"] for row in project_detail(cfg, db, project_id)["segments"]}
    rows = project_detail(cfg, db, project_id)["segments"]
    save_segment_review(cfg, db, project_id, [rows[1], rows[0]])
    after = {row["title"]: row["segment_id"] for row in project_detail(cfg, db, project_id)["segments"]}
    assert after == before
    assert [row["title"] for row in compile_render_manifest(cfg, db, project_id)["segments"]] == [rows[1]["title"], rows[0]["title"]]


def test_manifest_hash_is_deterministic_and_created_at_independent(tmp_path):
    cfg, db, project_id = _project(tmp_path, count=1)
    first = compile_render_manifest(cfg, db, project_id)
    second = compile_render_manifest(cfg, db, project_id)
    assert first["manifest_hash"] == second["manifest_hash"]
    second["created_at"] = "2099-01-01T00:00:00+00:00"
    assert manifest_hash(second) == first["manifest_hash"]

    rows = project_detail(cfg, db, project_id)["segments"]
    rows[0]["speed"] = 2
    save_segment_review(cfg, db, project_id, rows)
    changed = compile_render_manifest(cfg, db, project_id)
    assert changed["manifest_hash"] != first["manifest_hash"]


def test_visual_item_change_changes_approved_manifest_hash(tmp_path):
    cfg, db, project_id = _project(tmp_path, count=1)
    first = compile_render_manifest(cfg, db, project_id)
    plan_path = Path(tmp_path, "08_projects", f"project_{project_id}", "project_plan.json")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["visual_timeline"]["items"][0]["text"] = "changed visual text"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    changed = compile_render_manifest(cfg, db, project_id)
    assert changed["manifest_hash"] != first["manifest_hash"]


def test_build_manifest_does_not_write_snapshot(tmp_path):
    cfg, db, project_id = _project(tmp_path, count=1)
    path = Path(tmp_path, "08_projects", f"project_{project_id}", "render_manifest.json")
    assert not path.exists()
    manifest = build_render_manifest(cfg, db, project_id)
    assert manifest["manifest_hash"]
    assert not path.exists()


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda m: m["segments"][0].update({"segment_id": m["segments"][1]["segment_id"]}), "segment_id"),
        (lambda m: m["segments"][1].update({"order": 1}), "order"),
        (lambda m: m["segments"][0].update({"source_out_seconds": 0}), "source_out_seconds"),
        (lambda m: m["segments"][0].update({"speed": 0}), "speed"),
        (lambda m: m["segments"][0].update({"audio_role": "unknown"}), "audio_role"),
    ],
)
def test_manifest_validation_rejects_invalid_contract(tmp_path, change, message):
    cfg, db, project_id = _project(tmp_path, count=1)
    manifest = compile_render_manifest(cfg, db, project_id)
    if message == "segment_id":
        manifest["segments"].append(dict(manifest["segments"][0], order=2))
    change(manifest)
    result = validate_render_manifest(manifest)
    assert not result["valid"]
    assert any(message in error for error in result["errors"])


def test_manifest_validation_rejects_duration_mismatch_and_profile_tampering(tmp_path):
    cfg, db, project_id = _project(tmp_path, count=1)
    manifest = compile_render_manifest(cfg, db, project_id)
    manifest["segments"][0]["source_duration_seconds"] += 1
    result = validate_render_manifest(manifest)
    assert not result["valid"]
    assert any("source_duration_seconds" in error for error in result["errors"])

    manifest = compile_render_manifest(cfg, db, project_id)
    manifest["profile"]["width"] = 640
    result = validate_render_manifest(manifest)
    assert not result["valid"]
    assert any("profile width" in error for error in result["errors"])


def test_schema_file_is_json_and_declares_manifest_fields():
    schema = json.loads(Path(__file__).parents[1].joinpath("schemas", "render_manifest.schema.json").read_text(encoding="utf-8"))
    assert schema["properties"]["schema_version"]["const"] == "2.0"
    assert "segments" in schema["required"]


def test_render_manifest_contains_effective_segment_color(tmp_path):
    cfg, db, project_id = _project(tmp_path, count=1)
    detail = project_detail(cfg, db, project_id)
    segment_id = detail["segments"][0]["segment_id"]
    state = default_color_state()
    state["applied"].update({"mode": "manual", "exposure": 0.2})
    state["segments"][segment_id] = {"enabled": True, "locked": True, "excluded": False, "applied": {"mode": "manual", "exposure": -0.3}}
    save_project_color_state(cfg, db, project_id, state, mark_review=False)
    manifest = build_render_manifest(cfg, db, project_id)
    assert manifest["segments"][0]["color"]["mode"] == "manual"
    assert manifest["segments"][0]["color"]["exposure"] == -0.3
    assert manifest["segments"][0]["color"]["effective_source"] == "manual"


def test_analysis_suggestion_is_advisory_until_manual_override(tmp_path):
    cfg, db, project_id = _project(tmp_path, count=1)
    detail = project_detail(cfg, db, project_id)
    segment_id = detail["segments"][0]["segment_id"]
    state = default_color_state()
    state["applied"].update({"mode": "safe_restore", "exposure": -0.2})
    state["segment_analysis"][segment_id] = {
        "suggested": {"mode": "manual", "exposure": 0.8},
        "confidence": 0.9,
        "warnings": [],
    }
    state["segment_overrides"][segment_id] = {"enabled": True, "locked": False, "excluded": False}
    save_project_color_state(cfg, db, project_id, state, mark_review=False)

    analysis_only = build_render_manifest(cfg, db, project_id)["segments"][0]["color"]
    assert analysis_only["mode"] == "safe_restore"
    assert analysis_only["exposure"] == -0.2
    assert analysis_only["effective_source"] == "project"

    state["segment_overrides"][segment_id]["applied"] = {"mode": "manual", "exposure": 0.35}
    save_project_color_state(cfg, db, project_id, state, mark_review=False)
    manual = build_render_manifest(cfg, db, project_id)["segments"][0]["color"]
    assert manual["mode"] == "manual"
    assert manual["exposure"] == 0.35
    assert manual["effective_source"] == "manual"


def test_manifest_validation_rejects_missing_lut_file(tmp_path):
    cfg, db, project_id = _project(tmp_path, count=1)
    manifest = compile_render_manifest(cfg, db, project_id)
    manifest["settings"]["color"] = {"mode": "dji_lut", "lut_path": str(tmp_path / "missing.cube")}
    result = validate_render_manifest(manifest)
    assert not result["valid"]
    assert any("color LUT file does not exist" in error for error in result["errors"])

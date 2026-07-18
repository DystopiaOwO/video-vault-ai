import json
from pathlib import Path

from video_vault.database import add_analysis, add_bgm_track, add_project_bgm, init_db, upsert_video
from video_vault.project import build_project_plan, can_project_render, create_project, project_detail, project_dir, set_review_status
from video_vault.render_manifest import compile_render_manifest, manifest_hash
from video_vault.render_settings import default_render_settings, load_render_settings, save_render_settings


def _approved_project(tmp_path: Path) -> tuple[dict, Path, int]:
    db = tmp_path / "db.sqlite3"
    init_db(db)
    source = tmp_path / "20260718_081500_coffee.mp4"
    source.write_bytes(b"video")
    video_id = upsert_video(db, {"original_path": str(source), "current_path": str(source), "filename": source.name, "category": "coffee", "duration_seconds": 10})
    add_analysis(db, video_id, "mock", "rules", {"segments": [{"start_seconds": 1, "end_seconds": 5, "segment_type": "key_action", "title": "pour", "reason": "ok", "tags": ["coffee"], "score": 1, "suggested_use": "main"}]}, tmp_path / "raw.json")
    cfg = {"library_root": str(tmp_path)}
    project_id = create_project(db, "settings", [video_id], category="coffee")
    build_project_plan(cfg, db, project_id)
    set_review_status(cfg, db, project_id, "approved")
    return cfg, db, project_id


def test_default_settings_and_save_invalidate_approval(tmp_path):
    cfg, db, project_id = _approved_project(tmp_path)
    settings = load_render_settings(cfg, project_id)
    assert settings == default_render_settings()
    assert can_project_render(cfg, db, project_id)[0] is True

    path = save_render_settings(cfg, db, project_id, {"profile_id": "accurate_preview_1080p", "audio": {"bgm_gain_db": -12}})
    assert path.name == "render_settings.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["profile_id"] == "accurate_preview_1080p"
    assert saved["audio"]["bgm_gain_db"] == -12.0
    review = json.loads(project_dir(cfg, project_id).joinpath("review_status.json").read_text(encoding="utf-8"))
    assert review["approved_by_user"] is False
    assert "approved_manifest_hash" not in review
    assert can_project_render(cfg, db, project_id)[0] is False


def test_approval_captures_manifest_snapshot(tmp_path):
    cfg, db, project_id = _approved_project(tmp_path)
    review_path = project_dir(cfg, project_id) / "review_status.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    manifest = json.loads((project_dir(cfg, project_id) / "render_manifest.json").read_text(encoding="utf-8"))
    assert review["approved_by_user"] is True
    assert review["approved_manifest_hash"] == manifest["manifest_hash"]
    assert review["approved_plan_id"] == manifest["plan_id"]
    assert review["approved_at"]


def test_gate_rebuilds_current_review_and_settings_after_direct_edits(tmp_path):
    cfg, db, project_id = _approved_project(tmp_path)
    assert can_project_render(cfg, db, project_id)[0] is True

    review_path = project_dir(cfg, project_id) / "feedback" / "segment_review.json"
    rows = project_detail(cfg, db, project_id)["segments"]
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(json.dumps([{"segment_id": rows[0]["segment_id"], "speed": 2.0}], indent=2), encoding="utf-8")
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review[0]["speed"] = 2.0
    review_path.write_text(json.dumps(review, indent=2), encoding="utf-8")
    ok, reason = can_project_render(cfg, db, project_id)
    assert not ok
    assert reason == "approved_manifest_hash 已失效"

    cfg, db, project_id = _approved_project(tmp_path / "settings")
    settings_path = project_dir(cfg, project_id) / "render_settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else default_render_settings()
    settings["encoder"] = "cpu"
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    ok, reason = can_project_render(cfg, db, project_id)
    assert not ok
    assert reason == "approved_manifest_hash 已失效"


def test_gate_rebuilds_current_bgm_after_direct_project_change(tmp_path):
    cfg, db, project_id = _approved_project(tmp_path)
    track_id = add_bgm_track(db, {"title": "Travel", "artist": "", "file_path": str(tmp_path / "travel.mp3"), "source_url": "https://example.test/music", "license_name": "CC", "attribution_text": "Credit"})
    add_project_bgm(db, project_id, track_id)
    ok, reason = can_project_render(cfg, db, project_id)
    assert not ok
    assert reason == "approved_manifest_hash 已失效"


def test_gate_fails_closed_when_current_manifest_cannot_be_built(tmp_path):
    cfg, db, project_id = _approved_project(tmp_path)
    settings_path = project_dir(cfg, project_id) / "render_settings.json"
    settings = default_render_settings()
    settings["color"] = {"mode": "dji_lut", "lut_path": ""}
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    ok, reason = can_project_render(cfg, db, project_id)
    assert not ok
    assert reason.startswith("目前 Manifest 無法建立：")


def test_manifest_hash_excludes_metadata_but_includes_render_inputs(tmp_path):
    cfg, db, project_id = _approved_project(tmp_path)
    manifest = compile_render_manifest(cfg, db, project_id)
    manifest["bgm"] = [{
        "track_id": 1,
        "title": "Test BGM",
        "source_path": "C:/music/test.mp3",
        "source_url": "https://example.test/music",
        "license_name": "CC",
        "attribution_text": "Credit",
        "gain_db": -18.0,
        "loop": True,
        "fade_in_seconds": 1.0,
        "fade_out_seconds": 2.0,
    }]
    baseline = manifest_hash(manifest)
    metadata_only = dict(manifest, created_at="2099-01-01T00:00:00Z", validation={"valid": False}, manifest_hash="tampered")
    assert manifest_hash(metadata_only) == baseline

    mutations = [
        ("segment range", lambda value: value["segments"][0].update({"source_in_seconds": 1.5})),
        ("segment order", lambda value: value["segments"][0].update({"order": 2})),
        ("speed", lambda value: value["segments"][0].update({"speed": 2.0})),
        ("audio role", lambda value: value["segments"][0].update({"audio_role": "mute"})),
        ("profile", lambda value: value["settings"].update({"profile_id": "accurate_preview_1080p"})),
        ("encoder", lambda value: value["settings"].update({"encoder": "cpu"})),
        ("color mode", lambda value: value["settings"]["color"].update({"mode": "safe_restore"})),
        ("lut", lambda value: value["settings"]["color"].update({"lut_path": "C:/LUT/test.cube"})),
        ("audio settings", lambda value: value["settings"]["audio"].update({"bgm_gain_db": -12})),
        ("bgm track", lambda value: value["bgm"][0].update({"track_id": 2})),
        ("bgm gain", lambda value: value["bgm"][0].update({"gain_db": -12})),
        ("bgm loop", lambda value: value["bgm"][0].update({"loop": False})),
        ("transition", lambda value: value["settings"]["transition"].update({"duration_seconds": 1})),
        ("overlay", lambda value: value["settings"]["overlay"].update({"enabled": True})),
    ]
    for _, mutate in mutations:
        changed = json.loads(json.dumps(manifest))
        mutate(changed)
        assert manifest_hash(changed) != baseline

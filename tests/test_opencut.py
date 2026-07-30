from pathlib import Path

import csv
import json
import pytest
from types import SimpleNamespace

from video_vault.database import add_analysis, init_db, upsert_video
from video_vault.opencut import export_opencut_handoff
from video_vault.project import build_project_plan, can_project_render, create_project, project_detail, save_segment_review, set_review_status
from video_vault.storyboard import generate_storyboard


def test_opencut_handoff_writes_manifest(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video = tmp_path / "20260617_081500_coffee.mp4"
    video.write_bytes(b"v")
    video_id = upsert_video(db, {"original_path": str(video), "current_path": str(video), "filename": video.name, "category": "coffee", "duration_seconds": 10})
    add_analysis(db, video_id, "mock", "rules", {"segments": [{"start_seconds": 0, "end_seconds": 5, "segment_type": "key_action", "title": "pour", "reason": "ok", "tags": ["coffee"], "score": 1, "suggested_use": "main"}]}, tmp_path / "raw.json")
    project_id = create_project(db, "coffee", [video_id], category="coffee")

    out = export_opencut_handoff({"library_root": str(tmp_path)}, db, project_id)

    assert Path(out, "README.md").exists()
    assert Path(out, "recommended_segments.csv").exists()
    assert Path(out, "opencut_handoff.json").exists()


def test_needs_review_project_cannot_render_opencut_graded_clips(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video = tmp_path / "20260617_081500_coffee.mp4"
    video.write_bytes(b"v")
    video_id = upsert_video(db, {"original_path": str(video), "current_path": str(video), "filename": video.name, "category": "coffee", "duration_seconds": 10})
    add_analysis(db, video_id, "mock", "rules", {"segments": [{"start_seconds": 0, "end_seconds": 5, "segment_type": "key_action", "title": "pour", "reason": "ok", "tags": ["coffee"], "score": 1, "suggested_use": "main"}]}, tmp_path / "raw.json")
    project_id = create_project(db, "coffee", [video_id], category="coffee")

    with pytest.raises(PermissionError):
        export_opencut_handoff({"library_root": str(tmp_path)}, db, project_id, render_clips=True)

    out = export_opencut_handoff({"library_root": str(tmp_path)}, db, project_id, render_clips=False)
    assert Path(out, "opencut_handoff.json").exists()
    assert not list(Path(out, "graded_clips").glob("*.mp4"))


def test_opencut_handoff_uses_segment_review(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video = tmp_path / "20260617_081500_coffee.mp4"
    video.write_bytes(b"v")
    video_id = upsert_video(db, {"original_path": str(video), "current_path": str(video), "filename": video.name, "category": "coffee", "duration_seconds": 10})
    add_analysis(
        db,
        video_id,
        "mock",
        "rules",
        {"segments": [
            {"start_seconds": 0, "end_seconds": 5, "segment_type": "key_action", "title": "first", "reason": "ok", "tags": ["coffee"], "score": 1, "suggested_use": "main"},
            {"start_seconds": 6, "end_seconds": 8, "segment_type": "key_action", "title": "second", "reason": "ok", "tags": ["coffee"], "score": 1, "suggested_use": "main"},
        ]},
        tmp_path / "raw.json",
    )
    cfg = {"library_root": str(tmp_path)}
    project_id = create_project(db, "coffee", [video_id], category="coffee")
    build_project_plan(cfg, db, project_id)
    rows = project_detail(cfg, db, project_id)["segments"]
    rows[1]["start_seconds"] = 6.5
    rows[1]["end_seconds"] = 7.5
    rows[0]["include"] = False
    save_segment_review(cfg, db, project_id, [rows[1], rows[0]])

    out = export_opencut_handoff(cfg, db, project_id)
    data = json.loads(Path(out, "opencut_handoff.json").read_text(encoding="utf-8"))

    assert [seg["title"] for seg in data["segments"]] == ["second"]
    assert data["segments"][0]["start_seconds"] == 6.5
    assert data["segments"][0]["end_seconds"] == 7.5


def test_opencut_preview_keeps_approved_project_approved(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video = tmp_path / "20260617_081500_coffee.mp4"
    video.write_bytes(b"v")
    video_id = upsert_video(db, {"original_path": str(video), "current_path": str(video), "filename": video.name, "category": "coffee", "duration_seconds": 10})
    add_analysis(db, video_id, "mock", "rules", {"segments": [{"start_seconds": 0, "end_seconds": 5, "segment_type": "key_action", "title": "pour", "reason": "ok", "tags": ["coffee"], "score": 1, "suggested_use": "main"}]}, tmp_path / "raw.json")
    cfg = {"library_root": str(tmp_path)}
    project_id = create_project(db, "coffee", [video_id], category="coffee")
    build_project_plan(cfg, db, project_id)
    generate_storyboard(cfg, db, project_id)
    set_review_status(cfg, db, project_id, "approved")

    export_opencut_handoff(cfg, db, project_id, render_clips=False)

    assert can_project_render(cfg, db, project_id)[0] is True


def test_formal_opencut_uses_canonical_source_range_and_nested_manifest_paths(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    rendered = tmp_path / "rendered.mp4"
    rendered.write_bytes(b"rendered")
    segment = {
        "segment_id": "segment-1",
        "stable_id": "segment-1",
        "clip_id": "clip-1",
        "source_file": str(source),
        "source_in_seconds": 1.25,
        "source_out_seconds": 3.75,
        "timeline_duration_seconds": 2.5,
        "speed": 1,
        "title": "canonical",
        "group": "day",
    }
    delivery = {
        "contract_version": "handoff-v1",
        "handoff_id": "handoff-formal",
        "handoff_type": "formal",
        "created_at": "2026-07-28T00:00:00+00:00",
        "timeline_items": [segment],
        "exported_ids": ["segment-1"],
        "files": [],
        "file_hashes": {},
        "contract_hash": "initial",
    }
    approved_manifest = {
        "project_name": "formal",
        "segments": [segment],
        "bgm": [],
    }
    monkeypatch.setattr("video_vault.opencut.assert_project_approved", lambda *args: None)
    monkeypatch.setattr(
        "video_vault.opencut.build_handoff_manifest",
        lambda *args, **kwargs: json.loads(json.dumps(delivery)),
    )
    monkeypatch.setattr(
        "video_vault.opencut.load_approved_handoff_snapshot",
        lambda *args: {"manifest": approved_manifest},
    )
    monkeypatch.setattr(
        "video_vault.opencut.render_segment",
        lambda *args: SimpleNamespace(output_path=rendered, cache_key="cache-1"),
    )

    out = export_opencut_handoff(
        {"library_root": str(tmp_path)},
        tmp_path / "db.sqlite3",
        1,
        render_clips=True,
        mode="complete",
    )

    graded = next((out / "graded_clips").glob("*.mp4"))
    assert "_00012_" in graded.name
    with (out / "recommended_segments.csv").open(
        newline="", encoding="utf-8-sig"
    ) as stream:
        row = next(csv.DictReader(stream))
    assert row["start_seconds"] == "1.25"
    assert row["end_seconds"] == "3.75"
    manifest = json.loads((out / "handoff_manifest.json").read_text(encoding="utf-8"))
    assert any(
        item["path"].startswith("graded_clips/")
        for item in manifest["files"]
    )

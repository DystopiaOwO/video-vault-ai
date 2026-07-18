from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from video_vault.database import add_analysis, init_db, upsert_video
from video_vault.opencut import export_opencut_handoff
from video_vault.project import build_project_plan, create_project, project_dir


def _project(tmp_path: Path) -> tuple[dict, Path, int]:
    db = tmp_path / "db.sqlite3"
    init_db(db)
    source = tmp_path / "20260718_120000_travel.mp4"
    source.write_bytes(b"video")
    video_id = upsert_video(db, {
        "original_path": str(source), "current_path": str(source),
        "filename": source.name, "category": "travel", "duration_seconds": 20,
    })
    add_analysis(db, video_id, "mock", "rules", {"segments": [{
        "start_seconds": 0, "end_seconds": 10, "segment_type": "scene",
        "title": "old", "reason": "ok", "tags": ["travel"],
        "score": 0.5, "suggested_use": "B-roll",
    }]}, tmp_path / "raw.json")
    cfg = {"library_root": str(tmp_path)}
    return cfg, db, create_project(db, "travel", [video_id], category="travel", content_type="travel_diary")


def test_opencut_manifest_controls_manual_order_and_review_fields(tmp_path: Path):
    cfg, db, project_id = _project(tmp_path)
    build_project_plan(cfg, db, project_id)
    project_dir(cfg, project_id).joinpath("render_manifest.json").write_text(json.dumps({
        "schema_version": "1.0", "manifest_hash": "abc123",
        "plan_id": "travel_v001", "project_id": str(project_id),
        "segments": [
            {"segment_id": "second", "source_file": str(tmp_path / "20260718_120000_travel.mp4"),
             "source_in_ms": 7000, "source_out_ms": 9000, "manual_order": 2,
             "include": True, "speed": 0.5, "audio_role": "lower_original", "title": "第二段"},
            {"segment_id": "excluded", "source_file": str(tmp_path / "20260718_120000_travel.mp4"),
             "source_in_ms": 0, "source_out_ms": 1000, "manual_order": 1,
             "include": False, "speed": 1, "audio_role": "mute", "title": "不要用"},
            {"segment_id": "first", "source_file": str(tmp_path / "20260718_120000_travel.mp4"),
             "source_in_ms": 2000, "source_out_ms": 4000, "manual_order": 1,
             "include": True, "speed": 2, "audio_role": "keep_original", "title": "第一段"},
        ],
    }, ensure_ascii=False), encoding="utf-8")

    out = export_opencut_handoff(cfg, db, project_id)
    data = json.loads((out / "opencut_handoff.json").read_text(encoding="utf-8"))
    segments = data["segments"]

    assert [item["clip_id"] for item in segments] == ["first", "second"]
    assert segments[0]["start_seconds"] == 2.0
    assert segments[0]["end_seconds"] == 4.0
    assert segments[0]["speed"] == 2.0
    assert segments[1]["timeline_duration_seconds"] == 4.0
    assert data["manifest"]["manifest_hash"] == "abc123"

    with (out / "recommended_segments.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["order"] for row in rows] == ["1", "2"]
    assert rows[1]["audio_role"] == "lower_original"


def test_opencut_graded_clips_require_project_approval(tmp_path: Path):
    cfg, db, project_id = _project(tmp_path)
    build_project_plan(cfg, db, project_id)

    with pytest.raises(PermissionError, match="approval gate"):
        export_opencut_handoff(cfg, db, project_id, render_clips=True)

    handoff = export_opencut_handoff(cfg, db, project_id, render_clips=False)
    assert (handoff / "opencut_handoff.json").exists()
    assert not (handoff / "graded_clips").exists()


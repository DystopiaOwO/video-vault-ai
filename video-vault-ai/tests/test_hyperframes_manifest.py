from __future__ import annotations

import json
from pathlib import Path

from video_vault.database import add_analysis, init_db, upsert_video
from video_vault.hyperframes import export_hyperframes_project
from video_vault.project import create_project, project_dir


def test_hyperframes_uses_manifest_source_order_duration_and_overlay(tmp_path: Path):
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
        "title": "fallback", "reason": "ok", "tags": ["travel"],
        "score": 0.2, "suggested_use": "B-roll",
    }]}, tmp_path / "raw.json")
    cfg = {"library_root": str(tmp_path)}
    project_id = create_project(db, "travel", [video_id], category="travel", content_type="travel_diary")
    project_dir(cfg, project_id).joinpath("render_manifest.json").write_text(json.dumps({
        "schema_version": "1.0", "manifest_hash": "manifest-hash",
        "plan_id": "travel_v001", "project_id": str(project_id),
        "overlays": [{"timeline_start_ms": 0, "duration": 1.5, "text": "南港車站"}],
        "segments": [{
            "segment_id": "station", "source_file": str(source),
            "source_in_ms": 2500, "source_out_ms": 7500, "manual_order": 1,
            "include": True, "speed": 2.0, "title": "車站", "suggested_use": "開場",
        }],
    }, ensure_ascii=False), encoding="utf-8")

    out = export_hyperframes_project(cfg, db, project_id, render_clips=False)
    timeline = json.loads((out / "timeline.json").read_text(encoding="utf-8"))
    html = (out / "index.html").read_text(encoding="utf-8")

    clip = timeline["clips"][0]
    assert clip["source_in"] == 2.5
    assert clip["source_out"] == 7.5
    assert clip["duration"] == 2.5
    assert timeline["manifest"]["manifest_hash"] == "manifest-hash"
    assert 'data-media-start="2.5"' in html
    assert "南港車站" in html


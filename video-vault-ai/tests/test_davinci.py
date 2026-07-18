import re
import sys

from video_vault.cli import davinci_export, main
from video_vault.database import add_analysis, init_db, upsert_video
from video_vault.davinci import build_timeline_plan, build_timeline_plans
from video_vault.davinci.export_formats import export_all
from video_vault.davinci.resolve_api import create_timeline


def _db(tmp_path):
    db = tmp_path / "video_vault.sqlite3"
    init_db(db)
    video_id = upsert_video(
        db,
        {
            "original_path": "raw.mp4",
            "current_path": "sorted.mp4",
            "filename": "sorted.mp4",
            "category": "coffee",
        },
    )
    add_analysis(
        db,
        video_id,
        "mock",
        "rules",
        {
            "segments": [
                {"start_seconds": 10, "end_seconds": 20, "segment_type": "b_roll", "title": "B", "reason": "ok", "tags": [], "score": 0.5, "suggested_use": "B-roll"},
                {"start_seconds": 1, "end_seconds": 5, "segment_type": "shorts", "title": "A", "reason": "ok", "tags": [], "score": 0.9, "suggested_use": "Shorts"},
            ]
        },
        tmp_path / "raw.json",
    )
    return db


def test_build_timeline_plan_from_sqlite(tmp_path):
    plan = build_timeline_plan(_db(tmp_path))
    assert re.match(r"\d{8}_coffee_sorted_short_v1", plan["timeline_name"])
    assert plan["clips"][0]["title"] == "A"
    assert plan["clips"][0]["source_file"] == "sorted.mp4"
    assert plan["clips"][0]["suggested_use"] == "Shorts"


def test_export_json_edl_xml(tmp_path):
    plan = build_timeline_plan(_db(tmp_path))
    files = export_all(plan, tmp_path / "davinci")
    assert "timeline_name" in files["json"].read_text(encoding="utf-8")
    assert "TITLE:" in files["edl"].read_text(encoding="utf-8")
    assert "<timeline" in files["xml"].read_text(encoding="utf-8")


def test_resolve_unavailable_is_skipped(monkeypatch):
    monkeypatch.setitem(sys.modules, "DaVinciResolveScript", None)
    result = create_timeline({"timeline_name": "x", "clips": []})
    assert result["status"] == "skipped"


def test_davinci_export_dry_run_writes_nothing(tmp_path):
    cfg = {"library_root": str(tmp_path)}
    result = davinci_export(cfg, _db(tmp_path), dry_run=True)
    assert result["timelines"][0]["plan"]["clips"]
    assert not (tmp_path / "08_projects").exists()


def test_davinci_cli_dry_run_does_not_create_db(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f'library_root: "{tmp_path}"\n', encoding="utf-8")
    main(["--config", str(cfg), "davinci-export", "--dry-run"])
    assert not (tmp_path / "05_index").exists()


def test_davinci_plans_are_per_video(tmp_path):
    db = _db(tmp_path)
    other = upsert_video(db, {"original_path": "raw2.mp4", "current_path": "other.mp4", "filename": "other.mp4", "category": "coffee"})
    add_analysis(db, other, "mock", "rules", {"segments": [{"start_seconds": 1, "end_seconds": 2, "segment_type": "shorts", "title": "C", "reason": "ok", "tags": [], "score": 1, "suggested_use": "Shorts"}]}, tmp_path / "raw2.json")
    plans = build_timeline_plans(db)
    assert len(plans) == 2
    assert {plan["clips"][0]["source_file"] for plan in plans} == {"sorted.mp4", "other.mp4"}

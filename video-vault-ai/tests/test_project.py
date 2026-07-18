from pathlib import Path

from video_vault.database import add_analysis, init_db, upsert_video
from video_vault.project import build_project_plan, create_project, project_detail


def test_project_has_own_source_and_clip_files(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    a = tmp_path / "20260617_081500_coffee_a.mp4"
    b = tmp_path / "20260617_123000_food_b.mp4"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    v1 = upsert_video(db, {"original_path": str(a), "current_path": str(a), "filename": a.name, "category": "coffee", "duration_seconds": 10})
    v2 = upsert_video(db, {"original_path": str(b), "current_path": str(b), "filename": b.name, "category": "travel", "duration_seconds": 12})
    for video_id, tags in [(v1, ["coffee", "hands"]), (v2, ["food", "travel"])]:
        add_analysis(db, video_id, "mock", "rules", {"segments": [{"start_seconds": 0, "end_seconds": 5, "segment_type": "key_action", "title": "x", "reason": "ok", "tags": tags, "score": 1, "suggested_use": "main"}]}, tmp_path / f"{video_id}.json")

    project_id = create_project(db, "測試專案", [v1, v2], category="mixed")
    plan = build_project_plan({"library_root": str(tmp_path)}, db, project_id)
    detail = project_detail({"library_root": str(tmp_path)}, db, project_id)

    assert len(plan["clips"]) == 2
    assert "上午 / 飲食" in [g["label"] for g in plan["groups"]]
    assert "中午 / 飲食" in [g["label"] for g in plan["groups"]]
    assert Path(detail["folder"], "source").exists()
    assert Path(detail["folder"], "clips", "clip_001", "clip.json").exists()
    assert all("08_projects" in seg["source_file"] for group in plan["groups"] for seg in group["segments"])


def test_project_clip_status_uses_video_status_without_segments(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video_path = tmp_path / "20260617_081500_coffee_a.mp4"
    video_path.write_bytes(b"a")
    video_id = upsert_video(db, {"original_path": str(video_path), "current_path": str(video_path), "filename": video_path.name, "category": "coffee", "duration_seconds": 10, "status": "perceived"})

    project_id = create_project(db, "測試專案", [video_id], category="coffee")
    detail = project_detail({"library_root": str(tmp_path)}, db, project_id)

    assert detail["clips"][0]["status"] == "perceived"
    assert detail["clips"][0]["segment_count"] == 0


def test_project_title_cards_are_only_scene_changes(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video_path = tmp_path / "20260617_123000_food_b.mp4"
    video_path.write_bytes(b"b")
    video_id = upsert_video(db, {"original_path": str(video_path), "current_path": str(video_path), "filename": video_path.name, "category": "travel", "duration_seconds": 12})
    add_analysis(db, video_id, "mock", "rules", {"segments": [{"start_seconds": 0, "end_seconds": 5, "segment_type": "key_action", "title": "x", "reason": "ok", "tags": ["food"], "score": 1, "suggested_use": "main"}]}, tmp_path / "raw.json")

    project_id = create_project(db, "測試專案", [video_id], category="travel", content_type="travel_diary")
    plan = build_project_plan({"library_root": str(tmp_path)}, db, project_id)

    assert plan["title_cards"] == [{"where": "中午 / 飲食 第一段前", "text": "中午｜用餐/咖啡", "style": "地點/場景字卡，左下角，1.5 秒"}]

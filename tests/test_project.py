from pathlib import Path

from video_vault.database import add_analysis, add_bgm_track, add_frame, frames as db_frames, init_db, update_frame_analysis, update_video_summary, upsert_video
from video_vault.project import build_project_plan, can_project_render, create_project, pre_render_validation, project_detail, save_revision_notes, save_segment_review, set_review_status


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


def test_project_render_gate_requires_db_review_and_plan_approval(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video_path = tmp_path / "20260617_081500_coffee_a.mp4"
    video_path.write_bytes(b"a")
    video_id = upsert_video(db, {"original_path": str(video_path), "current_path": str(video_path), "filename": video_path.name, "category": "coffee", "duration_seconds": 10})
    add_analysis(db, video_id, "mock", "rules", {"segments": [{"start_seconds": 0, "end_seconds": 5, "segment_type": "key_action", "title": "x", "reason": "ok", "tags": ["coffee"], "score": 1, "suggested_use": "main"}]}, tmp_path / "raw.json")
    cfg = {"library_root": str(tmp_path)}
    project_id = create_project(db, "測試專案", [video_id], category="coffee")

    build_project_plan(cfg, db, project_id)
    ok, reason = can_project_render(cfg, db, project_id)
    assert not ok
    assert "尚未核准" in reason

    set_review_status(cfg, db, project_id, "approved")
    ok, reason = can_project_render(cfg, db, project_id)
    assert ok
    assert reason == "approved"

    plan_path = tmp_path / "08_projects" / f"project_{project_id}" / "project_plan.json"
    plan_path.write_text(plan_path.read_text(encoding="utf-8").replace('"status": "approved"', '"status": "needs_review"'), encoding="utf-8")
    ok, reason = can_project_render(cfg, db, project_id)
    assert not ok
    assert "project_plan.json" in reason


def test_revision_notes_flow_into_project_plan_and_script(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video_path = tmp_path / "20260617_081500_coffee_a.mp4"
    video_path.write_bytes(b"a")
    video_id = upsert_video(db, {"original_path": str(video_path), "current_path": str(video_path), "filename": video_path.name, "category": "coffee", "duration_seconds": 10})
    add_analysis(db, video_id, "mock", "rules", {"segments": [{"start_seconds": 0, "end_seconds": 5, "segment_type": "key_action", "title": "x", "reason": "ok", "tags": ["coffee"], "score": 1, "suggested_use": "main"}]}, tmp_path / "raw.json")
    cfg = {"library_root": str(tmp_path)}
    project_id = create_project(db, "測試專案", [video_id], category="coffee")

    save_revision_notes(cfg, project_id, "咖啡廳外觀移到剛到咖啡廳")
    plan = build_project_plan(cfg, db, project_id)
    script = (tmp_path / "08_projects" / f"project_{project_id}" / "project_script.md").read_text(encoding="utf-8")

    assert plan["revision_notes"] == "咖啡廳外觀移到剛到咖啡廳"
    assert plan["feedback_unresolved"]
    assert "## 審核備註" in script
    assert "咖啡廳外觀移到剛到咖啡廳" in script


def test_project_plan_build_keeps_version_history(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video_path = tmp_path / "20260617_081500_coffee_a.mp4"
    video_path.write_bytes(b"a")
    video_id = upsert_video(db, {"original_path": str(video_path), "current_path": str(video_path), "filename": video_path.name, "category": "coffee", "duration_seconds": 10})
    add_analysis(db, video_id, "mock", "rules", {"segments": [{"start_seconds": 0, "end_seconds": 5, "segment_type": "key_action", "title": "x", "reason": "ok", "tags": ["coffee"], "score": 1, "suggested_use": "main"}]}, tmp_path / "raw.json")
    cfg = {"library_root": str(tmp_path)}
    project_id = create_project(db, "測試專案", [video_id], category="coffee")

    first = build_project_plan(cfg, db, project_id)
    second = build_project_plan(cfg, db, project_id)
    folder = tmp_path / "08_projects" / f"project_{project_id}"
    latest = (folder / "plans" / "latest.json").read_text(encoding="utf-8")

    assert first["plan_id"] == "diary_montage_v001"
    assert second["plan_id"] == "diary_montage_v002"
    assert (folder / "plans" / "diary_montage_v001.json").exists()
    assert (folder / "plans" / "diary_montage_v002.md").exists()
    assert "diary_montage_v002" in latest


def test_project_plan_records_pipeline_and_writes_log_files(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video_path = tmp_path / "20260617_081500_coffee_a.mp4"
    video_path.write_bytes(b"a")
    video_id = upsert_video(db, {"original_path": str(video_path), "current_path": str(video_path), "filename": video_path.name, "category": "coffee", "duration_seconds": 10})
    add_analysis(db, video_id, "mock", "rules", {"segments": [{"start_seconds": 0, "end_seconds": 5, "segment_type": "key_action", "title": "x", "reason": "ok", "tags": ["coffee"], "score": 1, "suggested_use": "main"}]}, tmp_path / "raw.json")
    cfg = {"library_root": str(tmp_path)}
    project_id = create_project(db, "測試專案", [video_id], category="coffee")

    plan = build_project_plan(cfg, db, project_id)
    folder = tmp_path / "08_projects" / f"project_{project_id}"

    assert plan["pipeline_id"] == "coffee_diary"
    assert plan["pipeline"]["requires_approval_before"] == ["render", "opencut_render_clips", "hyperframes_mp4"]
    assert (folder / "decisions" / "decision_log.jsonl").exists()
    assert (folder / "checkpoints" / "plan_created.json").exists()


def test_pre_render_validation_writes_report(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video_path = tmp_path / "20260617_081500_coffee_a.mp4"
    video_path.write_bytes(b"a")
    video_id = upsert_video(db, {"original_path": str(video_path), "current_path": str(video_path), "filename": video_path.name, "category": "coffee", "duration_seconds": 10})
    add_analysis(db, video_id, "mock", "rules", {"segments": [{"start_seconds": 0, "end_seconds": 5, "segment_type": "key_action", "title": "x", "reason": "ok", "tags": ["coffee"], "score": 1, "suggested_use": "main"}]}, tmp_path / "raw.json")
    cfg = {"library_root": str(tmp_path)}
    project_id = create_project(db, "測試專案", [video_id], category="coffee")

    build_project_plan(cfg, db, project_id)
    set_review_status(cfg, db, project_id, "approved")
    report = pre_render_validation(cfg, db, project_id)
    folder = tmp_path / "08_projects" / f"project_{project_id}"

    assert report["status"] == "passed"
    assert (folder / "validation" / "pre_render_report.json").exists()
    assert (folder / "checkpoints" / "pre_render_validation_passed.json").exists()


def test_project_detail_exposes_segment_review_rows(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video_path = tmp_path / "20260617_081500_coffee_a.mp4"
    video_path.write_bytes(b"a")
    video_id = upsert_video(db, {"original_path": str(video_path), "current_path": str(video_path), "filename": video_path.name, "category": "coffee", "duration_seconds": 10})
    add_analysis(db, video_id, "mock", "rules", {"segments": [{"start_seconds": 0, "end_seconds": 5, "segment_type": "key_action", "title": "pour", "reason": "ok", "tags": ["coffee"], "score": 1, "suggested_use": "main"}]}, tmp_path / "raw.json")
    cfg = {"library_root": str(tmp_path)}
    project_id = create_project(db, "測試專案", [video_id], category="coffee")

    build_project_plan(cfg, db, project_id)
    detail = project_detail(cfg, db, project_id)

    assert detail["segments"][0]["title"] == "pour"
    assert detail["segments"][0]["include"] is True
    assert detail["segments"][0]["scene_role"] == "main_action"


def test_project_detail_exposes_clip_visual_summary(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video_path = tmp_path / "20260617_081500_coffee_a.mp4"
    frame_path = tmp_path / "frame.jpg"
    video_path.write_bytes(b"a")
    frame_path.write_bytes(b"jpg")
    video_id = upsert_video(db, {"original_path": str(video_path), "current_path": str(video_path), "filename": video_path.name, "category": "coffee", "duration_seconds": 10})
    add_frame(db, video_id, frame_path, 0)
    update_frame_analysis(db, int(db_frames(db, video_id)[0]["id"]), {"summary": "手沖咖啡特寫", "tags": ["coffee"], "visual_quality_score": 0.8, "usefulness_score": 0.9})

    project_id = create_project(db, "測試專案", [video_id], category="coffee")
    detail = project_detail({"library_root": str(tmp_path)}, db, project_id)

    assert detail["clips"][0]["visual_summary"] == "手沖咖啡特寫"


def test_manual_clip_summary_updates_user_context_without_mutating_ai_detail(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video_id = upsert_video(db, {"original_path": "a.mp4", "current_path": "a.mp4", "filename": "a.mp4", "category": "coffee"})
    frame1 = tmp_path / "frame.jpg"
    frame2 = tmp_path / "frame2.jpg"
    add_frame(db, video_id, frame1, 0)
    add_frame(db, video_id, frame2, 5)
    update_frame_analysis(db, int(db_frames(db, video_id)[0]["id"]), {"summary": "舊描述 A", "tags": ["coffee"], "visual_quality_score": 0.8, "usefulness_score": 0.9})
    update_frame_analysis(db, int(db_frames(db, video_id)[1]["id"]), {"summary": "舊描述 B", "tags": ["coffee"], "visual_quality_score": 0.8, "usefulness_score": 0.9})
    project_id = create_project(db, "p", [video_id])

    assert update_video_summary(db, video_id, "手動改過的描述")

    detail = project_detail({"library_root": str(tmp_path)}, db, project_id)
    clip = detail["clips"][0]
    assert clip["visual_summary"] == "舊描述 A / 舊描述 B"
    assert clip["ai_visual_summary"] == "舊描述 A / 舊描述 B"
    assert clip["user_summary"] == "手動改過的描述"
    assert clip["effective_summary"] == "手動改過的描述"
    assert clip["effective_summary_source"] == "user"
    assert [row["vision_summary"] for row in db_frames(db, video_id)] == ["舊描述 A", "舊描述 B"]


def test_project_plan_recommends_bgm_per_content_group(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    coffee = tmp_path / "20260617_081500_coffee_a.mp4"
    travel = tmp_path / "20260617_123000_travel_b.mp4"
    coffee.write_bytes(b"a")
    travel.write_bytes(b"b")
    coffee_id = upsert_video(db, {"original_path": str(coffee), "current_path": str(coffee), "filename": coffee.name, "category": "coffee", "duration_seconds": 10})
    travel_id = upsert_video(db, {"original_path": str(travel), "current_path": str(travel), "filename": travel.name, "category": "travel", "duration_seconds": 10})
    add_analysis(db, coffee_id, "mock", "rules", {"segments": [{"start_seconds": 0, "end_seconds": 5, "segment_type": "key_action", "title": "coffee", "reason": "ok", "tags": ["coffee"], "score": 1, "suggested_use": "main"}]}, tmp_path / "coffee.json")
    add_analysis(db, travel_id, "mock", "rules", {"segments": [{"start_seconds": 0, "end_seconds": 5, "segment_type": "b_roll", "title": "view", "reason": "ok", "tags": ["landscape"], "score": 1, "suggested_use": "B-roll"}]}, tmp_path / "travel.json")
    add_bgm_track(db, {"title": "Coffee Lofi", "artist": "", "file_path": str(tmp_path / "coffee.mp3"), "source_url": "x", "license_name": "cc", "mood": "coffee cozy lofi"})
    add_bgm_track(db, {"title": "Travel Vlog", "artist": "", "file_path": str(tmp_path / "travel.mp3"), "source_url": "x", "license_name": "cc", "mood": "travel cinematic vlog"})

    project_id = create_project(db, "測試專案", [coffee_id, travel_id], category="mixed")
    plan = build_project_plan({"library_root": str(tmp_path)}, db, project_id)
    recs = {item["activity"]: item["track"]["title"] for item in plan["bgm_recommendations"]}

    assert recs["飲食"] == "Coffee Lofi"
    assert recs["風景"] == "Travel Vlog"


def test_project_detail_exposes_openmontage_workflow_skeleton(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video_path = tmp_path / "20260617_081500_coffee_a.mp4"
    video_path.write_bytes(b"a")
    video_id = upsert_video(db, {"original_path": str(video_path), "current_path": str(video_path), "filename": video_path.name, "category": "coffee", "duration_seconds": 10})
    add_analysis(db, video_id, "mock", "rules", {"segments": [{"start_seconds": 0, "end_seconds": 5, "segment_type": "key_action", "title": "pour", "reason": "ok", "tags": ["coffee"], "score": 1, "suggested_use": "main"}]}, tmp_path / "raw.json")
    cfg = {"library_root": str(tmp_path)}
    project_id = create_project(db, "測試專案", [video_id], category="coffee")

    build_project_plan(cfg, db, project_id)
    workflow = project_detail(cfg, db, project_id)["workflow"]

    assert workflow["style"] == "openmontage_skeleton"
    assert [stage["id"] for stage in workflow["stages"]] == ["import", "perception", "story", "review", "handoff", "render"]
    assert workflow["stages"][0]["status"] == "done"
    assert workflow["stages"][2]["status"] == "done"


def test_segment_review_overrides_project_detail_rows(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video_path = tmp_path / "20260617_081500_coffee_a.mp4"
    video_path.write_bytes(b"a")
    video_id = upsert_video(db, {"original_path": str(video_path), "current_path": str(video_path), "filename": video_path.name, "category": "coffee", "duration_seconds": 10})
    add_analysis(db, video_id, "mock", "rules", {"segments": [{"start_seconds": 0, "end_seconds": 5, "segment_type": "key_action", "title": "pour", "reason": "ok", "tags": ["coffee"], "score": 1, "suggested_use": "main"}]}, tmp_path / "raw.json")
    cfg = {"library_root": str(tmp_path)}
    project_id = create_project(db, "測試專案", [video_id], category="coffee")

    build_project_plan(cfg, db, project_id)
    segment_id = project_detail(cfg, db, project_id)["segments"][0]["segment_id"]
    save_segment_review(cfg, db, project_id, [{"segment_id": segment_id, "include": False, "user_notes": "不要這段"}])
    detail = project_detail(cfg, db, project_id)

    assert detail["segments"][0]["include"] is False
    assert detail["segments"][0]["user_notes"] == "不要這段"


def test_segment_review_can_adjust_time_range(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video_path = tmp_path / "20260617_081500_coffee_a.mp4"
    video_path.write_bytes(b"a")
    video_id = upsert_video(db, {"original_path": str(video_path), "current_path": str(video_path), "filename": video_path.name, "category": "coffee", "duration_seconds": 10})
    add_analysis(db, video_id, "mock", "rules", {"segments": [{"start_seconds": 0, "end_seconds": 5, "segment_type": "key_action", "title": "pour", "reason": "ok", "tags": ["coffee"], "score": 1, "suggested_use": "main"}]}, tmp_path / "raw.json")
    cfg = {"library_root": str(tmp_path)}
    project_id = create_project(db, "測試專案", [video_id], category="coffee")

    build_project_plan(cfg, db, project_id)
    segment_id = project_detail(cfg, db, project_id)["segments"][0]["segment_id"]
    save_segment_review(cfg, db, project_id, [{"segment_id": segment_id, "start_seconds": 2, "end_seconds": 1}])
    segment = project_detail(cfg, db, project_id)["segments"][0]

    assert segment["start_seconds"] == 2
    assert segment["end_seconds"] == 2.1


def test_segment_review_manual_order_changes_detail_order(tmp_path):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    video_path = tmp_path / "20260617_081500_coffee_a.mp4"
    video_path.write_bytes(b"a")
    video_id = upsert_video(db, {"original_path": str(video_path), "current_path": str(video_path), "filename": video_path.name, "category": "coffee", "duration_seconds": 10})
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
    project_id = create_project(db, "測試專案", [video_id], category="coffee")

    build_project_plan(cfg, db, project_id)
    segments = project_detail(cfg, db, project_id)["segments"]
    save_segment_review(cfg, db, project_id, [segments[1], segments[0]])
    reordered = project_detail(cfg, db, project_id)["segments"]

    assert [row["title"] for row in reordered] == ["second", "first"]

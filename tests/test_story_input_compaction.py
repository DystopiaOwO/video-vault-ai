from __future__ import annotations

import json
from pathlib import Path

from video_vault.database import (
    add_analysis,
    add_frame,
    connect,
    init_db,
    update_frame_analysis,
    upsert_video,
)
from video_vault.project import build_project_plan, create_project, update_segment_evidence
from video_vault.story_input import (
    CLIP_AI_SUMMARY_MAX_ITEMS,
    CLIP_AI_SUMMARY_MAX_UTF8_BYTES,
    build_story_input_snapshot,
)
from video_vault.story_profiles import load_project_story_settings, save_project_story_settings
from video_vault.storyboard import generate_storyboard


def _canonical_bytes(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _fixture(tmp_path: Path, *, segment_count: int, frame_summaries: list[str]):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "db.sqlite3"
    init_db(db)
    source = tmp_path / "coffee.mp4"
    source.write_bytes(b"immutable-source")
    video_id = upsert_video(
        db,
        {
            "original_path": str(source),
            "current_path": str(source),
            "filename": source.name,
            "category": "coffee",
            "duration_seconds": max(30, segment_count * 3),
            "status": "uploaded",
        },
    )
    for index, summary in enumerate(frame_summaries):
        frame_path = tmp_path / f"frame-{index:03d}.jpg"
        add_frame(db, video_id, frame_path, float(index))
        with connect(db) as con:
            frame_id = int(con.execute("select id from frames where frame_path=?", (str(frame_path),)).fetchone()[0])
        update_frame_analysis(
            db,
            frame_id,
            {
                "summary": summary,
                "tags": ["coffee", f"frame-{index}"],
                "visual_quality_score": 0.9,
                "usefulness_score": 0.9,
            },
        )
    segments = [
        {
            "start_seconds": index * 3,
            "end_seconds": index * 3 + 2,
            "segment_type": "scene",
            "title": f"segment visual {index:03d}",
            "reason": f"segment reason {index:03d}",
            "tags": ["coffee", f"step-{index:03d}"],
            "score": 0.9,
            "suggested_use": f"role-{index % 3}",
            "action": f"action-{index:03d}",
            "shot_role": f"shot-role-{index % 4}",
            "technical_quality": {"sharpness": index % 2, "usable": True},
        }
        for index in range(segment_count)
    ]
    add_analysis(db, video_id, "local_multi_image", "fixture-model", {"segments": segments}, tmp_path / "raw.json")
    project_id = create_project(db, "Coffee Story", [video_id], category="coffee", content_type="diary_montage")
    cfg = {"library_root": str(tmp_path), "story": {"provider": "mock"}}
    build_project_plan(cfg, db, project_id)
    generate_storyboard(cfg, db, project_id)
    return cfg, db, project_id, video_id


def test_segments_use_distinct_segment_evidence_without_clip_summary_multiplication(tmp_path: Path):
    frame_summaries = [f"CLIP-WIDE-{index}-" + (chr(65 + index) * 300) for index in range(5)]
    cfg, db, project_id, _ = _fixture(tmp_path, segment_count=4, frame_summaries=frame_summaries)

    snapshot = build_story_input_snapshot(cfg, db, project_id)
    clip_summary = snapshot["clips"][0]["ai_visual_summary"]
    segment_payload = json.dumps(snapshot["segments"], ensure_ascii=False, sort_keys=True)

    assert len(clip_summary.encode("utf-8")) <= CLIP_AI_SUMMARY_MAX_UTF8_BYTES
    assert sum(marker in clip_summary for marker in (f"CLIP-WIDE-{index}-" for index in range(5))) == CLIP_AI_SUMMARY_MAX_ITEMS
    assert "CLIP-WIDE-" not in segment_payload
    assert [segment["visual_summary"] for segment in snapshot["segments"]] == [
        f"segment visual {index:03d}" for index in range(4)
    ]
    assert [segment["action"] for segment in snapshot["segments"]] == [f"action-{index:03d}" for index in range(4)]
    assert len({segment["visual_summary"] for segment in snapshot["segments"]}) == 4
    assert all("ai_visual_summary" not in segment["story_context"] for segment in snapshot["segments"])
    assert snapshot["ordered_segment_uuids"] == [segment["segment_uuid"] for segment in snapshot["segments"]]


def test_story_input_hash_tracks_relevant_segment_and_bounded_clip_changes(tmp_path: Path):
    cfg, db, project_id, video_id = _fixture(
        tmp_path,
        segment_count=4,
        frame_summaries=["first", "second", "third"],
    )
    before = build_story_input_snapshot(cfg, db, project_id)
    assert build_story_input_snapshot(cfg, db, project_id) == before

    late_path = tmp_path / "frame-late.jpg"
    add_frame(db, video_id, late_path, 999.0)
    with connect(db) as con:
        late_id = int(con.execute("select id from frames where frame_path=?", (str(late_path),)).fetchone()[0])
    update_frame_analysis(
        db,
        late_id,
        {
            "summary": "outside the bounded clip evidence",
            "tags": ["late"],
            "visual_quality_score": 1.0,
            "usefulness_score": 1.0,
        },
    )
    bounded_irrelevant = build_story_input_snapshot(cfg, db, project_id)
    assert bounded_irrelevant["input_hash"] == before["input_hash"]

    segment_id = before["segments"][0]["segment_uuid"]
    update_segment_evidence(cfg, db, project_id, segment_id, {"action": "human corrected action"})
    segment_changed = build_story_input_snapshot(cfg, db, project_id)
    assert segment_changed["input_hash"] != before["input_hash"]
    assert segment_changed["segments"][0]["action"] == "human corrected action"

    with connect(db) as con:
        con.execute(
            "update frames set vision_summary='relevant first observation changed' where video_id=? and timestamp_seconds=0",
            (video_id,),
        )
    clip_changed = build_story_input_snapshot(cfg, db, project_id)
    assert clip_changed["input_hash"] != segment_changed["input_hash"]


def test_human_authored_story_context_is_preserved_without_truncation(tmp_path: Path):
    cfg, db, project_id, _ = _fixture(tmp_path, segment_count=3, frame_summaries=["AI observation"])
    user_summary = "人工摘要：" + ("完整保留" * 800)
    with connect(db) as con:
        con.execute("update project_videos set user_summary=? where project_id=?", (user_summary, project_id))
    settings = load_project_story_settings(cfg, db, project_id)
    must_keep = ["必須保留手沖開場", "必須保留完成品"]
    exclude = ["不要使用失焦片段"]
    desired = ["準備", "沖煮", "完成"]
    save_project_story_settings(
        cfg,
        db,
        project_id,
        {
            **settings,
            "project_intent": "呈現完整沖煮過程",
            "desired_sequence": desired,
            "desired_pacing": "沉穩且不要省略等待",
            "must_keep": must_keep,
            "exclude_guidance": exclude,
        },
    )
    initial = build_story_input_snapshot(cfg, db, project_id)
    segment_id = initial["segments"][0]["segment_uuid"]
    user_notes = "人工片段指示：" + ("不得刪除" * 300)
    update_segment_evidence(cfg, db, project_id, segment_id, {"user_notes": user_notes, "locked": True})

    snapshot = build_story_input_snapshot(cfg, db, project_id)
    assert snapshot["clips"][0]["user_summary"] == user_summary
    assert snapshot["clips"][0]["story_context"]["user_summary"] == user_summary
    assert snapshot["project_intent"] == "呈現完整沖煮過程"
    assert snapshot["desired_sequence"] == desired
    assert snapshot["desired_pacing"] == "沉穩且不要省略等待"
    assert snapshot["must_keep"] == must_keep
    assert snapshot["exclude_guidance"] == exclude
    assert snapshot["segments"][0]["human_override"]["user_notes"] == user_notes
    assert snapshot["segments"][0]["human_override"]["locked"] is True


def test_large_story_input_grows_linearly_with_bounded_segment_evidence(tmp_path: Path):
    large_clip_evidence = [f"frame-{index}-" + ("視覺證據" * 800) for index in range(12)]
    cfg20, db20, project20, _ = _fixture(
        tmp_path / "twenty",
        segment_count=20,
        frame_summaries=large_clip_evidence,
    )
    cfg40, db40, project40, _ = _fixture(
        tmp_path / "forty",
        segment_count=40,
        frame_summaries=large_clip_evidence,
    )

    snapshot20 = build_story_input_snapshot(cfg20, db20, project20)
    snapshot40 = build_story_input_snapshot(cfg40, db40, project40)
    total20 = _canonical_bytes(snapshot20)
    total40 = _canonical_bytes(snapshot40)
    segment20 = _canonical_bytes(snapshot20["segments"])
    segment40 = _canonical_bytes(snapshot40["segments"])

    assert len(snapshot20["clips"][0]["ai_visual_summary"].encode("utf-8")) <= CLIP_AI_SUMMARY_MAX_UTF8_BYTES
    assert all("frame-" not in json.dumps(segment, ensure_ascii=False) for segment in snapshot40["segments"])
    assert segment40 < segment20 * 2.15
    assert total40 - total20 < 20 * 3000
    assert snapshot40 == build_story_input_snapshot(cfg40, db40, project40)

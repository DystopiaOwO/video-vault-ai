from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json

from .database import frames, segments, videos

STATUSES = {"new", "ingested", "perceived", "plan_drafted", "needs_review", "approved", "rejected", "rendered", "needs_revision"}


def video_dir(cfg: dict, video_id: int) -> Path:
    path = Path(cfg["library_root"]) / "05_index" / f"video_{video_id}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def perceive_output(cfg: dict, db: Path, video: dict) -> Path:
    observed_frames = frames(db, int(video["id"]))
    observations = [
        {
            "start_seconds": frame["timestamp_seconds"],
            "end_seconds": frame["timestamp_seconds"] + cfg["frame_interval_seconds"],
            "observation": frame["vision_summary"] or "",
            "motion_level": "unknown",
            "visual_quality": _quality(frame["score_visual_quality"]),
        }
        for frame in observed_frames
    ]
    tags = {tag for frame in observed_frames for tag in (frame["tags"] or "").split(",") if tag}
    data = {
        "video_id": video["id"],
        "source_file": video["current_path"],
        "duration_seconds": video["duration_seconds"],
        "category": video["category"],
        "detected_style": _style(tags),
        "speech_likelihood": "unknown",
        "repetition_level": "unknown",
        "recommended_edit_type": "montage" if tags else "review",
        "timeline_observations": observations,
    }
    out = video_dir(cfg, int(video["id"])) / "perception.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def draft_plan(cfg: dict, db: Path, video: dict) -> dict:
    plan_segments = []
    for i, seg in enumerate(segments(db, int(video["id"])), 1):
        speed = _speed(seg["tags"] or "")
        duration = max(0.1, (seg["end_seconds"] - seg["start_seconds"]) / speed)
        plan_segments.append(
            {
                "order": i,
                "source_file": video["current_path"],
                "start_seconds": seg["start_seconds"],
                "end_seconds": seg["end_seconds"],
                "clip_type": seg["segment_type"],
                "speed": speed,
                "estimated_output_seconds": round(duration, 1),
                "reason": seg["reason"],
            }
        )
    return {
        "video_id": video["id"],
        "status": "needs_review",
        "edit_type": "travel_vlog" if video["category"] == "travel" else "indoor_montage",
        "category": video["category"],
        "target_duration_seconds": round(sum(s["estimated_output_seconds"] for s in plan_segments), 1),
        "style": {"mood": ["calm", "clean", "slow_life", "asmr"], "pace": "medium_fast", "color_note": "tone_map_sdr_v1"},
        "audio": {"original_audio_mode": "mute", "voice_policy": "lower_if_detected", "bgm_enabled": True, "bgm_volume_db": -18, "fade_in_seconds": 2, "fade_out_seconds": 3},
        "segments": plan_segments,
    }


def write_plan_files(cfg: dict, plan: dict) -> tuple[Path, Path, Path]:
    now = datetime.now().isoformat(timespec="seconds")
    folder = video_dir(cfg, int(plan["video_id"]))
    plan_path = folder / "edit_plan.json"
    script_path = folder / "edit_script.md"
    status_path = folder / "review_status.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    script_path.write_text(edit_script(plan), encoding="utf-8")
    status_path.write_text(
        json.dumps({"video_id": plan["video_id"], "status": plan["status"], "created_at": now, "updated_at": now, "approved_by_user": False, "notes": ""}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return plan_path, script_path, status_path


def load_plan(cfg: dict, video_id: int) -> dict:
    return json.loads((video_dir(cfg, video_id) / "edit_plan.json").read_text(encoding="utf-8"))


def set_plan_status(cfg: dict, video_id: int, status: str, notes: str = "") -> tuple[Path, Path]:
    if status not in STATUSES:
        raise ValueError(status)
    folder = video_dir(cfg, video_id)
    plan_path = folder / "edit_plan.json"
    status_path = folder / "review_status.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    review = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {"video_id": video_id, "created_at": datetime.now().isoformat(timespec="seconds")}
    plan["status"] = status
    review.update({"status": status, "updated_at": datetime.now().isoformat(timespec="seconds"), "approved_by_user": status == "approved", "notes": notes})
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    status_path.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    return plan_path, status_path


def review_text(cfg: dict, video_id: int) -> str:
    return (video_dir(cfg, video_id) / "edit_script.md").read_text(encoding="utf-8")


def revise_plan(cfg: dict, video_id: int) -> tuple[Path, Path]:
    folder = video_dir(cfg, video_id)
    notes_path = folder / "revision_prompt.txt"
    notes = notes_path.read_text(encoding="utf-8") if notes_path.exists() else ""
    plan_path, status_path = set_plan_status(cfg, video_id, "needs_review", notes)
    plan = load_plan(cfg, video_id)
    (folder / "edit_script.md").write_text(edit_script(plan) + f"\n\n## 修改備註\n{notes}\n", encoding="utf-8")
    return plan_path, status_path


def edit_script(plan: dict) -> str:
    lines = [
        "# 剪輯計畫審核稿",
        "",
        f"- 影片 ID: {plan['video_id']}",
        f"- 狀態: {plan['status']}",
        f"- 類型: {plan['edit_type']}",
        f"- 目標長度: 約 {plan['target_duration_seconds']} 秒",
        f"- 原音: {plan['audio']['original_audio_mode']}",
        f"- BGM: {'啟用' if plan['audio']['bgm_enabled'] else '停用'} / {plan['audio']['bgm_volume_db']} dB",
        "",
        "## 片段",
    ]
    for seg in plan["segments"]:
        lines += [
            "",
            f"### {seg['order']}. {seg['clip_type']}",
            f"- 時間: {_time(seg['start_seconds'])} - {_time(seg['end_seconds'])}",
            f"- 速度: {seg['speed']}x",
            f"- 預估輸出: {seg['estimated_output_seconds']} 秒",
            f"- 理由: {seg['reason']}",
        ]
    lines += [
        "",
        "## 審核重點",
        "- 片段是否符合主題？",
        "- 速度是否自然？",
        "- 長度是否剛好？",
        "- BGM / 原音配置是否 OK？",
    ]
    return "\n".join(lines)


def all_video_ids(db: Path) -> list[int]:
    return [int(video["id"]) for video in videos(db)]


def _quality(score) -> str:
    return "good" if float(score or 0) >= 0.6 else "review"


def _style(tags: set[str]) -> str:
    return "travel" if "travel" in tags or "landscape" in tags else "indoor_process"


def _speed(tags: str) -> float:
    return 3.0 if "dripping" in tags or "hands" in tags else 1.0


def _time(seconds: float) -> str:
    seconds = int(seconds or 0)
    return f"{seconds // 3600:02}:{seconds % 3600 // 60:02}:{seconds % 60:02}"

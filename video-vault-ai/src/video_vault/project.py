from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
import re
import shutil

from .bgm import auto_assign_bgm
from .database import create_project_row, init_db, project, project_bgm_tracks, project_videos, projects, segments, set_project_status, set_project_videos


def create_project(db: Path, name: str, video_ids: list[int], kind: str = "auto", category: str = "unknown", content_type: str = "diary_montage", platform: str = "YouTube", target_duration_seconds: float = 0) -> int:
    init_db(db)
    project_id = create_project_row(db, name.strip() or "未命名專案", kind, category, content_type, platform, target_duration_seconds)
    set_project_videos(db, project_id, video_ids)
    return project_id


def list_projects(db: Path) -> list[dict]:
    init_db(db)
    return [dict(row) for row in projects(db)]


def sync_project_files(cfg: dict, db: Path, project_id: int) -> list[dict]:
    # ponytail: copy-once project source files; hardlinks can come later if disk usage matters.
    rows = [dict(v) for v in project_videos(db, project_id)]
    source_dir = project_dir(cfg, project_id) / "source"
    clips_dir = project_dir(cfg, project_id) / "clips"
    result = []
    for order, video in enumerate(rows, 1):
        clip_id = f"clip_{order:03}"
        src = Path(video["current_path"])
        dst = src if src.parent.resolve() == source_dir.resolve() else source_dir / f"{clip_id}_{src.name}"
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)
        clip_dir = clips_dir / clip_id
        clip_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "clip_id": clip_id,
            "project_id": project_id,
            "video_id": video["id"],
            "filename": dst.name,
            "source_path": str(dst),
            "original_source_path": video["current_path"],
            "order": order,
            "included": True,
            "duration_seconds": video["duration_seconds"],
            "detected_category": video["category"],
            "time_of_day": _time_label(video),
            "status": video.get("status") or "uploaded",
            "segment_count": len(list(segments(db, int(video["id"])))),
        }
        (clip_dir / "clip.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        result.append(data)
    return result


def build_project_plan(cfg: dict, db: Path, project_id: int) -> dict:
    init_db(db)
    row = project(db, project_id)
    if not row:
        raise ValueError(f"project not found: {project_id}")
    clips = sync_project_files(cfg, db, project_id)
    by_video_id = {int(c["video_id"]): c for c in clips}
    itinerary = _read_json(project_dir(cfg, project_id) / "plans" / "itinerary.json").get("chapters", [])
    groups: dict[str, dict] = {}
    for video in project_videos(db, project_id):
        video = dict(video)
        clip = by_video_id[int(video["id"])]
        video_segments = [dict(seg) for seg in segments(db, int(video["id"]))]
        if not video_segments:
            chapter = _chapter_for(itinerary, clip["clip_id"]) if itinerary else None
            activity = chapter["label"] if chapter else ("已感知但無推薦片段" if clip["status"] == "perceived" else "未分析")
            label = activity if chapter else f"{clip['time_of_day']} / {activity}"
            groups.setdefault(label, _group(label, clip["time_of_day"], activity, chapter.get("order", 999) if chapter else 999))["clips"].append(_clip_summary(clip))
            continue
        for seg in video_segments:
            chapter = _chapter_for(itinerary, clip["clip_id"]) if itinerary else None
            activity = chapter["label"] if chapter else _activity(seg.get("tags", ""), video.get("category", ""))
            label = activity if chapter else f"{clip['time_of_day']} / {activity}"
            group = groups.setdefault(label, _group(label, clip["time_of_day"], activity, chapter.get("order", 999) if chapter else 999))
            group["clips"].append(_clip_summary(clip))
            group["segments"].append(
                {
                    "clip_id": clip["clip_id"],
                    "video_id": video["id"],
                    "source_file": clip["source_path"],
                    "start_seconds": seg["start_seconds"],
                    "end_seconds": seg["end_seconds"],
                    "title": seg["title"],
                    "suggested_use": seg["suggested_use"],
                    "tags": [tag for tag in (seg.get("tags") or "").split(",") if tag],
                    "score": seg["score"],
                }
            )
    ordered = sorted(groups.values(), key=lambda g: (int(g.get("order", 999)), _time_rank(g["time_of_day"]), g["activity"], g["label"]))
    for group in ordered:
        group["clips"] = _dedupe(group["clips"])
        group["segments"].sort(key=lambda s: (s["clip_id"], float(s["start_seconds"] or 0)) if itinerary or project_info_is_travel(row) else (-float(s["score"] or 0), s["clip_id"], float(s["start_seconds"] or 0)))
    project_info = dict(row)
    auto_assign_bgm(cfg, db, project_id, project_info, ordered)
    bgm = [dict(track) for track in project_bgm_tracks(db, project_id)]
    plan = {
        "project_id": project_id,
        "name": project_info["name"],
        "category": project_info["category"],
        "content_type": project_info["content_type"],
        "platform": project_info["platform"],
        "target_duration_seconds": project_info["target_duration_seconds"],
        "status": "needs_review",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "clips": [_clip_summary(c) for c in clips],
        "groups": ordered,
        "bgm": bgm,
        "title_cards": _title_cards(project_info, ordered),
    }
    write_project_files(cfg, plan)
    set_project_status(db, project_id, "needs_review")
    return plan


def project_detail(cfg: dict, db: Path, project_id: int) -> dict:
    init_db(db)
    row = project(db, project_id)
    if not row:
        return {}
    folder = project_dir(cfg, project_id)
    return {
        "project": dict(row),
        "clips": sync_project_files(cfg, db, project_id),
        "bgm": [dict(row) for row in project_bgm_tracks(db, project_id)],
        "plan": _read_json(folder / "project_plan.json"),
        "script": (folder / "project_script.md").read_text(encoding="utf-8") if (folder / "project_script.md").exists() else "",
        "folder": str(folder),
    }


def set_review_status(cfg: dict, db: Path, project_id: int, status: str, notes: str = "") -> Path:
    folder = project_dir(cfg, project_id)
    path = folder / "review_status.json"
    data = {"project_id": project_id, "status": status, "approved_by_user": status == "approved", "notes": notes, "updated_at": datetime.now().isoformat(timespec="seconds")}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    set_project_status(db, project_id, status)
    plan_path = folder / "project_plan.json"
    if plan_path.exists():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["status"] = status
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_project_files(cfg: dict, plan: dict) -> tuple[Path, Path]:
    folder = project_dir(cfg, int(plan["project_id"]))
    plan_path = folder / "project_plan.json"
    script_path = folder / "project_script.md"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    script_path.write_text(project_script(plan), encoding="utf-8")
    return plan_path, script_path


def project_script(plan: dict) -> str:
    segments = [seg for group in plan["groups"] for seg in group["segments"]]
    lines = [
        f"# {plan['name']}",
        "",
        "## 剪輯方向",
        f"- 這個專案有 {len(plan['clips'])} 支素材，目前找到 {len(segments)} 段可用片段。",
        f"- 建議先做成「{_content_type_label(plan['content_type'])}」，用時間順序當主線，再穿插特寫與氣氛鏡頭。",
        f"- 先不要急著全剪進去，優先挑每組分數最高的片段做第一版。",
        _bgm_line(plan.get("bgm", [])),
        "",
        "## 自動字卡",
    ]
    for card in plan.get("title_cards", []):
        lines.append(f"- {card['where']}｜{card['text']}｜{card['style']}")
    lines += [
        "",
        "## 建議剪輯順序",
    ]
    for group in plan["groups"]:
        if not group["segments"]:
            continue
        lines.append(f"- {group['label']}：先放 {_group_role(group['activity'])}，可用片段 {len(group['segments'])} 段。")
    if not any(group["segments"] for group in plan["groups"]):
        lines.append("- 尚未找到可用片段，請先重跑內容感知或降低推薦門檻。")

    lines += ["", "## 推薦片段"]
    for group in plan["groups"]:
        lines += ["", f"### {group['label']}"]
        if not group["segments"]:
            lines.append(f"- {_empty_group_note(group)}")
        for seg in group["segments"][:6]:
            lines.append(f"- {seg['clip_id']} {_time(seg['start_seconds'])}-{_time(seg['end_seconds'])}｜{_use_label(seg['suggested_use'])}｜分數 {seg['score']}")
        if len(group["segments"]) > 6:
            lines.append(f"- 另外還有 {len(group['segments']) - 6} 段，先不用全放。")
    lines += ["", "## 下一步", "- 先看調色預覽，挑出畫面舒服的片段。", "- 如果分組方向可以，按 OpenCut 匯出，再進 OpenCut 依照上面的順序拼第一版。"]
    return "\n".join(lines)


def _content_type_label(value: str) -> str:
    return {"travel_diary": "旅行日記", "diary_montage": "日常紀錄", "process_montage": "過程剪輯", "highlight": "精華短片"}.get(value, value)


def _bgm_line(bgm: list[dict]) -> str:
    if not bgm:
        return "- BGM：目前資料庫沒有可套用的音樂，先略過。"
    track = bgm[0]
    credit = track.get("attribution_text") or track.get("source_url") or "請確認授權資訊"
    return f"- BGM：已套用「{track.get('title', '')}」；YouTube 說明欄署名：{credit}"


def _title_cards(project_info: dict, groups: list[dict]) -> list[dict]:
    cards = []
    last_text = ""
    for group in groups:
        if not group.get("segments"):
            continue
        text = _card_text(group)
        if text == last_text:
            continue
        cards.append({"where": f"{group['label']} 第一段前", "text": text, "style": "地點/場景字卡，左下角，1.5 秒"})
        last_text = text
    return cards


def _card_text(group: dict) -> str:
    if group["activity"] == "飲食":
        return f"{group['time_of_day']}｜用餐/咖啡"
    if group["activity"] == "風景":
        return f"{group['time_of_day']}｜路上風景"
    if group["activity"] == "逛街":
        return f"{group['time_of_day']}｜街上散步"
    return group["label"].replace(" / ", " ")


def _group_role(activity: str) -> str:
    return {"飲食": "主體動作", "風景": "場景交代和轉場", "逛街": "移動過程", "特寫": "節奏點或封面候選", "其他": "補畫面"}.get(activity, activity)


def _use_label(value: str) -> str:
    return {"B-roll": "補畫面", "Shorts": "可當亮點/短影音", "Product closeup": "特寫"}.get(value, value)


def _empty_group_note(group: dict) -> str:
    if group["activity"] == "已感知但無推薦片段":
        return "已分析，但畫面分數不夠高，先不要放進第一版。"
    return "尚未有可用片段，請先做內容感知。"


def project_dir(cfg: dict, project_id: int) -> Path:
    path = Path(cfg["library_root"]) / "08_projects" / f"project_{project_id}"
    for name in ("source", "clips", "plans", "output", "feedback"):
        (path / name).mkdir(parents=True, exist_ok=True)
    return path


def _group(label: str, time_of_day: str, activity: str, order: int = 999) -> dict:
    return {"label": label, "time_of_day": time_of_day, "activity": activity, "order": order, "clips": [], "segments": []}


def _chapter_for(chapters: list[dict], clip_id: str) -> dict | None:
    return next((chapter for chapter in chapters if clip_id in chapter.get("clip_ids", [])), None)


def project_info_is_travel(row) -> bool:
    return dict(row).get("content_type") == "travel_diary"


def _clip_summary(clip: dict) -> dict:
    return {k: clip[k] for k in ("clip_id", "video_id", "filename", "source_path", "order", "duration_seconds", "detected_category", "time_of_day", "status", "segment_count")}


def _dedupe(items: list[dict]) -> list[dict]:
    seen = set()
    result = []
    for item in items:
        if item["clip_id"] not in seen:
            seen.add(item["clip_id"])
            result.append(item)
    return result


def _time_label(video: dict) -> str:
    match = re.match(r"\d{8}_(\d{2})\d{4}_", video.get("filename", ""))
    hour = int(match.group(1)) if match else None
    if hour is None:
        return "未定時間"
    if 5 <= hour <= 10:
        return "上午"
    if 11 <= hour <= 14:
        return "中午"
    if 15 <= hour <= 17:
        return "下午"
    if 18 <= hour <= 23:
        return "晚上"
    return "深夜"


def _time_rank(label: str) -> int:
    return {"深夜": 0, "上午": 1, "中午": 2, "下午": 3, "晚上": 4, "未定時間": 9}.get(label, 9)


def _activity(tags: str, category: str) -> str:
    words = {word.strip().lower() for word in f"{tags},{category}".split(",") if word.strip()}
    if words & {"coffee", "matcha", "food", "dripping", "steam", "hands"}:
        return "飲食"
    if words & {"landscape", "travel", "nature", "view", "beach", "mountain"}:
        return "風景"
    if words & {"street", "city", "walking", "shop", "shopping"}:
        return "逛街"
    if words & {"closeup"}:
        return "特寫"
    return "其他"


def _time(seconds: float) -> str:
    seconds = int(seconds or 0)
    return f"{seconds // 3600:02}:{seconds % 3600 // 60:02}:{seconds % 60:02}"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

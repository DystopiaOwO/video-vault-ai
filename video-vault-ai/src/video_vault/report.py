from __future__ import annotations

from pathlib import Path

from .database import frames, segments


def write_report(video: dict, db: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 影片內容分析報告",
        "",
        f"檔案：{video['filename']}",
        f"分類：{video['category']}",
        f"長度：{_time(video['duration_seconds'])}",
        f"解析度：{video['width']}x{video['height']}",
        f"FPS: {video['fps']}",
        "",
        "## 摘要",
        f"依抽出影格分析 {video['filename']}。",
        "",
        "## 推薦標籤",
        _all_tags(db, int(video["id"]), video["category"]),
        "",
        "## 影格分析",
    ]
    for frame in frames(db, int(video["id"])):
        if frame["vision_summary"]:
            lines.append(
                f"- {_time(frame['timestamp_seconds'])}: {frame['vision_summary']} "
                f"[{frame['tags']}] quality={frame['score_visual_quality']} usefulness={frame['score_usefulness']}"
            )
    lines += [
        "",
        "## 推薦片段",
    ]
    for i, seg in enumerate(segments(db, int(video["id"])), 1):
        lines += [
            "",
            f"### 片段 {i}",
            f"時間：{_time(seg['start_seconds'])} - {_time(seg['end_seconds'])}",
            f"類型：{seg['segment_type']}",
            f"原因：{seg['reason']}",
            f"建議用途：{seg['suggested_use']}",
        ]
    lines += ["", "## Shorts 建議", "1. 30 秒感官剪輯", "2. 製作過程精華", "3. 封面候選畫面"]
    out = out_dir / f"{Path(video['filename']).stem}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def _time(seconds: float) -> str:
    seconds = int(seconds or 0)
    return f"{seconds // 3600:02}:{seconds % 3600 // 60:02}:{seconds % 60:02}"


def _tags(category: str) -> str:
    return "travel, street, food, cafe, landscape, walking, city" if category == "travel" else "coffee, pour_over, kettle, dripping, closeup, calm, asmr"


def _all_tags(db: Path, video_id: int, category: str) -> str:
    tags = sorted({tag for frame in frames(db, video_id) for tag in (frame["tags"] or "").split(",") if tag})
    return ", ".join(tags) if tags else _tags(category)

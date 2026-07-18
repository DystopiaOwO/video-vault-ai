from __future__ import annotations

from datetime import date
from pathlib import Path
import json
import sqlite3


def build_timeline_plan(db: Path, fallback_json: Path | None = None) -> dict:
    plans = build_timeline_plans(db, fallback_json)
    return plans[0] if plans else _plan("mixed", "empty", [])


def build_timeline_plans(db: Path, fallback_json: Path | None = None) -> list[dict]:
    clips = _clips_from_sqlite(db)
    if not clips and fallback_json and fallback_json.exists():
        clips = _clips_from_json(fallback_json)
    groups: dict[str, list[dict]] = {}
    for clip in clips:
        groups.setdefault(clip["source_file"], []).append(clip)
    return [_plan(_category(group), Path(source).stem or "clip", group) for source, group in groups.items()]


def _clips_from_sqlite(db: Path) -> list[dict]:
    if not db.exists():
        return []
    with sqlite3.connect(db) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            select v.id as video_id, v.current_path, v.filename, v.category, s.start_seconds, s.end_seconds,
                   s.title, s.suggested_use, s.score
            from segments s
            join videos v on v.id = s.video_id
            order by s.video_id, s.score desc, s.start_seconds
            """
        ).fetchall()
    return [_clip(dict(row)) for row in rows]


def _clips_from_json(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("clips", data if isinstance(data, list) else data.get("segments", []))
    return [_clip(row) for row in rows]


def _clip(row: dict) -> dict:
    return {
        "video_id": row.get("video_id"),
        "filename": row.get("filename") or Path(row.get("source_file") or row.get("current_path") or "").name,
        "source_file": row.get("source_file") or row.get("current_path") or row.get("file_path") or "",
        "start_seconds": float(row.get("start_seconds") or 0),
        "end_seconds": float(row.get("end_seconds") or 0),
        "title": row.get("title") or "Untitled",
        "suggested_use": row.get("suggested_use") or "",
        "category": row.get("category") or "unknown",
        "score": float(row.get("score") or 0),
    }


def _category(clips: list[dict]) -> str:
    categories = {clip["category"] for clip in clips if clip.get("category")}
    return categories.pop() if len(categories) == 1 else "mixed"


def _plan(category: str, name: str, clips: list[dict]) -> dict:
    # ponytail: include source stem so separate same-category videos do not overwrite each other.
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:60]
    return {
        "timeline_name": f"{date.today():%Y%m%d}_{category}_{safe_name}_short_v1",
        "category": category,
        "clips": clips,
    }

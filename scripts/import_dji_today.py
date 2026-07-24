from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from video_vault.analyzer.vision_pipeline import analyze_video_frames
from video_vault.config import load_config
from video_vault.database import add_frame, frames as db_frames, init_db, set_project_videos, set_video_status, upsert_video, videos
from video_vault.ffmpeg_tools import extract_frames, frame_timestamp, metadata
from video_vault.naming import rename_after_perception
from video_vault.paths import db_path
from video_vault.planner import draft_plan, perceive_output, write_plan_files
from video_vault.project import build_project_plan, create_project, project_dir


def main() -> None:
    cfg = load_config("config.yaml")
    db = db_path(cfg)
    init_db(db)
    today = datetime.now().strftime("%Y%m%d")
    device = Path(sys.argv[1] if len(sys.argv) > 1 else "J:/")
    files = sorted(p for p in device.rglob("*") if p.suffix.lower() in {".mp4", ".mov", ".m4v"} and _shot_today(p, today))
    if not files:
        print("no DJI videos found for today")
        return

    project_id = create_project(db, f"DJI {today}", [], category="travel", content_type="travel_diary", platform="YouTube", target_duration_seconds=0)
    source_dir = project_dir(cfg, project_id) / "source"
    video_ids = []
    for order, src in enumerate(files, 1):
        dst = source_dir / f"clip_{order:03}_{src.name}"
        if not dst.exists():
            shutil.copy2(src, dst)
        video_id = upsert_video(db, {"original_path": str(dst), "current_path": str(dst), "filename": dst.name, "category": "unknown", **metadata(dst, cfg), "status": "ingested"})
        video = dict(next(v for v in videos(db) if int(v["id"]) == video_id))
        if not db_frames(db, video_id):
            out_dir = Path(cfg["library_root"]) / "03_frames" / Path(video["filename"]).stem
            for frame in extract_frames(dst, out_dir, cfg):
                add_frame(db, video_id, frame, frame_timestamp(frame, cfg))
        analyze_video_frames(db, video, cfg)
        video = rename_after_perception(cfg, db, video)
        perceive_output(cfg, db, video)
        write_plan_files(cfg, draft_plan(cfg, db, video))
        set_video_status(db, video_id, "needs_review")
        video_ids.append(video_id)
        print(f"processed #{video_id}: {video['filename']}")

    set_project_videos(db, project_id, video_ids)
    plan = build_project_plan(cfg, db, project_id)
    print(f"project #{project_id}: {plan['name']}")
    print(project_dir(cfg, project_id))


def _shot_today(path: Path, today: str) -> bool:
    return bool(re.search(rf"{today}\d{{6}}", path.name)) or datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y%m%d") == today


if __name__ == "__main__":
    main()

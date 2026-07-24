from __future__ import annotations

from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from video_vault.analyzer.vision_pipeline import analyze_video_frames
from video_vault.config import load_config
from video_vault.database import add_frame, connect, frames as db_frames, init_db, project_videos, set_video_status
from video_vault.ffmpeg_tools import extract_frames, frame_timestamp
from video_vault.paths import db_path
from video_vault.planner import draft_plan, perceive_output, write_plan_files
from video_vault.project import build_project_plan


def main() -> None:
    cfg = load_config("config.yaml")
    provider = cfg.get("ai", {}).get("provider")
    if provider == "mock":
        raise SystemExit("config.yaml is still using mock provider")
    key_env = cfg.get("ai", {}).get("cloud", {}).get("api_key_env", "OPENAI_API_KEY")
    if provider == "cloud" and not os.environ.get(key_env):
        raise SystemExit(f"{key_env} is not set")
    db = db_path(cfg)
    init_db(db)
    project_id = int(sys.argv[1])
    for video in project_videos(db, project_id):
        video = dict(video)
        video_id = int(video["id"])
        if not db_frames(db, video_id):
            out_dir = Path(cfg["library_root"]) / "03_frames" / Path(video["filename"]).stem
            for frame in extract_frames(Path(video["current_path"]), out_dir, cfg):
                add_frame(db, video_id, frame, frame_timestamp(frame, cfg))
        analyze_video_frames(db, video, cfg)
        perceive_output(cfg, db, video)
        write_plan_files(cfg, draft_plan(cfg, db, video))
        set_video_status(db, video_id, "needs_review")
        print(f"reanalyzed #{video_id}: {video['filename']}")
    build_project_plan(cfg, db, project_id)


if __name__ == "__main__":
    main()

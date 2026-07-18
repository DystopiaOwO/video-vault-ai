from __future__ import annotations

import argparse
import json
from pathlib import Path

from .analyzer.vision_pipeline import analyze_video_frames
from .bgm import import_bgm, list_bgm, youtube_credits
from .config import check_tools, load_config, save_default_config
from .color import render_color_preview
from .database import add_frame, frames as db_frames, init_db, set_video_status, upsert_video, videos, write_json_index
from .ffmpeg_tools import extract_frames, frame_timestamp, make_proxy, metadata
from .hyperframes import export_hyperframes_project, render_fast_draft, render_hyperframes_project
from .ingest import ingest_file
from .naming import rename_after_perception
from .opencut import export_opencut_handoff
from .paths import db_path, ensure_library, index_json_path, root
from .planner import all_video_ids, draft_plan, perceive_output, review_text, revise_plan, set_plan_status, write_plan_files
from .report import write_report
from .renderer import render_approved
from .scanner import scan_inbox
from .ui import run_ui


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="video-vault")
    parser.add_argument("--config", default="config.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("init", "scan", "ingest", "extract-frames", "make-proxy", "index", "analyze", "perceive", "draft-plan", "review-plan", "approve-plan", "reject-plan", "revise-plan", "render-approved", "report", "dry-run"):
        sub.add_parser(name)
    bgm_parser = sub.add_parser("add-bgm")
    bgm_parser.add_argument("file")
    bgm_parser.add_argument("--title", default="")
    bgm_parser.add_argument("--artist", default="")
    bgm_parser.add_argument("--source-url", required=True)
    bgm_parser.add_argument("--license-name", required=True)
    bgm_parser.add_argument("--license-url", default="")
    bgm_parser.add_argument("--attribution-required", action="store_true")
    bgm_parser.add_argument("--attribution-text", default="")
    bgm_parser.add_argument("--mood", default="")
    sub.add_parser("list-bgm")
    sub.add_parser("bgm-credits")
    ui_parser = sub.add_parser("ui")
    ui_parser.add_argument("--host", default="127.0.0.1")
    ui_parser.add_argument("--port", type=int, default=8765)
    color_parser = sub.add_parser("color-preview")
    color_parser.add_argument("--video-id", type=int, required=True)
    color_parser.add_argument("--mode", default="")
    color_parser.add_argument("--seconds", type=int, default=20)
    opencut_parser = sub.add_parser("opencut-export")
    opencut_parser.add_argument("--project-id", type=int, required=True)
    opencut_parser.add_argument("--render-clips", action="store_true")
    opencut_parser.add_argument("--max-segments", type=int, default=20)
    hf_parser = sub.add_parser("hyperframes-export")
    hf_parser.add_argument("--project-id", type=int, required=True)
    hf_parser.add_argument("--render-clips", action="store_true")
    hf_parser.add_argument("--max-segments", type=int, default=20)
    hf_render_parser = sub.add_parser("hyperframes-render")
    hf_render_parser.add_argument("--project-id", type=int, required=True)
    hf_render_parser.add_argument("--max-segments", type=int, default=20)
    for name in ("review-plan", "approve-plan", "reject-plan", "revise-plan", "render-approved"):
        sub.choices[name].add_argument("--video-id", type=int)
    sub.choices["render-approved"].add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "init":
        cfg_path = save_default_config(args.config)
        cfg = load_config(args.config)
        ensure_library(cfg)
        init_db(db_path(cfg))
        print(f"created {cfg_path} and {root(cfg)}")
        return

    cfg = load_config(args.config)
    db = db_path(cfg)
    if args.cmd != "dry-run":
        init_db(db)

    if args.cmd == "dry-run":
        dry_run(cfg)
    elif args.cmd == "scan":
        for path in scan_inbox(cfg):
            print(path)
    elif args.cmd == "ingest":
        for path in scan_inbox(cfg):
            print(f"{path} -> {ingest_file(path, cfg, db)}")
    elif args.cmd == "extract-frames":
        for video in videos(db):
            out_dir = root(cfg) / "03_frames" / Path(video["filename"]).stem
            for frame in extract_frames(Path(video["current_path"]), out_dir, cfg):
                add_frame(db, int(video["id"]), frame, frame_timestamp(frame, cfg))
            print(out_dir)
    elif args.cmd == "make-proxy":
        for video in videos(db):
            out = root(cfg) / "02_proxy" / f"{Path(video['filename']).stem}.mp4"
            print(make_proxy(Path(video["current_path"]), out, cfg))
    elif args.cmd == "index":
        for path in scan_inbox(cfg):
            meta = metadata(path, cfg)
            upsert_video(db, {"original_path": str(path), "current_path": str(path), "filename": path.name, "category": "unknown", **meta})
        print(write_json_index(db, index_json_path(cfg)))
    elif args.cmd in ("analyze", "perceive"):
        for video in videos(db):
            video_data = dict(video)
            analyze_one_video(cfg, db, video_data)
            video_data = rename_after_perception(cfg, db, video_data)
            print(perceive_output(cfg, db, video_data))
            set_video_status(db, int(video_data["id"]), "perceived")
    elif args.cmd == "draft-plan":
        for video in videos(db):
            plan = draft_plan(cfg, db, dict(video))
            plan_path, script_path, _ = write_plan_files(cfg, plan)
            set_video_status(db, int(video["id"]), "needs_review")
            print(plan_path)
            print(script_path)
    elif args.cmd == "review-plan":
        for video_id in _target_video_ids(args, db):
            print(review_text(cfg, video_id))
            print("use approve-plan/reject-plan/revise-plan to continue")
    elif args.cmd == "approve-plan":
        for video_id in _target_video_ids(args, db):
            print(set_plan_status(cfg, video_id, "approved")[0])
            set_video_status(db, video_id, "approved")
    elif args.cmd == "reject-plan":
        for video_id in _target_video_ids(args, db):
            print(set_plan_status(cfg, video_id, "rejected")[0])
            set_video_status(db, video_id, "rejected")
    elif args.cmd == "revise-plan":
        for video_id in _target_video_ids(args, db):
            print(revise_plan(cfg, video_id)[0])
            set_video_status(db, video_id, "needs_review")
    elif args.cmd == "render-approved":
        for video_id in _target_video_ids(args, db):
            out = render_approved(cfg, video_id, args.dry_run)
            if out:
                print(out)
                if not args.dry_run:
                    set_video_status(db, video_id, "rendered")
    elif args.cmd == "report":
        for video in videos(db):
            print(write_report(dict(video), db, root(cfg) / "06_reports"))
    elif args.cmd == "ui":
        run_ui(cfg, args.host, args.port)
    elif args.cmd == "color-preview":
        video = next(v for v in videos(db) if int(v["id"]) == args.video_id)
        mode = args.mode or cfg.get("color", {}).get("default_mode", "safe_restore")
        out = root(cfg) / "08_projects" / "color_previews" / f"video_{args.video_id}_{mode}.mp4"
        print(render_color_preview(Path(video["current_path"]), out, cfg, mode, args.seconds))
    elif args.cmd == "opencut-export":
        try:
            print(export_opencut_handoff(cfg, db, args.project_id, args.render_clips, args.max_segments))
        except PermissionError as exc:
            print(exc)
    elif args.cmd == "hyperframes-export":
        try:
            print(export_hyperframes_project(cfg, db, args.project_id, args.render_clips, args.max_segments))
        except PermissionError as exc:
            print(exc)
    elif args.cmd == "hyperframes-render":
        try:
            out = export_hyperframes_project(cfg, db, args.project_id, True, args.max_segments)
            result = render_fast_draft(out, cfg, db=db, project_id=args.project_id)
        except PermissionError as exc:
            print(exc)
            return
        print(result["output"] if result["ok"] else result["stderr"])
    elif args.cmd == "add-bgm":
        track_id = import_bgm(
            cfg,
            db,
            Path(args.file),
            {
                "title": args.title,
                "artist": args.artist,
                "source_url": args.source_url,
                "license_name": args.license_name,
                "license_url": args.license_url,
                "attribution_required": args.attribution_required,
                "attribution_text": args.attribution_text,
                "mood": args.mood,
            },
        )
        print(f"bgm #{track_id}")
    elif args.cmd == "list-bgm":
        for track in list_bgm(db):
            print(f"#{track['id']} {track['title']} - {track['artist']} [{track['license_name']}] {track['source_url']}")
    elif args.cmd == "bgm-credits":
        print(youtube_credits(db))


def dry_run(cfg: dict) -> None:
    print(f"library_root: {root(cfg)}")
    print(f"videos in inbox: {len(scan_inbox(cfg))}")
    missing = check_tools(cfg)
    print("ffmpeg: ok" if "ffmpeg_path" not in missing else "ffmpeg: missing")
    print("ffprobe: ok" if "ffprobe_path" not in missing else "ffprobe: missing")
    print("no files changed")


def analyze_one_video(cfg: dict, db: Path, video: dict) -> None:
    if not db_frames(db, int(video["id"])):
        out_dir = root(cfg) / "03_frames" / Path(video["filename"]).stem
        for frame in extract_frames(Path(video["current_path"]), out_dir, cfg):
            add_frame(db, int(video["id"]), frame, frame_timestamp(frame, cfg))
    result = analyze_video_frames(db, video, cfg)
    raw = root(cfg) / "07_ai_suggestions" / f"{Path(video['filename']).stem}.json"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def _target_video_ids(args, db: Path) -> list[int]:
    return [args.video_id] if getattr(args, "video_id", None) else all_video_ids(db)


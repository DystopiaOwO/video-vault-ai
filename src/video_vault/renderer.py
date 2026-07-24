from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile

from .color import color_filter, run_ffmpeg, video_encode_args
from .planner import load_plan, video_dir


def render_approved(cfg: dict, video_id: int, dry_run: bool = False) -> Path | None:
    plan = load_plan(cfg, video_id)
    status_path = video_dir(cfg, video_id) / "review_status.json"
    review = __import__("json").loads(status_path.read_text(encoding="utf-8"))
    if plan.get("status") != "approved" or not review.get("approved_by_user"):
        print(f"video {video_id}: not approved; review/approve before render")
        return None
    out = Path(cfg["library_root"]) / "99_exports" / f"video_{video_id}_approved_render.mp4"
    if dry_run:
        print(f"would render {out}")
        return out
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"vv_render_{video_id}_"))
    parts = []
    for seg in plan["segments"]:
        part = tmp_dir / f"part_{seg['order']:03}.mp4"
        cmd = [
            cfg["ffmpeg_path"],
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            str(seg["start_seconds"]),
            "-to",
            str(seg["end_seconds"]),
            "-i",
            seg["source_file"],
            "-vf",
            f"setpts=PTS/{seg['speed']},{color_filter(cfg.get('color', {}).get('default_mode', 'safe_restore'), cfg)}",
            "-r",
            "30",
            *video_encode_args(cfg),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
        ]
        cmd += ["-an"] if plan["audio"]["original_audio_mode"] == "mute" else ["-c:a", "aac"]
        run_ffmpeg([*cmd, str(part)], cfg)
        parts.append(part)
    list_file = tmp_dir / "concat.txt"
    list_file.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([cfg["ffmpeg_path"], "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", "-movflags", "+faststart", str(out)], check=True)
    return out

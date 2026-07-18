from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import importlib

from .color import color_filter, run_ffmpeg, video_encode_args
from .planner import load_plan, video_dir


def render_approved(cfg: dict, video_id: int, dry_run: bool = False) -> Path | None:
    delegated = _delegate_to_render_engine(cfg, video_id, dry_run)
    if delegated is not None:
        return delegated
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
            "-i",
            seg["source_file"],
            "-t",
            str(max(0.1, float(seg["end_seconds"]) - float(seg["start_seconds"]))),
            "-vf",
            f"setpts=PTS/{float(seg.get('speed', 1.0) or 1.0):g},{color_filter(cfg.get('color', {}).get('default_mode', 'safe_restore'), cfg)}",
            "-r",
            "30",
            *video_encode_args(cfg),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
        ]
        if plan["audio"]["original_audio_mode"] == "mute":
            cmd += ["-an"]
        else:
            cmd += ["-af", _atempo_chain(float(seg.get("speed", 1.0) or 1.0)), "-c:a", "aac"]
        run_ffmpeg([*cmd, str(part)], cfg)
        parts.append(part)
    list_file = tmp_dir / "concat.txt"
    list_file.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([cfg["ffmpeg_path"], "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), *video_encode_args(cfg), "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(out)], check=True)
    return out


def _atempo_chain(speed: float) -> str:
    if speed <= 0:
        raise ValueError("speed must be positive")
    factors = []
    remaining = speed
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    factors.append(remaining)
    return ",".join(f"atempo={factor:.8g}" for factor in factors)


def _delegate_to_render_engine(cfg: dict, video_id: int, dry_run: bool) -> Path | None:
    """Use a v2 legacy adapter when the integrated engine exposes one."""
    try:
        module = importlib.import_module(".render_engine", __package__)
    except ModuleNotFoundError as exc:
        if exc.name == f"{__package__}.render_engine":
            return None
        raise

    for name in ("render_approved_project", "render_final", "render_approved_legacy"):
        method = getattr(module, name, None)
        if callable(method):
            return method(cfg, video_id, dry_run=dry_run)

    factory = cfg.get("render_engine_factory")
    if callable(factory):
        engine = factory(cfg)
        method = getattr(engine, "render_approved", None) or getattr(engine, "render_final", None)
        if callable(method):
            return method(video_id=video_id, dry_run=dry_run)
    return None

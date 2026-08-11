from __future__ import annotations

from pathlib import Path
import json
import subprocess

from .media_decode import perception_decode_args


def probe(path: Path, cfg: dict) -> dict:
    cmd = [
        cfg["ffprobe_path"],
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True, encoding="utf-8")
    return json.loads(out.stdout)


def metadata(path: Path, cfg: dict) -> dict:
    data = probe(path, cfg)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    return {
        "duration_seconds": float(data.get("format", {}).get("duration") or 0),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": _fps(video.get("avg_frame_rate", "0/1")),
        "codec": video.get("codec_name", ""),
        "file_size": path.stat().st_size,
    }


def extract_frames(
    video: Path,
    out_dir: Path,
    cfg: dict,
    timestamps: list[float] | None = None,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    if timestamps is None and cfg.get("_frame_timestamps") is not None:
        timestamps = [float(value) for value in cfg["_frame_timestamps"]]
    if timestamps is None:
        interval = int(cfg["frame_interval_seconds"])
        duration = int(metadata(video, cfg)["duration_seconds"])
        timestamps = list(range(0, max(duration, 1), interval))
    height = int(cfg.get("frame_height", 720))
    # ponytail: seek per frame; much faster for sparse samples on long 4K phone clips.
    for i, timestamp in enumerate(timestamps):
        out = out_dir / f"frame_{i:05d}.jpg"
        if out.exists():
            continue
        cmd = [
            cfg["ffmpeg_path"],
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            *perception_decode_args(cfg),
            "-ss",
            _timestamp_arg(timestamp),
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-vf",
            f"scale=-2:{height}",
            "-q:v",
            "3",
            str(out),
        ]
        completed = subprocess.run(cmd, check=True)
        if completed is not None and (not out.is_file() or out.stat().st_size <= 0):
            raise RuntimeError(
                f"ffmpeg did not produce a frame at {float(timestamp):.6f}s"
            )
    return sorted(out_dir.glob("frame_*.jpg"))


def _timestamp_arg(timestamp: float) -> str:
    value = float(timestamp)
    if value.is_integer():
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def frame_timestamp(frame: Path, cfg: dict) -> float:
    return int(frame.stem.rsplit("_", 1)[-1]) * float(cfg["frame_interval_seconds"])


def make_proxy(video: Path, out_file: Path, cfg: dict) -> Path:
    out_file.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        cfg["ffmpeg_path"],
        "-y",
        "-i",
        str(video),
        "-vf",
        f"scale=-2:{cfg['proxy_height']}",
        "-c:v",
        "libx264",
        "-crf",
        "23",
        "-preset",
        "veryfast",
        "-c:a",
        "aac",
        str(out_file),
    ]
    subprocess.run(cmd, check=True)
    return out_file


def _fps(value: str) -> float:
    top, _, bottom = value.partition("/")
    return float(top) / float(bottom or 1) if float(bottom or 1) else 0.0

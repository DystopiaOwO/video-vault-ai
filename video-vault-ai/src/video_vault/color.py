from __future__ import annotations

from pathlib import Path
import json
import subprocess


def color_filter(mode: str, cfg: dict, source: Path | None = None) -> str:
    mode = mode or "safe_restore"
    if mode == "none":
        return "scale=-2:1080,format=yuv420p"
    if mode == "warm_food":
        return "eq=contrast=1.06:saturation=1.12:gamma=0.98:brightness=-0.01,scale=-2:1080,format=yuv420p"
    if mode == "dji_lut":
        lut = Path(cfg.get("color", {}).get("dji_lut_path", ""))
        if not lut.exists():
            raise FileNotFoundError(f"DJI LUT not found: {lut}")
        # ponytail: one configured LUT path; add LUT library UI only if multiple cameras matter.
        return f"lut3d=file='{_ffmpeg_path(lut)}',{auto_eq_filter(source, cfg) if source else 'eq=contrast=0.96:saturation=0.94:gamma=0.9:brightness=-0.04'},scale=-2:1080,format=yuv420p"
    return "eq=contrast=0.98:saturation=0.96:gamma=0.92:brightness=-0.035,scale=-2:1080,format=yuv420p"


def render_color_preview(source: Path, out: Path, cfg: dict, mode: str = "safe_restore", seconds: int = 20) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        cfg["ffmpeg_path"],
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-t",
        str(seconds),
        "-i",
        str(source),
        "-vf",
        color_filter(mode, cfg, source),
        *video_encode_args(cfg),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(out),
    ]
    run_ffmpeg(cmd, cfg)
    return out


def auto_eq_filter(source: Path | None, cfg: dict) -> str:
    if not source:
        return "eq=contrast=0.96:saturation=0.94:gamma=0.9:brightness=-0.04"
    decision = color_decision(source, cfg)
    return f"eq=contrast={decision['contrast']}:saturation={decision['saturation']}:gamma={decision['gamma']}:brightness={decision['brightness']}"


def color_decision(source: Path, cfg: dict) -> dict:
    out = Path(cfg["library_root"]) / "05_index" / "color_decisions"
    out.mkdir(parents=True, exist_ok=True)
    cache = out / f"{source.stem}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    stats = _brightness_stats(source, cfg)
    highlight = stats["highlight_ratio"]
    avg = stats["average"]
    if highlight > 0.18 or avg > 178:
        decision = {"brightness": -0.07, "gamma": 0.86, "saturation": 0.9, "contrast": 0.94, **stats}
    elif highlight > 0.09 or avg > 158:
        decision = {"brightness": -0.04, "gamma": 0.9, "saturation": 0.94, "contrast": 0.96, **stats}
    elif avg < 78:
        decision = {"brightness": 0.02, "gamma": 1.05, "saturation": 1.02, "contrast": 1.02, **stats}
    else:
        decision = {"brightness": -0.015, "gamma": 0.96, "saturation": 0.98, "contrast": 1.0, **stats}
    cache.write_text(json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8")
    return decision


def _brightness_stats(source: Path, cfg: dict) -> dict:
    return _stats_from_luma_probe(source, cfg)


def _stats_from_luma_probe(source: Path, cfg: dict) -> dict:
    cmd = [
        cfg["ffmpeg_path"],
        "-hide_banner",
        "-v",
        "error",
        "-i",
        str(source),
        "-vf",
        "fps=1/8,scale=160:-1,format=gray,signalstats,metadata=print:file=-",
        "-frames:v",
        "8",
        "-f",
        "null",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    avgs, highs = [], []
    for line in (proc.stdout + proc.stderr).splitlines():
        if "lavfi.signalstats.YAVG=" in line:
            avgs.append(float(line.rsplit("=", 1)[-1]))
        elif "lavfi.signalstats.YHIG=" in line:
            highs.append(float(line.rsplit("=", 1)[-1]))
    if not avgs:
        return {"average": 128, "highlight_ratio": 0, "sampled_frames": 0}
    over = sum(1 for value in highs if value >= 245) / max(len(highs), 1)
    return {"average": round(sum(avgs) / len(avgs), 2), "highlight_ratio": round(over, 3), "sampled_frames": len(avgs)}


def video_encode_args(cfg: dict) -> list[str]:
    encoder = cfg.get("color", {}).get("video_encoder", "h264_nvenc")
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "22"]
    if encoder == "h264_qsv":
        return ["-c:v", "h264_qsv", "-global_quality", "22"]
    if encoder == "h264_amf":
        return ["-c:v", "h264_amf", "-quality", "balanced", "-qp_i", "22", "-qp_p", "22"]
    return cpu_encode_args()


def cpu_encode_args() -> list[str]:
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22"]


def run_ffmpeg(cmd: list[str], cfg: dict) -> None:
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError:
        encoder = cfg.get("color", {}).get("video_encoder", "h264_nvenc")
        if encoder == "libx264":
            raise
        cpu = _replace_video_encoder(cmd)
        subprocess.run(cpu, check=True)


def _replace_video_encoder(cmd: list[str]) -> list[str]:
    cleaned = []
    skip_next_for = {"-c:v", "-preset", "-cq", "-global_quality", "-quality", "-qp_i", "-qp_p", "-crf"}
    skip = False
    for item in cmd:
        if skip:
            skip = False
            continue
        if item in skip_next_for:
            skip = True
            continue
        cleaned.append(item)
    out_index = len(cleaned) - 1
    return [*cleaned[:out_index], *cpu_encode_args(), *cleaned[out_index:]]


def _ffmpeg_path(path: Path) -> str:
    return path.resolve().as_posix().replace(":", r"\:")

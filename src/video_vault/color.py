from __future__ import annotations

from pathlib import Path
import json
import sqlite3
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
        *video_decode_args(cfg),
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
    cache = out / f"{source.stem}_reference_v1.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    reference = _color_reference(source, cfg)
    stats = _brightness_stats_at(source, cfg, reference["timestamp_seconds"]) if reference else _brightness_stats(source, cfg)
    if reference:
        stats["color_reference"] = reference
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
    decision = _apply_perception_bias(decision, _perception_text(source, cfg))
    cache.write_text(json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8")
    return decision


def _apply_perception_bias(decision: dict, text: str) -> dict:
    text = text.lower()
    if not text:
        return decision
    out = dict(decision)
    if any(word in text for word in ("food", "coffee", "matcha", "cafe", "咖啡", "抹茶", "食物", "餐點", "咖啡廳")):
        out["saturation"] = round(min(out["saturation"] + 0.04, 1.12), 3)
        out["gamma"] = round(max(out["gamma"] - 0.015, 0.86), 3)
    if any(word in text for word in ("landscape", "travel", "street", "車站", "街景", "風景", "戶外", "旅行")):
        out["contrast"] = round(min(out["contrast"] + 0.03, 1.08), 3)
    if any(word in text for word in ("overexposure", "overexposed", "過曝", "太亮", "白掉")):
        out["brightness"] = round(max(out["brightness"] - 0.03, -0.1), 3)
        out["gamma"] = round(max(out["gamma"] - 0.03, 0.84), 3)
    out["perception_basis"] = text[:500]
    return out


def _perception_text(source: Path, cfg: dict) -> str:
    db = Path(cfg["library_root"]) / "05_index" / "video_vault.sqlite3"
    if not db.exists():
        return ""
    try:
        with sqlite3.connect(db) as con:
            con.row_factory = sqlite3.Row
            video = con.execute("select id, category, filename from videos where current_path=? or original_path=? or filename=? limit 1", (str(source), str(source), source.name)).fetchone()
            if not video:
                return ""
            frames = con.execute("select vision_summary, tags from frames where video_id=? order by score_usefulness desc limit 8", (video["id"],)).fetchall()
            segments = con.execute("select title, reason, tags, suggested_use from segments where video_id=? order by score desc limit 5", (video["id"],)).fetchall()
            parts = [video["category"] or "", video["filename"] or ""]
            parts += [" ".join(str(row[k] or "") for k in row.keys()) for row in [*frames, *segments]]
            return " ".join(parts)
    except sqlite3.Error:
        return ""


def _color_reference(source: Path, cfg: dict) -> dict | None:
    db = Path(cfg["library_root"]) / "05_index" / "video_vault.sqlite3"
    if not db.exists():
        return None
    try:
        with sqlite3.connect(db) as con:
            con.row_factory = sqlite3.Row
            video = con.execute("select id from videos where current_path=? or original_path=? or filename=? limit 1", (str(source), str(source), source.name)).fetchone()
            if not video:
                return None
            segment = con.execute(
                "select start_seconds, end_seconds, title, reason, score from segments where video_id=? order by score desc limit 1",
                (video["id"],),
            ).fetchone()
            if segment:
                return {
                    "source": "segment",
                    "timestamp_seconds": round((float(segment["start_seconds"]) + float(segment["end_seconds"])) / 2, 2),
                    "score": segment["score"],
                    "label": segment["title"] or segment["reason"] or "",
                }
            frame = con.execute(
                "select timestamp_seconds, vision_summary, score_usefulness from frames where video_id=? order by score_usefulness desc limit 1",
                (video["id"],),
            ).fetchone()
            if frame:
                return {
                    "source": "frame",
                    "timestamp_seconds": float(frame["timestamp_seconds"]),
                    "score": frame["score_usefulness"],
                    "label": frame["vision_summary"] or "",
                }
    except sqlite3.Error:
        return None
    return None


def _brightness_stats(source: Path, cfg: dict) -> dict:
    return _stats_from_luma_probe(source, cfg)


def _brightness_stats_at(source: Path, cfg: dict, timestamp: float) -> dict:
    cmd = [
        cfg["ffmpeg_path"],
        "-hide_banner",
        "-v",
        "error",
        "-ss",
        str(max(0, timestamp)),
        "-i",
        str(source),
        "-vf",
        "scale=160:-1,format=gray,signalstats,metadata=print:file=-",
        "-frames:v",
        "1",
        "-f",
        "null",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    stats = _parse_luma_stats(proc.stdout + proc.stderr)
    stats["sampled_reference_second"] = timestamp
    return stats


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
    return _parse_luma_stats(proc.stdout + proc.stderr)


def _parse_luma_stats(text: str) -> dict:
    avgs, highs = [], []
    for line in text.splitlines():
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
        return ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ull", "-cq", "22"]
    if encoder == "h264_qsv":
        return ["-c:v", "h264_qsv", "-global_quality", "22"]
    if encoder == "h264_amf":
        return ["-c:v", "h264_amf", "-quality", "balanced", "-qp_i", "22", "-qp_p", "22"]
    return cpu_encode_args()


def video_decode_args(cfg: dict) -> list[str]:
    return ["-hwaccel", "cuda"] if cfg.get("color", {}).get("video_encoder", "h264_nvenc") == "h264_nvenc" else []


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
    skip_next_for = {"-c:v", "-preset", "-tune", "-cq", "-global_quality", "-quality", "-qp_i", "-qp_p", "-crf", "-hwaccel"}
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

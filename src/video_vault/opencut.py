from __future__ import annotations

from pathlib import Path
import csv
import json
import shutil
import subprocess
import urllib.request

from .color import color_filter, run_ffmpeg, video_encode_args
from .project import assert_project_approved, build_project_plan, project_detail, project_dir

OPENCUT_URL = "http://127.0.0.1:3000/projects"


def opencut_status() -> dict:
    try:
        with urllib.request.urlopen(OPENCUT_URL, timeout=2) as response:
            return {"running": 200 <= response.status < 500, "url": OPENCUT_URL}
    except Exception:
        return {"running": False, "url": OPENCUT_URL}


def start_opencut(workspace: Path | None = None) -> dict:
    if opencut_status()["running"]:
        return opencut_status()
    root = workspace or Path(__file__).resolve().parents[3] / "tools" / "opencut-classic"
    web = root / "apps" / "web"
    bun = _bun()
    if not bun or not web.exists():
        return {"running": False, "url": OPENCUT_URL, "error": "OpenCut 或 Bun 尚未安裝"}
    subprocess.Popen([str(bun), "run", "dev"], cwd=web, creationflags=subprocess.CREATE_NO_WINDOW)
    return opencut_status()


def _bun() -> Path | None:
    found = shutil.which("bun")
    if found:
        return Path(found)
    base = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    return next(base.glob("Oven-sh.Bun_*/*/bun.exe"), None) if base.exists() else None


def export_opencut_handoff(cfg: dict, db: Path, project_id: int, render_clips: bool = False, max_segments: int = 20) -> Path:
    if render_clips:
        assert_project_approved(cfg, db, project_id, "OpenCut 調色片段輸出")
    detail = project_detail(cfg, db, project_id)
    plan = detail.get("plan") or {}
    if not plan.get("groups"):
        plan = build_project_plan(cfg, db, project_id)
        detail = project_detail(cfg, db, project_id)
    out = project_dir(cfg, project_id) / "output" / "opencut_handoff"
    assets = out / "assets"
    clips_dir = out / "graded_clips"
    assets.mkdir(parents=True, exist_ok=True)
    clips_dir.mkdir(parents=True, exist_ok=True)

    lut = Path(cfg.get("color", {}).get("dji_lut_path", ""))
    if lut.is_file():
        shutil.copy2(lut, assets / lut.name)
    for track in detail.get("bgm", []):
        src = Path(track.get("file_path", ""))
        if src.exists():
            shutil.copy2(src, assets / src.name)

    segments = _segments(detail, max_segments)
    (out / "opencut_handoff.json").write_text(json.dumps({"project": detail["project"], "clips": detail["clips"], "bgm": detail.get("bgm", []), "bgm_recommendations": plan.get("bgm_recommendations", []), "title_cards": plan.get("title_cards", []), "segments": segments}, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(out / "recommended_segments.csv", segments)
    (out / "README.md").write_text(_readme(detail, segments, render_clips), encoding="utf-8")

    if render_clips:
        targets = set()
        for i, seg in enumerate(segments, 1):
            target = clips_dir / f"{i:03}_{seg['clip_id']}_{int(float(seg['start_seconds']) * 10):05d}_{_safe(seg['title'])}.mp4"
            targets.add(target.name)
            seg["graded_clip"] = str(target if _has_video_track(target, cfg) else _render_segment(cfg, seg, target))
        for stale in clips_dir.glob("*.mp4"):
            if stale.name not in targets:
                stale.unlink(missing_ok=True)
        (out / "opencut_handoff.json").write_text(json.dumps({"project": detail["project"], "clips": detail["clips"], "bgm": detail.get("bgm", []), "bgm_recommendations": plan.get("bgm_recommendations", []), "title_cards": plan.get("title_cards", []), "segments": segments}, ensure_ascii=False, indent=2), encoding="utf-8")
        _write_csv(out / "recommended_segments.csv", segments)
    return out


def _segments(detail: dict, max_segments: int = 20) -> list[dict]:
    return [seg for seg in detail.get("segments", []) if seg.get("include", True)][:max_segments]


def _write_csv(path: Path, segments: list[dict]) -> None:
    fields = ["group", "clip_id", "source_file", "start_seconds", "end_seconds", "title", "suggested_use", "score", "graded_clip"]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(segments)


def _render_segment(cfg: dict, seg: dict, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    duration = max(0.1, float(seg["end_seconds"]) - float(seg["start_seconds"]))
    cmd = [
        cfg["ffmpeg_path"],
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        str(seg["start_seconds"]),
        "-t",
        str(duration),
        "-i",
        seg["source_file"],
        "-vf",
        color_filter(cfg.get("color", {}).get("default_mode", "dji_lut"), cfg, Path(seg["source_file"])),
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


def _has_video_track(path: Path, cfg: dict) -> bool:
    if not path.exists() or path.stat().st_size < 1024 * 1024:
        return False
    proc = subprocess.run(
        [cfg.get("ffprobe_path", "ffprobe"), "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0 and "video" in proc.stdout


def _readme(detail: dict, segments: list[dict], render_clips: bool) -> str:
    return "\n".join(
        [
            f"# OpenCut handoff - {detail['project']['name']}",
            "",
            "## OpenCut 使用方式",
            "1. 在 OpenCut 建立新專案。",
            "2. 匯入本專案 source 資料夾的原始影片。",
            "3. 匯入 assets 內的 BGM / LUT。",
            "4. 依照 recommended_segments.csv 的時間碼剪片。",
            "5. 如果有 graded_clips，可先用這些已套 LUT 片段快速拼剪。",
            "",
            f"- 推薦片段數: {len(segments)}",
            f"- 已產生調色片段: {'yes' if render_clips else 'no'}",
            "- 字卡草稿在 opencut_handoff.json 的 title_cards。",
        ]
    )


def _safe(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in (text or "clip"))[:40]

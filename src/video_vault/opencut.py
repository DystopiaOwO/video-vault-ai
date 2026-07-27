from __future__ import annotations

from pathlib import Path
import csv
import json
import shutil
import subprocess
import urllib.request

from .color import color_filter, run_ffmpeg, video_encode_args
from .database import project_bgm_tracks
from .handoff import HandoffError, build_handoff_manifest, load_approved_handoff_snapshot, register_handoff_file, write_handoff_manifest
from .project import assert_project_approved, build_project_plan, project_detail, project_dir
from .segment_renderer import render_segment

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


def export_opencut_handoff(
    cfg: dict,
    db: Path,
    project_id: int,
    render_clips: bool = False,
    max_segments: int = 20,
    *,
    base_revision: int | None = None,
    mode: str = "diagnostic_first_n",
    first_n: int | None = None,
) -> Path:
    if render_clips:
        assert_project_approved(cfg, db, project_id, "OpenCut 調色片段輸出")
        if mode == "diagnostic_first_n":
            mode = "complete"
    delivery = build_handoff_manifest(cfg, db, project_id, mode=mode, first_n=first_n if first_n is not None else max_segments)
    approved = None
    source_manifest = None
    if delivery.get("handoff_type") == "formal":
        approved = load_approved_handoff_snapshot(cfg, db, project_id)
        source_manifest = approved["manifest"]
    detail = project_detail(cfg, db, project_id) if delivery.get("handoff_type") == "diagnostic" else {"project": {"name": source_manifest.get("project_name", f"project_{project_id}")}, "clips": []}
    plan = detail.get("plan") or {}
    out = project_dir(cfg, project_id) / "output" / "opencut_handoff"
    assets = out / "assets"
    clips_dir = out / "graded_clips"
    assets.mkdir(parents=True, exist_ok=True)
    clips_dir.mkdir(parents=True, exist_ok=True)

    lut = Path(cfg.get("color", {}).get("dji_lut_path", ""))
    if lut.is_file():
        shutil.copy2(lut, assets / lut.name)
    # Diagnostic previews may resolve current BGM rows. Formal handoffs use
    # the BGM contract embedded in the immutable approved manifest.
    handoff_bgm = [dict(row) for row in (source_manifest.get("bgm", []) if source_manifest else project_bgm_tracks(db, project_id))]
    for track in handoff_bgm:
        src = Path(track.get("file_path", ""))
        if src.exists():
            shutil.copy2(src, assets / src.name)

    segments = [dict(item) for item in delivery.get("timeline_items", [])]
    handoff = {
        "project": detail["project"],
        "clips": detail.get("clips", []),
        "bgm": handoff_bgm,
        "bgm_recommendations": plan.get("bgm_recommendations", []),
        "title_cards": plan.get("title_cards", []),
        "segments": segments,
        "handoff_manifest": delivery,
    }
    (out / "opencut_handoff.json").write_text(json.dumps(handoff, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(out / "recommended_segments.csv", segments)
    (out / "README.md").write_text(_readme(detail, segments, render_clips), encoding="utf-8")

    if render_clips:
        targets = set()
        for i, seg in enumerate(segments, 1):
            target = clips_dir / f"{i:03}_{seg['clip_id']}_{int(float(seg['start_seconds']) * 10):05d}_{_safe(seg['title'])}.mp4"
            targets.add(target.name)
            if _has_video_track(target, cfg):
                seg["graded_clip"] = str(target)
            elif source_manifest is not None:
                original = next((item for item in source_manifest.get("segments", []) if str(item.get("segment_id")) == str(seg.get("stable_id") or seg.get("segment_id"))), None)
                if original is None:
                    raise HandoffError("mapping_missing", f"找不到核准片段：{seg.get('stable_id')}", action="重新建立 approval snapshot")
                rendered = render_segment(cfg, source_manifest, dict(original))
                shutil.copy2(rendered.output_path, target)
                seg["cache_key"] = rendered.cache_key
                seg["graded_clip"] = str(target)
            else:
                seg["graded_clip"] = str(_render_segment(cfg, seg, target))
        for stale in clips_dir.glob("*.mp4"):
            if stale.name not in targets:
                stale.unlink(missing_ok=True)
        handoff["segments"] = segments
        (out / "opencut_handoff.json").write_text(json.dumps(handoff, ensure_ascii=False, indent=2), encoding="utf-8")
        _write_csv(out / "recommended_segments.csv", segments)
    manifest_path = out / "handoff_manifest.json"
    for item in sorted(out.rglob("*")):
        if item.is_file() and item.name not in {"handoff_manifest.json", "opencut_handoff.json"}:
            stable_id = next((str(seg.get("stable_id") or "") for seg in segments if seg.get("graded_clip") and Path(str(seg["graded_clip"])).name == item.name), "")
            register_handoff_file(delivery, item, stable_id=stable_id)
    write_handoff_manifest(manifest_path, delivery)
    handoff["handoff_manifest"] = delivery
    (out / "opencut_handoff.json").write_text(json.dumps(handoff, ensure_ascii=False, indent=2), encoding="utf-8")
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

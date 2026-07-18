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
    detail = project_detail(cfg, db, project_id)
    plan = detail.get("plan") or build_project_plan(cfg, db, project_id)
    if render_clips:
        # JSON/CSV/README handoff remains available during review; only
        # material-changing graded clip rendering requires approval.
        assert_project_approved(cfg, db, project_id, "OpenCut 調色片段輸出")
    manifest = load_render_manifest(cfg, project_id)
    out = project_dir(cfg, project_id) / "output" / "opencut_handoff"
    assets = out / "assets"
    clips_dir = out / "graded_clips"
    assets.mkdir(parents=True, exist_ok=True)
    if render_clips:
        clips_dir.mkdir(parents=True, exist_ok=True)

    lut = Path(cfg.get("color", {}).get("dji_lut_path", ""))
    if lut.is_file():
        shutil.copy2(lut, assets / lut.name)
    for track in detail.get("bgm", []):
        src = Path(track.get("file_path", ""))
        if src.exists():
            shutil.copy2(src, assets / src.name)

    segments = _segments(detail, plan, max_segments, manifest)
    payload = _handoff_payload(detail, plan, segments, manifest)
    (out / "opencut_handoff.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
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
        (out / "opencut_handoff.json").write_text(json.dumps(_handoff_payload(detail, plan, segments, manifest), ensure_ascii=False, indent=2), encoding="utf-8")
        _write_csv(out / "recommended_segments.csv", segments)
    return out


def load_render_manifest(cfg: dict, project_id: int) -> dict | None:
    folder = project_dir(cfg, project_id)
    candidates = (
        folder / "render_manifest.json",
        folder / "output" / "render_manifest.json",
        folder / "render" / "manifest" / "render_manifest.json",
        folder / "output" / "render" / "render_manifest.json",
    )
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Render Manifest 無法讀取：{path}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("segments", []), list):
            raise ValueError(f"Render Manifest 格式錯誤：{path}")
        return data
    return None


def _handoff_payload(detail: dict, plan: dict, segments: list[dict], manifest: dict | None) -> dict:
    return {
        "project": detail["project"],
        "clips": detail["clips"],
        "bgm": detail.get("bgm", []),
        "bgm_recommendations": plan.get("bgm_recommendations", []),
        "title_cards": (manifest or {}).get("overlays") or plan.get("title_cards", []),
        "manifest": {key: manifest[key] for key in ("schema_version", "manifest_hash", "plan_id", "project_id") if key in manifest} if manifest else None,
        "segments": segments,
    }


def _segments(detail: dict, plan: dict, max_segments: int = 20, manifest: dict | None = None) -> list[dict]:
    if manifest is not None:
        rows = []
        for index, raw in enumerate(manifest.get("segments", []), 1):
            if not raw.get("include", True):
                continue
            start = _seconds(raw, "source_in", "source_in_ms")
            end = _seconds(raw, "source_out", "source_out_ms")
            speed = float(raw.get("speed", 1.0) or 1.0)
            if end <= start or speed <= 0:
                continue
            order = int(raw.get("manual_order", raw.get("order", index)) or index)
            rows.append({
                **raw,
                "order": order,
                "manual_order": order,
                "include": True,
                "clip_id": raw.get("clip_id") or raw.get("segment_id") or f"segment_{index:03}",
                "source_file": str(raw.get("source_file") or raw.get("source_path") or ""),
                "start_seconds": start,
                "end_seconds": end,
                "timeline_duration_seconds": (end - start) / speed,
                "speed": speed,
                "audio_role": raw.get("audio_role", "keep_original"),
                "title": raw.get("title", ""),
                "suggested_use": raw.get("suggested_use", raw.get("scene_role", "")),
                "score": raw.get("score", 0),
            })
        return sorted(rows, key=lambda row: (row["order"], row["clip_id"]))[:max_segments]
    return _legacy_segments(plan, max_segments)


def _legacy_segments(plan: dict, max_segments: int = 20) -> list[dict]:
    if plan.get("content_type") == "travel_diary":
        return _travel_segments(plan, max_segments)
    rows = []
    for group in plan.get("groups", []):
        for seg in group.get("segments", []):
            rows.append({**seg, "group": group["label"], "group_order": int(group.get("order", 999))})
    return sorted(rows, key=lambda s: (-float(s.get("score") or 0), s["clip_id"], float(s.get("start_seconds") or 0)))[:max_segments]


def _travel_segments(plan: dict, max_segments: int) -> list[dict]:
    groups = [g for g in plan.get("groups", []) if g.get("segments")]
    if not groups:
        return []
    per_group = max(1, max_segments // len(groups))
    rows = []
    for group in groups:
        ordered = sorted(group["segments"], key=lambda s: (s["clip_id"], float(s.get("start_seconds") or 0)))
        for seg in ordered[:per_group]:
            rows.append({**seg, "group": group["label"], "group_order": int(group.get("order", 999))})
    remaining = max_segments - len(rows)
    if remaining > 0:
        used = {(r["clip_id"], r["start_seconds"], r["end_seconds"]) for r in rows}
        extras = []
        for group in groups:
            for seg in sorted(group["segments"], key=lambda s: -float(s.get("score") or 0)):
                key = (seg["clip_id"], seg["start_seconds"], seg["end_seconds"])
                if key not in used:
                    extras.append({**seg, "group": group["label"], "group_order": int(group.get("order", 999))})
        rows.extend(sorted(extras, key=lambda s: (-float(s.get("score") or 0), s["group_order"]))[:remaining])
    return sorted(rows, key=lambda s: (s["group_order"], s["clip_id"], float(s.get("start_seconds") or 0)))


def _seconds(raw: dict, seconds_key: str, milliseconds_key: str) -> float:
    if raw.get(milliseconds_key) is not None:
        return float(raw[milliseconds_key]) / 1000.0
    return float(raw.get(seconds_key, 0) or 0)


def _write_csv(path: Path, segments: list[dict]) -> None:
    fields = [
        "order", "include", "group", "clip_id", "source_file",
        "start_seconds", "end_seconds", "timeline_duration_seconds", "speed",
        "audio_role", "title", "suggested_use", "score", "graded_clip",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(segments)


def _render_segment(cfg: dict, seg: dict, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    speed = float(seg.get("speed", 1.0) or 1.0)
    duration = max(0.1, (float(seg["end_seconds"]) - float(seg["start_seconds"])) / speed)
    video_filter = f"setpts=PTS/{speed:g},{color_filter(cfg.get('color', {}).get('default_mode', 'dji_lut'), cfg, Path(seg['source_file']))}"
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
        video_filter,
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

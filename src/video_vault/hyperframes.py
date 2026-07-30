from __future__ import annotations

from html import escape
from pathlib import Path
import json
import os
import shutil
import subprocess

from .color import run_ffmpeg, video_decode_args, video_encode_args
from .handoff import HandoffError, build_handoff_manifest, escape_ffconcat_path
from .opencut import export_opencut_handoff
from .project import assert_project_approved, project_dir


HYPERFRAMES_RUNTIME = Path(__file__).resolve().parents[2] / "tools" / "hyperframes"


def export_hyperframes_project(
    cfg: dict,
    db: Path,
    project_id: int,
    render_clips: bool = True,
    max_segments: int = 20,
    *,
    base_revision: int | None = None,
    mode: str = "diagnostic_first_n",
    first_n: int | None = None,
) -> Path:
    if render_clips:
        assert_project_approved(cfg, db, project_id, "HyperFrames 正式交付")
        if mode == "diagnostic_first_n":
            mode = "complete"
    if render_clips:
        unload_local_llm_model(cfg)
    # ponytail: avoid CPU-heavy LUT pre-render here; dedicated OpenCut graded clips can do that when explicitly requested.
    handoff_kwargs = {}
    if base_revision is not None:
        handoff_kwargs["base_revision"] = base_revision
    if mode != "diagnostic_first_n" or first_n is not None:
        handoff_kwargs["mode"] = mode
        handoff_kwargs["first_n"] = first_n
    handoff = export_opencut_handoff(cfg, db, project_id, render_clips=False, max_segments=max_segments, **handoff_kwargs)
    data = json.loads((handoff / "opencut_handoff.json").read_text(encoding="utf-8"))
    out = project_dir(cfg, project_id) / "output" / "hyperframes"
    media = out / "media"
    media.mkdir(parents=True, exist_ok=True)

    clips = []
    t = 0.0
    source_segments = {str(seg.get("stable_id") or seg.get("segment_id") or seg.get("clip_id") or ""): seg for seg in data.get("segments", [])}
    visual_timeline = data.get("visual_timeline") or data.get("handoff_manifest", {}).get("visual_timeline", {}) or {}
    visual_items = {str(item.get("stable_id") or ""): dict(item) for item in visual_timeline.get("resolved_items") or visual_timeline.get("items") or [] if isinstance(item, dict)}
    visual_events = []
    sequence = visual_timeline.get("sequence") or []
    if sequence:
        for entry in sequence:
            stable_id = str(entry.get("stable_id") or "")
            if entry.get("kind") == "visual":
                item = visual_items.get(stable_id)
                if item:
                    visual_events.append({**item, "timeline_start": float(entry.get("start_seconds") or 0), "duration": float(entry.get("duration_seconds") or item.get("duration_seconds") or 0)})
                continue
            seg = source_segments.get(stable_id)
            if seg is None:
                continue
            src = _resolve_media_source(seg, handoff, out)
            index = len(clips) + 1
            dst = media / f"{index:03}_{src.name}"
            _publish_media_copy(src, dst)
            clips.append({**seg, "file": dst.name, "timeline_start": round(float(entry.get("start_seconds") or 0), 3), "duration": round(float(entry.get("duration_seconds") or _duration(seg)), 3)})
        t = float(visual_timeline.get("resolved_duration_seconds") or max((item["timeline_start"] + item["duration"] for item in clips), default=0))
    else:
        for i, seg in enumerate(data.get("segments", []), 1):
            src = _resolve_media_source(seg, handoff, out)
            dst = media / f"{i:03}_{src.name}"
            _publish_media_copy(src, dst)
            duration = _duration(seg)
            clips.append({**seg, "file": dst.name, "timeline_start": round(t, 3), "duration": round(duration, 3)})
            t += duration
    # Lower thirds are overlays rather than sequence entries, but they remain
    # first-class native events in the same approved visual contract.
    for item in visual_items.values():
        if item.get("type") == "lower_third" and not any(event.get("stable_id") == item.get("stable_id") for event in visual_events):
            visual_events.append({**item, "timeline_start": float(item.get("resolved_start_seconds", item.get("start_seconds", 0)) or 0), "duration": float(item.get("duration_seconds") or 0)})

    bgm = _copy_bgm(data, media)
    timeline = {
        "project": data["project"],
        "clips": clips,
        "bgm": bgm,
        "duration": round(float(data.get("handoff_manifest", {}).get("visual_timeline_duration_seconds") or t), 3),
        "visual_timeline": visual_timeline,
        "visual_items": visual_events,
        "handoff_manifest": data.get("handoff_manifest", {}),
    }
    (out / "timeline.json").write_text(json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "index.html").write_text(_html(data["project"]["name"], clips, bgm, t, visual_events), encoding="utf-8")
    (out / "README.md").write_text(_readme(out), encoding="utf-8")
    return out


def render_hyperframes_project(project: Path, output: Path | None = None, cfg: dict | None = None, db: Path | None = None, project_id: int | None = None) -> dict:
    if cfg is not None and db is not None and project_id is not None:
        assert_project_approved(cfg, db, project_id, "HyperFrames MP4 輸出")
    if cfg is not None:
        unload_local_llm_model(cfg)
    output = output or project / "story_draft.mp4"
    npx = shutil.which("npx.cmd") or shutil.which("npx")
    if not npx:
        return {"ok": False, "code": "dependency_missing", "output": str(output), "stdout": "", "stderr": "找不到 npx。請先在交付專案內完成固定版本的 HyperFrames 安裝；正式 render 不會自動下載依賴"}
    runtime = HYPERFRAMES_RUNTIME
    package_lock = runtime / "package-lock.json"
    modules = runtime / "node_modules"
    if not package_lock.is_file() or not modules.is_dir():
        return {"ok": False, "code": "dependency_missing", "output": str(output), "stdout": "", "stderr": "缺少 pinned HyperFrames runtime。請在 tools/hyperframes 執行 npm ci；正式 render 不會自動下載依賴"}
    # --no-install and --prefix keep formal rendering offline and tied to the
    # committed lockfile/runtime instead of the user's global npm state.
    cmd = [npx, "--no-install", "--prefix", str(runtime), "hyperframes", "render", "--gpu", "--browser-gpu", "--output", str(output)]
    env = os.environ.copy()
    env.update(
        {
            "HYPERFRAMES_NO_TELEMETRY": "1",
            "HYPERFRAMES_NO_UPDATE_CHECK": "1",
            "HYPERFRAMES_NO_AUTO_INSTALL": "1",
            "DO_NOT_TRACK": "1",
        }
    )
    proc = subprocess.run(cmd, cwd=project, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return {"ok": proc.returncode == 0, "output": str(output), "stdout": proc.stdout, "stderr": proc.stderr}


def render_fast_draft(project: Path, cfg: dict, output: Path | None = None, db: Path | None = None, project_id: int | None = None) -> dict:
    if db is not None and project_id is not None:
        assert_project_approved(cfg, db, project_id, "HyperFrames MP4 輸出")
    unload_local_llm_model(cfg)
    timeline = json.loads((project / "timeline.json").read_text(encoding="utf-8"))
    output = output or project / "story_draft_fast.mp4"
    fast_dir = project / "fast_segments"
    fast_dir.mkdir(exist_ok=True)
    segment_files = [_fast_segment(project, fast_dir, clip, cfg, i) for i, clip in enumerate(timeline["clips"], 1)]
    list_file = project / "concat.txt"
    list_file.write_text("".join(f"file '{escape_ffconcat_path(path)}'\n" for path in segment_files), encoding="utf-8")
    bgm = timeline.get("bgm")
    if bgm:
        run_ffmpeg(
            [
                cfg["ffmpeg_path"],
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                *video_decode_args(cfg),
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
                "-stream_loop",
                "-1",
                "-i",
                str(project / "media" / bgm["file"]),
                "-filter_complex",
                "[0:a]volume=0.35[a0];[1:a]volume=0.25[a1];[a0][a1]amix=inputs=2:duration=first:dropout_transition=2[a]",
                "-map",
                "0:v",
                "-map",
                "[a]",
                *video_encode_args(cfg),
                "-c:a",
                "aac",
                "-shortest",
                "-movflags",
                "+faststart",
                str(output),
            ],
            cfg,
        )
    else:
        run_ffmpeg([cfg["ffmpeg_path"], "-hide_banner", "-loglevel", "error", "-y", *video_decode_args(cfg), "-f", "concat", "-safe", "0", "-i", str(list_file), *video_encode_args(cfg), "-an", "-movflags", "+faststart", str(output)], cfg)
    return {"ok": True, "output": str(output), "stdout": "", "stderr": ""}


def _fast_segment(project: Path, out_dir: Path, clip: dict, cfg: dict, index: int) -> Path:
    src = project / "media" / clip["file"]
    start, end = _source_range(clip)
    duration = max(0.1, end - start)
    out = out_dir / f"{index:03}_{int(start * 10):06d}_{src.name}"
    if out.exists() and out.stat().st_size > 1024 * 1024:
        return out
    subprocess.run([cfg["ffmpeg_path"], "-hide_banner", "-loglevel", "error", "-y", "-ss", str(start), "-t", str(duration), "-i", str(src), "-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-avoid_negative_ts", "make_zero", str(out)], check=True)
    return out


def unload_local_llm_model(cfg: dict) -> dict:
    if str(cfg.get("render", {}).get("stop_local_llm_server", "true")).lower() in {"0", "false", "no"}:
        return {"ok": True, "skipped": True}
    lms = shutil.which("lms")
    if not lms:
        return {"ok": False, "stderr": "找不到 lms CLI"}
    model = str(cfg.get("ai", {}).get("local", {}).get("model") or "")
    cmd = [lms, "unload", model] if model else [lms, "unload", "--all"]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return {"ok": proc.returncode == 0, "model": model, "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip()}


def stop_local_llm_server(cfg: dict) -> dict:
    return unload_local_llm_model(cfg)


def _duration(seg: dict) -> float:
    timeline_duration = seg.get("timeline_duration_seconds")
    if timeline_duration is not None:
        return max(0.1, float(timeline_duration))
    start, end = _source_range(seg)
    return max(0.1, (end - start) / max(0.01, float(seg.get("speed") or 1)))


def _source_range(segment: dict) -> tuple[float, float]:
    start = float(segment.get("source_in_seconds", segment.get("start_seconds", 0)) or 0)
    raw_end = segment.get("source_out_seconds", segment.get("end_seconds"))
    if raw_end is None:
        raw_end = start + float(segment.get("duration") or 0.1)
    return start, float(raw_end)


def _copy_bgm(data: dict, media: Path) -> dict | None:
    tracks = data.get("bgm") or []
    if not tracks:
        return None
    src = Path(str(tracks[0].get("source_path") or tracks[0].get("file_path") or ""))
    if not src.exists():
        return None
    dst = media / src.name
    if not dst.exists():
        shutil.copy2(src, dst)
    return {**tracks[0], "file": dst.name}


def _resolve_media_source(segment: dict, handoff: Path, project: Path) -> Path:
    """Resolve absolute sources and paths packaged by a prior handoff.

    Formal handoffs intentionally use package-relative paths such as
    ``assets/media/clip.mp4``.  Diagnostic plans usually contain absolute
    project-owned source paths.  Keep resolution bounded to those known local
    roots so a missing asset cannot become a dangling HTML reference.
    """

    raw_values = [segment.get("graded_clip"), segment.get("source_file"), segment.get("source_media_path")]
    roots = [handoff, project, project.parent / "opencut_handoff"]
    for raw in raw_values:
        if not raw:
            continue
        raw_path = Path(str(raw)).expanduser()
        candidates = [raw_path] if raw_path.is_absolute() else [root / raw_path for root in roots]
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved.is_file():
                return resolved
    display = next((str(value) for value in raw_values if value), "<empty>")
    raise HandoffError("file_missing", f"HyperFrames 來源素材不存在：{display}", action="恢復原始素材後重新匯出 HyperFrames 專案")


def _publish_media_copy(source: Path, destination: Path) -> None:
    """Copy a media asset without leaving a stale or partial destination."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        shutil.copy2(source, temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _html(title: str, clips: list[dict], bgm: dict | None, duration: float, visual_items: list[dict] | None = None) -> str:
    videos = "\n".join(
        f'<video id="hf-video-{index}" class="clip scene" data-start="{clip["timeline_start"]}" data-duration="{clip["duration"]}" data-track-index="0" data-media-start="{_source_range(clip)[0]}" src="media/{escape(clip["file"], quote=True)}" muted playsinline></video>'
        for index, clip in enumerate(clips, 1)
    )
    cards = []
    if visual_items:
        for item in visual_items:
            visual_type = str(item.get("type") or "visual")
            class_name = "lower-third" if visual_type == "lower_third" else "card"
            cards.append(
                f'<div class="clip {class_name}" data-visual-id="{escape(str(item.get("stable_id") or ""), quote=True)}" '
                f'data-start="{float(item.get("timeline_start") or 0):.3f}" data-duration="{float(item.get("duration") or 0):.3f}" data-animation="{escape(str(item.get("animation_id") or "static"), quote=True)}" data-track-index="1">{escape(str(item.get("text") or ""))}</div>'
            )
    else:
        seen = set()
        for clip in clips:
            group = clip.get("group", "")
            if group and group not in seen:
                seen.add(group)
                cards.append(f'<div class="clip card" data-start="{clip["timeline_start"]}" data-duration="2" data-track-index="1">{escape(group)}</div>')
    audio = f'<audio class="clip" data-start="0" data-duration="{duration}" data-track-index="2" data-volume="0.35" src="media/{escape(bgm["file"], quote=True)}"></audio>' if bgm else ""
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
html,body,#root{{margin:0;width:100%;height:100%;background:#050607;overflow:hidden;font-family:"Microsoft JhengHei",sans-serif}}
.scene{{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}}
.card,.lower-third{{position:absolute;left:56px;bottom:56px;color:white;background:rgba(0,0,0,.62);padding:18px 24px;border-radius:8px;font-size:44px;font-weight:700;letter-spacing:0;text-shadow:0 2px 10px #000}}
.lower-third{{bottom:18%;font-size:38px}}
</style>
</head>
<body>
<div id="root" data-composition-id="story" data-start="0" data-duration="{duration:.3f}" data-width="1920" data-height="1080">
{videos}
{''.join(cards)}
{audio}
</div>
<script>
const cards = Array.from(document.querySelectorAll(".card,.lower-third"));
const update = () => cards.forEach(el => {{
  const start = Number(el.dataset.start || 0);
  const duration = Math.max(1.4, Number(el.dataset.duration || 2));
  const visible = (window.__storyTime || 0) >= start && (window.__storyTime || 0) <= start + duration;
  if (!visible) {{ el.style.opacity = "0"; return; }}
  if (el.dataset.animation !== "fade-in-out") {{ el.style.opacity = "1"; return; }}
  const fade = Math.min(0.2, duration / 2);
  const elapsed = (window.__storyTime || 0) - start;
  el.style.opacity = String(Math.max(0, Math.min(1, Math.min(elapsed / fade, (duration - elapsed) / fade))));
}});
window.__timelines = window.__timelines || {{}};
window.__timelines["story"] = {{ update, setTime: time => {{ window.__storyTime = Number(time) || 0; update(); }} }};
update();
</script>
</body>
</html>"""


def _readme(out: Path) -> str:
    return "\n".join(
        [
            "# HyperFrames 初剪專案",
            "",
            "本交付包要求 tools/hyperframes 已依 package-lock 安裝；正式 render 不會使用 npx 自動下載。",
            "",
            "預覽（offline/no-install）：",
            "npx --no-install --prefix tools/hyperframes hyperframes preview",
            "",
            "輸出 MP4（offline/no-install）：",
            "npx --no-install --prefix tools/hyperframes hyperframes render --output story_draft.mp4",
            "",
            "若本機缺少固定版本 dependency，請先完成受控 setup；缺少時正式流程會 fail closed。",
            "",
            f"資料夾：{out}",
        ]
    )

from __future__ import annotations

from html import escape
from pathlib import Path
import json
import re
import shutil
import subprocess

from .opencut import export_opencut_handoff
from .paths import db_path
from .project import assert_project_approved, project_dir


def export_hyperframes_project(cfg: dict, db: Path, project_id: int, render_clips: bool = True, max_segments: int = 20) -> Path:
    handoff = export_opencut_handoff(cfg, db, project_id, render_clips=render_clips, max_segments=max_segments)
    data = json.loads((handoff / "opencut_handoff.json").read_text(encoding="utf-8"))
    out = project_dir(cfg, project_id) / "output" / "hyperframes"
    media = out / "media"
    media.mkdir(parents=True, exist_ok=True)

    clips = []
    t = 0.0
    for i, seg in enumerate(data.get("segments", []), 1):
        src = Path(seg.get("graded_clip") or seg["source_file"])
        dst = media / f"{i:03}_{src.name}"
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)
        speed = float(seg.get("speed", 1.0) or 1.0)
        source_in = 0.0 if seg.get("graded_clip") else float(seg.get("start_seconds", seg.get("source_in", 0)) or 0)
        source_out = float(seg.get("end_seconds", seg.get("source_out", source_in)) or source_in)
        duration = _duration(seg)
        clips.append({
            **seg,
            "file": dst.name,
            "source_in": round(source_in, 3),
            "source_out": round(source_out, 3),
            "timeline_start": round(t, 3),
            "duration": round(duration, 3),
            "speed": speed,
        })
        t += duration

    bgm = _copy_bgm(data, media)
    timeline = {
        "kind": "hyperframes_preview",
        "project": data["project"],
        "manifest": data.get("manifest"),
        "clips": clips,
        "title_cards": data.get("title_cards", []),
        "bgm": bgm,
        "duration": round(t, 3),
    }
    (out / "timeline.json").write_text(json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "index.html").write_text(_html(data["project"]["name"], clips, bgm, t, data.get("title_cards", [])), encoding="utf-8")
    (out / "README.md").write_text(_readme(out), encoding="utf-8")
    return out


def render_hyperframes_project(project: Path, output: Path | None = None, cfg: dict | None = None,
                               db: Path | None = None, project_id: int | None = None) -> dict:
    _assert_managed_project_approved(cfg, db, project, project_id, "HyperFrames MP4 輸出")
    output = output or project / "story_draft.mp4"
    npx = shutil.which("npx.cmd") or shutil.which("npx")
    if not npx:
        return {"ok": False, "output": str(output), "stdout": "", "stderr": "找不到 npx，請確認 Node.js/npm 已安裝並在 PATH 裡"}
    cmd = [npx, "-y", "hyperframes", "render", "--gpu", "--browser-gpu", "--output", str(output)]
    proc = subprocess.run(cmd, cwd=project, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return {"ok": proc.returncode == 0, "output": str(output), "stdout": proc.stdout, "stderr": proc.stderr}


def render_fast_draft(project: Path, cfg: dict, output: Path | None = None,
                      db: Path | None = None, project_id: int | None = None) -> dict:
    """Legacy Rough Preview only; this is never Accurate Preview or Final."""
    _assert_managed_project_approved(cfg, db, project, project_id, "HyperFrames 粗略預覽輸出")
    timeline = json.loads((project / "timeline.json").read_text(encoding="utf-8"))
    output = output or project / "story_draft_fast.mp4"
    list_file = project / "concat.txt"
    list_file.write_text("".join(f"file '{(project / 'media' / clip['file']).as_posix()}'\n" for clip in timeline["clips"]), encoding="utf-8")
    tmp = project / "story_draft_video.mp4"
    subprocess.run([cfg["ffmpeg_path"], "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", "-movflags", "+faststart", str(tmp)], check=True)
    bgm = timeline.get("bgm")
    if bgm:
        subprocess.run(
            [
                cfg["ffmpeg_path"],
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(tmp),
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
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-shortest",
                "-movflags",
                "+faststart",
                str(output),
            ],
            check=True,
        )
    else:
        shutil.copy2(tmp, output)
    return {"ok": True, "kind": "legacy_rough_preview", "output": str(output), "stdout": "", "stderr": ""}


def _assert_managed_project_approved(cfg: dict | None, db: Path | None, project: Path,
                                     project_id: int | None, action: str) -> None:
    """Apply the gate when called from a managed project or an explicit API call."""
    if cfg is None:
        return
    if project_id is None:
        match = re.search(r"project_(\d+)", str(project))
        if match:
            project_id = int(match.group(1))
    if project_id is None:
        return
    db = db or db_path(cfg)
    if db.exists():
        assert_project_approved(cfg, db, project_id, action)


def _duration(seg: dict) -> float:
    start = float(seg.get("start_seconds", seg.get("source_in", 0)) or 0)
    end = float(seg.get("end_seconds", seg.get("source_out", start)) or start)
    speed = float(seg.get("speed", 1.0) or 1.0)
    return max(0.1, (end - start) / speed)


def _copy_bgm(data: dict, media: Path) -> dict | None:
    tracks = data.get("bgm") or []
    if not tracks:
        return None
    src = Path(tracks[0].get("file_path", ""))
    if not src.exists():
        return None
    dst = media / src.name
    if not dst.exists():
        shutil.copy2(src, dst)
    return {**tracks[0], "file": dst.name}


def _html(title: str, clips: list[dict], bgm: dict | None, duration: float,
          title_cards: list[dict] | None = None) -> str:
    videos = "\n".join(
        f'<video class="clip scene" data-start="{clip["timeline_start"]}" data-duration="{clip["duration"]}" data-track-index="0" data-media-start="{clip.get("source_in", 0)}" src="media/{escape(clip["file"], quote=True)}" muted playsinline></video>'
        for clip in clips
    )
    cards = []
    seen = set()
    for card in title_cards or []:
        text = card.get("text") or card.get("title") or card.get("label")
        if not text:
            continue
        start = card.get("timeline_start", card.get("start_seconds", 0))
        if card.get("timeline_start_ms") is not None:
            start = float(card["timeline_start_ms"]) / 1000.0
        cards.append(f'<div class="clip card" data-start="{float(start):.3f}" data-duration="{float(card.get("duration", 2) or 2):.3f}" data-track-index="1">{escape(str(text))}</div>')
    for clip in clips:
        group = clip.get("group", "")
        if group and group not in seen and not title_cards:
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
.card{{position:absolute;left:56px;bottom:56px;color:white;background:rgba(0,0,0,.62);padding:18px 24px;border-radius:8px;font-size:44px;font-weight:700;letter-spacing:0;text-shadow:0 2px 10px #000}}
</style>
</head>
<body>
<div id="root" data-composition-id="story" data-start="0" data-width="1920" data-height="1080">
{videos}
{''.join(cards)}
{audio}
</div>
<script src="https://cdn.jsdelivr.net/npm/gsap@3/dist/gsap.min.js"></script>
<script>
const tl = gsap.timeline({{ paused: true }});
gsap.utils.toArray(".card").forEach(el => {{
  const start = Number(el.dataset.start || 0);
  tl.fromTo(el, {{ opacity: 0, y: 20 }}, {{ opacity: 1, y: 0, duration: .35 }}, start);
  tl.to(el, {{ opacity: 0, y: -12, duration: .35 }}, start + Math.max(1.4, Number(el.dataset.duration || 2) - .35));
}});
window.__timelines = window.__timelines || {{}};
window.__timelines["story"] = tl;
</script>
</body>
</html>"""


def _readme(out: Path) -> str:
    return "\n".join(
        [
            "# HyperFrames 初剪專案",
            "",
            "預覽：",
            "npx hyperframes preview",
            "",
            "輸出 MP4：",
            "npx hyperframes render --output story_draft.mp4",
            "",
            f"資料夾：{out}",
        ]
    )

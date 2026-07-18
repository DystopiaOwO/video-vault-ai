from __future__ import annotations

from html import escape
from pathlib import Path
import json
import shutil
import subprocess

from .opencut import export_opencut_handoff
from .project import project_dir


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
        duration = _duration(seg)
        clips.append({**seg, "file": dst.name, "timeline_start": round(t, 3), "duration": round(duration, 3)})
        t += duration

    bgm = _copy_bgm(data, media)
    (out / "timeline.json").write_text(json.dumps({"project": data["project"], "clips": clips, "bgm": bgm, "duration": round(t, 3)}, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "index.html").write_text(_html(data["project"]["name"], clips, bgm, t), encoding="utf-8")
    (out / "README.md").write_text(_readme(out), encoding="utf-8")
    return out


def render_hyperframes_project(project: Path, output: Path | None = None) -> dict:
    output = output or project / "story_draft.mp4"
    npx = shutil.which("npx.cmd") or shutil.which("npx")
    if not npx:
        return {"ok": False, "output": str(output), "stdout": "", "stderr": "找不到 npx，請確認 Node.js/npm 已安裝並在 PATH 裡"}
    cmd = [npx, "-y", "hyperframes", "render", "--gpu", "--browser-gpu", "--output", str(output)]
    proc = subprocess.run(cmd, cwd=project, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return {"ok": proc.returncode == 0, "output": str(output), "stdout": proc.stdout, "stderr": proc.stderr}


def render_fast_draft(project: Path, cfg: dict, output: Path | None = None) -> dict:
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
    return {"ok": True, "output": str(output), "stdout": "", "stderr": ""}


def _duration(seg: dict) -> float:
    if seg.get("graded_clip") and Path(seg["graded_clip"]).exists():
        return max(0.1, float(seg["end_seconds"]) - float(seg["start_seconds"]))
    return max(0.1, float(seg["end_seconds"]) - float(seg["start_seconds"]))


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


def _html(title: str, clips: list[dict], bgm: dict | None, duration: float) -> str:
    videos = "\n".join(
        f'<video class="clip scene" data-start="{clip["timeline_start"]}" data-duration="{clip["duration"]}" data-track-index="0" data-media-start="0" src="media/{escape(clip["file"], quote=True)}" muted playsinline></video>'
        for clip in clips
    )
    cards = []
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

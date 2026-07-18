from __future__ import annotations

from pathlib import Path
import json


def export_all(plan: dict, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / plan["timeline_name"]
    files = {
        "json": base.with_suffix(".json"),
        "edl": base.with_suffix(".edl"),
        "xml": base.with_suffix(".xml"),
    }
    files["json"].write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    files["edl"].write_text(to_edl(plan), encoding="utf-8")
    files["xml"].write_text(to_xml(plan), encoding="utf-8")
    return files


def planned_files(plan: dict, out_dir: Path) -> dict[str, Path]:
    base = out_dir / plan["timeline_name"]
    return {"json": base.with_suffix(".json"), "edl": base.with_suffix(".edl"), "xml": base.with_suffix(".xml")}


def to_edl(plan: dict) -> str:
    lines = [f"TITLE: {plan['timeline_name']}", "FCM: NON-DROP FRAME", ""]
    cursor = 0.0
    for i, clip in enumerate(plan["clips"], 1):
        duration = max(0.0, clip["end_seconds"] - clip["start_seconds"])
        lines += [
            f"{i:03}  AX       V     C        {_tc(clip['start_seconds'])} {_tc(clip['end_seconds'])} {_tc(cursor)} {_tc(cursor + duration)}",
            f"* FROM CLIP NAME: {Path(clip['source_file']).name}",
            f"* COMMENT: {clip['title']} / {clip['suggested_use']}",
            "",
        ]
        cursor += duration
    return "\n".join(lines)


def to_xml(plan: dict) -> str:
    clips = "\n".join(
        f'    <clip source="{_escape(clip["source_file"])}" start="{clip["start_seconds"]}" end="{clip["end_seconds"]}" title="{_escape(clip["title"])}" use="{_escape(clip["suggested_use"])}" />'
        for clip in plan["clips"]
    )
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<timeline name="{_escape(plan["timeline_name"])}">\n{clips}\n</timeline>\n'


def _tc(seconds: float, fps: int = 24) -> str:
    frames = int(round(seconds * fps))
    h, rem = divmod(frames, fps * 3600)
    m, rem = divmod(rem, fps * 60)
    s, f = divmod(rem, fps)
    return f"{h:02}:{m:02}:{s:02}:{f:02}"


def _escape(value: object) -> str:
    return str(value).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")

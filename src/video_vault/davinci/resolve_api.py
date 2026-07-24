from __future__ import annotations

import os
import sys
from pathlib import Path


def create_timeline(plan: dict) -> dict:
    try:
        _setup_windows_scripting_paths()
        import DaVinciResolveScript as dvr  # type: ignore
    except Exception as exc:
        return {"status": "skipped", "reason": f"DaVinci scripting unavailable: {exc}"}

    try:
        resolve = dvr.scriptapp("Resolve")
        if not resolve:
            return {"status": "skipped", "reason": "DaVinci Resolve is not running"}
        project_manager = resolve.GetProjectManager()
        project = project_manager.GetCurrentProject() or project_manager.CreateProject(plan["timeline_name"])
        media_pool = project.GetMediaPool()
        imported = media_pool.ImportMedia([clip["source_file"] for clip in plan["clips"]])
        timeline = media_pool.CreateEmptyTimeline(plan["timeline_name"])
        if timeline and imported:
            # ponytail: best-effort whole-clip append; precise in/out needs Resolve-specific clipInfo tuning later.
            media_pool.AppendToTimeline(imported)
        return {"status": "created", "timeline": plan["timeline_name"], "clips": len(plan["clips"])}
    except Exception as exc:
        return {"status": "skipped", "reason": f"DaVinci scripting failed: {exc}"}


def _setup_windows_scripting_paths() -> None:
    api = Path(os.environ.get("RESOLVE_SCRIPT_API", r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting"))
    lib = Path(os.environ.get("RESOLVE_SCRIPT_LIB", r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll"))
    modules = api / "Modules"
    if modules.exists() and str(modules) not in sys.path:
        sys.path.append(str(modules))
    if api.exists():
        os.environ.setdefault("RESOLVE_SCRIPT_API", str(api))
    if lib.exists():
        os.environ.setdefault("RESOLVE_SCRIPT_LIB", str(lib))

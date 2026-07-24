from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .database import frames, update_project_media_metadata, update_video_file
from .ffmpeg_tools import probe

CATEGORIES = ("matcha", "roasting", "travel", "coffee")


def rename_after_perception(cfg: dict, db: Path, video: dict) -> dict:
    """Apply post-perception naming without mutating shared project sources.

    Rows returned by ``project_videos()`` carry a ``project_media_uuid``. For
    those rows the generated name and category are project-local metadata only;
    the shared source path and global video row remain unchanged. Legacy
    non-project imports retain the existing physical rename behavior.
    """
    source = Path(video["current_path"])
    if not source.exists():
        return video
    category = detect_category(db, int(video["id"]), video.get("category") or "unknown")
    stamp = recording_time(source, cfg).strftime("%Y%m%d_%H%M%S")
    display_name = source.name if source.name.startswith(f"{stamp}_{category}_") else f"{stamp}_{category}_{source.name}"
    project_media_uuid = str(video.get("project_media_uuid") or "")
    if project_media_uuid:
        updated = update_project_media_metadata(
            db,
            project_media_uuid,
            display_name=display_name,
            category=category,
            analysis_status="perceived",
            increment_perception_revision=True,
        )
        if not updated:
            raise ValueError(f"project media not found: {project_media_uuid}")
        video.update(
            {
                "filename": display_name,
                "project_display_name": display_name,
                "category": category,
                "project_category": category,
                "status": "perceived",
                "project_analysis_status": "perceived",
                "perception_revision": int(video.get("perception_revision") or 0) + 1,
            }
        )
        return video

    if source.name.startswith(f"{stamp}_{category}_"):
        update_video_file(db, int(video["id"]), source, category)
        video.update({"current_path": str(source), "filename": source.name, "category": category})
        return video
    target = _unique(source.with_name(display_name))
    if target != source:
        source.rename(target)
    update_video_file(db, int(video["id"]), target, category)
    video.update({"current_path": str(target), "filename": target.name, "category": category})
    return video


def detect_category(db: Path, video_id: int, fallback: str = "unknown") -> str:
    text = ",".join(frame["tags"] or "" for frame in frames(db, video_id)).lower()
    for category in CATEGORIES:
        if category in text:
            return category
    if "dripping" in text or "steam" in text or "hands" in text:
        return "coffee"
    return fallback if fallback and fallback != "unknown" else "unknown"


def recording_time(path: Path, cfg: dict) -> datetime:
    try:
        data = probe(path, cfg)
        tags = [data.get("format", {}).get("tags", {})]
        tags += [stream.get("tags", {}) for stream in data.get("streams", [])]
        for tag in tags:
            value = tag.get("creation_time")
            if value:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
    except Exception:
        pass
    return datetime.fromtimestamp(path.stat().st_mtime)


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"cannot find unique filename for {path}")

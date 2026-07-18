from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil

from .database import upsert_video
from .ffmpeg_tools import metadata

CATEGORIES = ("coffee", "matcha", "roasting", "travel")


def guess_category(path: Path) -> str:
    text = path.name.lower()
    return next((c for c in CATEGORIES if c in text), "unknown")


def target_path(src: Path, cfg: dict, category: str) -> Path:
    stamp = datetime.fromtimestamp(src.stat().st_mtime).strftime("%Y%m%d_%H%M%S")
    name = f"{stamp}_unknown_{category}_{src.name}"
    return Path(cfg["library_root"]) / "01_sorted" / category / name


def ingest_file(src: Path, cfg: dict, db: Path, dry_run: bool = False) -> Path:
    category = guess_category(src)
    dst = target_path(src, cfg, category)
    if not dry_run:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if cfg["default_ingest_mode"] == "move":
            shutil.move(str(src), dst)
        else:
            shutil.copy2(src, dst)
        meta = metadata(dst, cfg)
        upsert_video(
            db,
            {
                "original_path": str(src),
                "current_path": str(dst),
                "filename": dst.name,
                "category": category,
                "created_at": datetime.fromtimestamp(dst.stat().st_mtime).isoformat(timespec="seconds"),
                **meta,
                "status": "indexed",
            },
        )
    return dst

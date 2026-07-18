from __future__ import annotations

from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".m4v"}


def scan_inbox(cfg: dict) -> list[Path]:
    inbox = Path(cfg["library_root"]) / cfg["inbox_dir"]
    if not inbox.exists():
        return []
    return sorted(p for p in inbox.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTS)

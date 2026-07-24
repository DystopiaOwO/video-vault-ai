from __future__ import annotations

from pathlib import Path

DIRS = [
    "00_inbox",
    "01_sorted/coffee",
    "01_sorted/matcha",
    "01_sorted/roasting",
    "01_sorted/travel",
    "01_sorted/unknown",
    "02_proxy",
    "03_frames",
    "04_audio",
    "05_index",
    "06_reports",
    "07_ai_suggestions",
    "08_projects",
    "99_exports",
]


def root(cfg: dict) -> Path:
    return Path(cfg["library_root"])


def ensure_library(cfg: dict) -> None:
    for name in DIRS:
        (root(cfg) / name).mkdir(parents=True, exist_ok=True)


def db_path(cfg: dict) -> Path:
    return root(cfg) / "05_index" / "video_vault.sqlite3"


def index_json_path(cfg: dict) -> Path:
    return root(cfg) / "05_index" / "index.json"

from __future__ import annotations

from pathlib import Path
import shutil

DEFAULT_CONFIG = {
    "library_root": "D:/VideoLibrary",
    "inbox_dir": "00_inbox",
    "frame_interval_seconds": 5,
    "frame_height": 720,
    "proxy_height": 1080,
    "default_ingest_mode": "copy",
    "ffmpeg_path": "ffmpeg",
    "ffprobe_path": "ffprobe",
    "ai": {
        "provider": "mock",
        "local": {"ollama_url": "http://localhost:11434", "model": "gemma4:12b"},
        "cloud": {"provider": "openai", "model": "gpt-4.1-mini", "api_key_env": "OPENAI_API_KEY"},
    },
}


def config_path(path: str | None = None) -> Path:
    return Path(path or "config.yaml")


def load_config(path: str | None = None) -> dict:
    file = config_path(path)
    data = _parse_yaml(file.read_text(encoding="utf-8")) if file.exists() else {}
    return _merge(DEFAULT_CONFIG, data or {})


def save_default_config(path: str | None = None) -> Path:
    file = config_path(path)
    if not file.exists():
        file.write_text(_dump_yaml(DEFAULT_CONFIG), encoding="utf-8")
    return file


def check_tools(cfg: dict) -> list[str]:
    missing = [name for name in ("ffmpeg_path", "ffprobe_path") if shutil.which(cfg[name]) is None]
    return missing


def _merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _parse_yaml(text: str) -> dict:
    # ponytail: tiny parser for this fixed config shape; use PyYAML if config grows lists/types.
    root: dict = {}
    stack: list[tuple[int, dict]] = [(-1, root)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        key, _, value = raw.strip().lstrip("\ufeff").partition(":")
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value.strip():
            parent[key] = _scalar(value.strip())
        else:
            parent[key] = {}
            stack.append((indent, parent[key]))
    return root


def _scalar(value: str):
    value = value.strip("'\"")
    if value.isdigit():
        return int(value)
    return value


def _dump_yaml(data: dict, indent: int = 0) -> str:
    lines = []
    for key, value in data.items():
        pad = " " * indent
        if isinstance(value, dict):
            lines.append(f"{pad}{key}:")
            lines.append(_dump_yaml(value, indent + 2).rstrip())
        else:
            lines.append(f'{pad}{key}: "{value}"' if isinstance(value, str) else f"{pad}{key}: {value}")
    return "\n".join(lines) + "\n"

from __future__ import annotations

from pathlib import Path
import re
import shutil

import yaml

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
        "local": {
            "base_url": "http://127.0.0.1:1234/v1",
            "model": "qwen/qwen2.5-vl-7b",
            "max_concurrent_requests": 1,
            "use_same_model_for_revision": True,
            "revision_model": "",
        },
        "cloud": {"provider": "openai", "model": "gpt-4.1-mini", "api_key_env": "OPENAI_API_KEY"},
    },
}


def config_path(path: str | None = None) -> Path:
    return Path(path or "config.yaml")


def load_config(path: str | None = None) -> dict:
    file = config_path(path)
    text = file.read_text(encoding="utf-8") if file.exists() else ""
    data = _safe_load_config(text, file) if text else {}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"Config root must be a mapping: {file}")
    return _merge(DEFAULT_CONFIG, data or {})


def _safe_load_config(text: str, file: Path) -> dict:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # Keep compatibility with old Windows config writers that emitted
        # unescaped backslashes inside double-quoted path values.
        repaired = re.sub(
            r'(?m)^(\s*[^:#\n]+:\s*)"([^"\n]*)"\s*$',
            lambda match: f'{match.group(1)}"{match.group(2).replace("\\", "\\\\")}"',
            text,
        )
        if repaired == text:
            raise ValueError(f"Invalid YAML config: {file}") from exc
        try:
            data = yaml.safe_load(repaired)
        except yaml.YAMLError as repaired_exc:
            raise ValueError(f"Invalid YAML config: {file}") from repaired_exc
    return data


def save_default_config(path: str | None = None) -> Path:
    file = config_path(path)
    if not file.exists():
        file.write_text(yaml.safe_dump(DEFAULT_CONFIG, allow_unicode=True, sort_keys=False), encoding="utf-8")
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



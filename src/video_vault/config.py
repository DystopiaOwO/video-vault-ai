from __future__ import annotations

from pathlib import Path
import shutil

DEFAULT_CONFIG = {
    "library_root": "D:/VideoLibrary",
    "inbox_dir": "00_inbox",
    "frame_interval_seconds": 5,
    "frame_height": 720,
    "sampling": {
        "policy_name": "adaptive-balanced",
        "policy_version": 1,
        "mode": "adaptive",
        "preset": "balanced",
        "baseline_interval_seconds": 5,
        "prescan_interval_seconds": 0.5,
        "dense_interval_seconds": 1,
        "scene_threshold": 0.32,
        "motion_threshold": 0.06,
        "min_interval_seconds": 0.25,
        "max_frames_per_clip": 180,
        "max_frames_per_minute": 30,
        "visual_dedupe_threshold": 0.985,
        "hardware_decode": "software",
    },
    "perception": {
        "multi_frame": {
            "enabled": True,
            "min_frames": 3,
            "max_frames": 5,
        },
        "audio": {
            "enabled": True,
            "policy_name": "local-pcm-vad-events",
            "policy_version": 1,
            "sample_rate": 16000,
            "window_seconds": 1.0,
            "hop_seconds": 0.5,
            "vad_threshold_db": -48.0,
            "max_analysis_seconds": 1800,
            "ffmpeg_timeout_seconds": 180,
        },
        "cloud_review": {
            "enabled": False,
            "provider": "mock",
            "confidence_threshold": 0.55,
            "max_calls_per_clip": 3,
            "max_frames_per_clip": 12,
            "max_calls_per_project": 6,
            "max_frames_per_project": 24,
            "estimated_cost_per_frame_usd": 0.0,
            "max_estimated_cost_usd_per_clip": 0.12,
            "max_estimated_cost_usd_per_project": 0.24,
            "timeout_seconds": 60,
        },
    },
    "proxy_height": 1080,
    "default_ingest_mode": "copy",
    "ffmpeg_path": "ffmpeg",
    "ffprobe_path": "ffprobe",
    "render": {
        "max_concurrent_jobs": 1,
        "minimum_free_disk_bytes": 0,
        "visual_font_path": "",
    },
    "delivery_qa": {
        "contract_version": "delivery-qa-v1",
        "timeout_seconds": 600,
        "threshold_overrides": {},
        "profiles": {},
    },
    "ai": {
        "provider": "mock",
        "local": {
            "base_url": "http://127.0.0.1:1234/v1",
            "model": "gemma4:12b",
        },
        "cloud": {"provider": "openai", "model": "gpt-4.1-mini", "api_key_env": "OPENAI_API_KEY"},
    },
    "story": {
        "provider": "mock",
        "model": "",
        "base_url": "http://127.0.0.1:1234/v1",
        "timeout_seconds": 180,
        "runtime_provisioning": {
            "enabled": False,
            "target_context_length": 0,
            "load_timeout_seconds": 180,
            "cleanup_timeout_seconds": 30,
        },
        "prompt_version": "project-story-v1",
        "schema_version": 1,
    },
}


def config_path(path: str | None = None) -> Path:
    return Path(path or "config.yaml")


def load_config(path: str | None = None) -> dict:
    file = config_path(path)
    data = _parse_yaml(file.read_text(encoding="utf-8")) if file.exists() else {}
    merged = _merge(DEFAULT_CONFIG, data or {})
    if file.exists() and "sampling" not in (data or {}):
        merged["sampling"] = {
            **merged["sampling"],
            "mode": "fixed",
            "baseline_interval_seconds": float(
                merged.get("frame_interval_seconds") or 5
            ),
            "migrated_from_fixed_interval": True,
        }
    local_source = ((data or {}).get("ai") or {}).get("local") or {}
    if local_source and "base_url" not in local_source:
        legacy_url = local_source.get("lmstudio_url") or local_source.get("ollama_url")
        if legacy_url:
            legacy_url = str(legacy_url).rstrip("/")
            if local_source.get("ollama_url") and not legacy_url.endswith("/v1"):
                legacy_url += "/v1"
            merged["ai"]["local"]["base_url"] = legacy_url
    return merged


def save_default_config(path: str | None = None) -> Path:
    file = config_path(path)
    if not file.exists():
        file.write_text(_dump_yaml(DEFAULT_CONFIG), encoding="utf-8")
    return file


def check_tools(cfg: dict) -> list[str]:
    missing = [name for name in ("ffmpeg_path", "ffprobe_path") if shutil.which(cfg[name]) is None]
    return missing


def parse_bool(value: object, *, default: bool = False) -> bool:
    """Normalize YAML/config booleans without treating ``"false"`` as true."""

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "on", "1"}:
            return True
        if normalized in {"false", "no", "off", "0"}:
            return False
    return default


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
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
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

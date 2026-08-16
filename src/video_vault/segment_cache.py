"""Deterministic hash cache for normalized single-segment outputs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


SEGMENT_RENDERER_CONTRACT_VERSION = 5


def encoder_cache_identity(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Return the deterministic encoder binding used by a segment cache key.

    A formal render has a resolved encoder contract.  Legacy/non-formal callers
    may only have a raw request, which remains explicit and is never confused
    with a resolved contract-bound artifact.
    """
    contract = settings.get("encoder_contract")
    if isinstance(contract, Mapping):
        return {
            "binding": "resolved_contract",
            "version": str(contract.get("version") or ""),
            "hash": str(contract.get("contract_hash") or ""),
            "implementation": str(contract.get("implementation") or ""),
        }
    return {
        "binding": "raw_request",
        "requested": str(settings.get("encoder") or "auto"),
    }


def cache_key_payload(
    manifest: Mapping[str, Any],
    segment: Mapping[str, Any],
    *,
    source_fingerprint: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source = Path(str(segment["source_file"])).expanduser().resolve()
    stat = source.stat() if source.exists() else None
    supplied_source = dict(source_fingerprint or {})
    profile = dict(manifest.get("profile") or {})
    settings = dict(manifest.get("settings") or {})
    color = dict(segment.get("color") or settings.get("color") or {})
    lut = Path(str(color.get("lut_path"))).expanduser().resolve() if color.get("lut_path") else None
    lut_stat = lut.stat() if lut and lut.exists() else None
    return {
        "contract_version": SEGMENT_RENDERER_CONTRACT_VERSION,
        "source_file": str(source),
        "source_size": stat.st_size if stat else None,
        "source_mtime_ns": stat.st_mtime_ns if stat else None,
        "source_sha256": str(supplied_source.get("sha256") or (_sha256_file(source) if source.is_file() else "")) or None,
        "segment_id": str(segment.get("segment_id") or ""),
        "source_in_seconds": float(segment.get("source_in_seconds") or 0),
        "source_out_seconds": float(segment.get("source_out_seconds") or 0),
        "speed": float(segment.get("speed") or 0),
        "segment_audio": {
            key: (segment.get("audio") or {}).get(key, segment.get("audio_role") if key == "role" else None)
            for key in ("role", "volume_db", "fade_in_seconds", "fade_out_seconds")
        },
        "preview_audio_envelope": {
            key: (segment.get("audio") or {}).get(key)
            for key in ("_preview_slice", "_timeline_offset_seconds", "_segment_timeline_duration_seconds")
        },
        "profile": {key: profile.get(key) for key in ("profile_id", "width", "height", "fps", "video_codec", "pixel_format", "audio_codec", "audio_sample_rate", "audio_channels")},
        # Keep the raw request for readability/backward diagnostics.  The
        # binding below is the cache identity that prevents a legacy auto/CPU
        # artifact from being reused by a resolved formal encoder contract.
        "encoder": settings.get("encoder", "auto"),
        "encoder_cache_identity": encoder_cache_identity(settings),
        "audio_codec": profile.get("audio_codec"),
        "audio_sample_rate": profile.get("audio_sample_rate"),
        "audio_channels": profile.get("audio_channels"),
        "segment_audio_pipeline_version": 2,
        "color_mode": color.get("mode", "none"),
        "color_settings": {key: color.get(key) for key in ("mode", "lut_kind", "exposure", "temperature", "tint", "contrast", "highlights", "shadows", "saturation", "gamma", "enabled")},
        "color_reference_id": str(segment.get("color_reference_id") or ""),
        "lut_path": str(lut) if lut else None,
        "lut_size": lut_stat.st_size if lut_stat else None,
        "lut_mtime_ns": lut_stat.st_mtime_ns if lut_stat else None,
        "lut_sha256": _sha256_file(lut) if lut and lut.is_file() else None,
    }


def build_segment_cache_key(
    manifest: Mapping[str, Any],
    segment: Mapping[str, Any],
    *,
    source_fingerprint: Mapping[str, Any] | None = None,
) -> str:
    payload = cache_key_payload(manifest, segment, source_fingerprint=source_fingerprint)
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def segment_cache_key(manifest: Mapping[str, Any], segment: Mapping[str, Any]) -> str:
    return build_segment_cache_key(manifest, segment)


def cache_paths(cache_root: str | Path, cache_key: str) -> dict[str, Path]:
    root = Path(cache_root)
    metadata = root / f"{cache_key}.json"
    return {
        "output": root / f"{cache_key}.mp4",
        "partial": root / f"{cache_key}.partial.mp4",
        "metadata": metadata,
        "metadata_temp": metadata.with_name(f".{metadata.name}.tmp"),
        "log": root / f"{cache_key}.log",
    }


def write_cache_metadata(path: Path, cache_key: str, payload: Mapping[str, Any], **extra: Any) -> None:
    temp = write_cache_metadata_temp(path, cache_key, payload, **extra)
    temp.replace(path)


def write_cache_metadata_temp(path: Path, cache_key: str, payload: Mapping[str, Any], **extra: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"cache_key": cache_key, "key_payload": dict(payload), **extra}
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return temp


def publish_cache_atomically(partial: Path, output: Path, metadata_temp: Path, metadata: Path) -> None:
    """Publish the two cache files and remove every incomplete artifact on failure."""
    try:
        partial.replace(output)
        metadata_temp.replace(metadata)
    except OSError:
        for path in (output, metadata, partial, metadata_temp):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def read_cache_metadata(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


__all__ = [
    "SEGMENT_RENDERER_CONTRACT_VERSION",
    "build_segment_cache_key",
    "cache_key_payload",
    "cache_paths",
    "encoder_cache_identity",
    "publish_cache_atomically",
    "read_cache_metadata",
    "segment_cache_key",
    "write_cache_metadata",
    "write_cache_metadata_temp",
]

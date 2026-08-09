"""Persistent and single-flight source fingerprint resolution.

The storyboard/status read path must never hash a large source file.  Explicit
thumbnail generation may resolve a missing fingerprint, but concurrent callers
share one in-flight full-file hash and subsequent calls reuse the result.
"""

from __future__ import annotations

from pathlib import Path
import hashlib
import json
import threading
from typing import Any, Mapping


_CONDITION = threading.Condition()
_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}
_IN_FLIGHT: set[tuple[Any, ...]] = set()
_METRICS = {
    "full_hash_calls": 0,
    "persisted_hits": 0,
    "memory_cache_hits": 0,
    "inflight_waits": 0,
}


def source_stat(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def parse_source_fingerprint(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def persisted_fingerprint_for_stat(path: Path, value: Any, *, stat: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    fingerprint = parse_source_fingerprint(value)
    sha256 = str(fingerprint.get("sha256") or "")
    if len(sha256) != 64:
        return None
    current = dict(stat or source_stat(path))
    try:
        if int(fingerprint.get("size")) != int(current["size"]):
            return None
        if int(fingerprint.get("mtime_ns")) != int(current["mtime_ns"]):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "path": str(path),
        "size": int(current["size"]),
        "mtime_ns": int(current["mtime_ns"]),
        "sha256": sha256,
    }


def peek_source_fingerprint(path: Path, persisted: Any = None) -> dict[str, Any] | None:
    """Return a current fingerprint without reading file contents."""

    path = path.expanduser().resolve()
    stat = source_stat(path)
    persisted_hit = persisted_fingerprint_for_stat(path, persisted, stat=stat)
    if persisted_hit is not None:
        with _CONDITION:
            _METRICS["persisted_hits"] += 1
        return persisted_hit
    key = _cache_key(path, stat)
    with _CONDITION:
        cached = _CACHE.get(key)
        if cached is not None:
            _METRICS["memory_cache_hits"] += 1
            return dict(cached)
    return None


def resolve_source_fingerprint(path: Path, persisted: Any = None) -> dict[str, Any]:
    """Resolve a full SHA-256, using persistence and single-flight caching."""

    path = path.expanduser().resolve(strict=True)
    stat = source_stat(path)
    persisted_hit = persisted_fingerprint_for_stat(path, persisted, stat=stat)
    key = _cache_key(path, stat)
    if persisted_hit is not None:
        with _CONDITION:
            _CACHE[key] = dict(persisted_hit)
            _METRICS["persisted_hits"] += 1
        return persisted_hit

    with _CONDITION:
        while True:
            cached = _CACHE.get(key)
            if cached is not None:
                _METRICS["memory_cache_hits"] += 1
                return dict(cached)
            if key not in _IN_FLIGHT:
                _IN_FLIGHT.add(key)
                break
            _METRICS["inflight_waits"] += 1
            _CONDITION.wait()

    try:
        digest = _sha256_file(path)
        result = {
            "path": str(path),
            "size": int(stat["size"]),
            "mtime_ns": int(stat["mtime_ns"]),
            "sha256": digest,
        }
        with _CONDITION:
            _CACHE[key] = dict(result)
            _METRICS["full_hash_calls"] += 1
            return result
    finally:
        with _CONDITION:
            _IN_FLIGHT.discard(key)
            _CONDITION.notify_all()


def reset_source_fingerprint_cache() -> None:
    with _CONDITION:
        _CACHE.clear()
        _IN_FLIGHT.clear()
        for key in _METRICS:
            _METRICS[key] = 0


def source_fingerprint_metrics() -> dict[str, int]:
    with _CONDITION:
        return {key: int(value) for key, value in _METRICS.items()}


def _cache_key(path: Path, stat: Mapping[str, Any]) -> tuple[Any, ...]:
    try:
        file_id = (int(path.stat().st_dev), int(path.stat().st_ino))
    except (OSError, TypeError, ValueError):
        file_id = (str(path),)
    if file_id == (0, 0):
        file_id = (str(path),)
    return (*file_id, int(stat["size"]), int(stat["mtime_ns"]))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


__all__ = [
    "parse_source_fingerprint",
    "peek_source_fingerprint",
    "persisted_fingerprint_for_stat",
    "reset_source_fingerprint_cache",
    "resolve_source_fingerprint",
    "source_fingerprint_metrics",
    "source_stat",
]

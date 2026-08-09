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


SOURCE_FINGERPRINT_CONTRACT_VERSION = "source-fingerprint-v2"
SOURCE_IDENTITY_CONTRACT = "stat-file-identity-v1"


class SourceFingerprintChangedError(RuntimeError):
    """The source changed while a full fingerprint was being calculated."""

    def __init__(self, path: Path, before: Mapping[str, Any], after: Mapping[str, Any]):
        self.path = path
        self.before = dict(before)
        self.after = dict(after)
        super().__init__(f"source changed during fingerprint: {path}")


_CONDITION = threading.Condition()
_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}
_IN_FLIGHT: set[tuple[Any, ...]] = set()
_METRICS = {
    "full_hash_calls": 0,
    "persisted_hits": 0,
    "memory_cache_hits": 0,
    "inflight_waits": 0,
}


def source_stat(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "source_identity": {
            "contract": SOURCE_IDENTITY_CONTRACT,
            "device": int(getattr(stat, "st_dev", 0) or 0),
            "inode": int(getattr(stat, "st_ino", 0) or 0),
            "ctime_ns": int(getattr(stat, "st_ctime_ns", 0) or 0),
        },
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
    if (
        str(fingerprint.get("contract_version") or "") != SOURCE_FINGERPRINT_CONTRACT_VERSION
        or len(sha256) != 64
    ):
        return None
    current = dict(stat or source_stat(path))
    try:
        if int(fingerprint.get("size")) != int(current["size"]):
            return None
        if int(fingerprint.get("mtime_ns")) != int(current["mtime_ns"]):
            return None
        if dict(fingerprint.get("source_identity") or {}) != dict(current["source_identity"]):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "contract_version": SOURCE_FINGERPRINT_CONTRACT_VERSION,
        "path": str(path),
        "size": int(current["size"]),
        "mtime_ns": int(current["mtime_ns"]),
        "source_identity": dict(current["source_identity"]),
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
        after = source_stat(path)
        if _source_version(after) != _source_version(stat):
            raise SourceFingerprintChangedError(path, stat, after)
        result = {
            "contract_version": SOURCE_FINGERPRINT_CONTRACT_VERSION,
            "path": str(path),
            "size": int(after["size"]),
            "mtime_ns": int(after["mtime_ns"]),
            "source_identity": dict(after["source_identity"]),
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
    identity = dict(stat.get("source_identity") or {})
    if not identity or not any(identity.get(key) for key in ("device", "inode", "ctime_ns")):
        identity = {"path": str(path)}
    return (
        identity.get("contract"),
        identity.get("device"),
        identity.get("inode"),
        identity.get("ctime_ns"),
        int(stat["size"]),
        int(stat["mtime_ns"]),
    )


def _source_version(stat: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        int(stat["size"]),
        int(stat["mtime_ns"]),
        tuple(sorted(dict(stat["source_identity"]).items())),
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


__all__ = [
    "SOURCE_FINGERPRINT_CONTRACT_VERSION",
    "SOURCE_IDENTITY_CONTRACT",
    "SourceFingerprintChangedError",
    "parse_source_fingerprint",
    "peek_source_fingerprint",
    "persisted_fingerprint_for_stat",
    "reset_source_fingerprint_cache",
    "resolve_source_fingerprint",
    "source_fingerprint_metrics",
    "source_stat",
]

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import time

import pytest

from video_vault.database import add_analysis, init_db, project_videos, upsert_video
from video_vault.project import build_project_plan, create_project
from video_vault.source_fingerprint import (
    SourceFingerprintChangedError,
    persisted_fingerprint_for_stat,
    reset_source_fingerprint_cache,
    resolve_source_fingerprint,
    source_fingerprint_metrics,
    source_stat,
)
from video_vault.storyboard import _thumbnail_path, generate_storyboard, storyboard_for_api
import video_vault.source_fingerprint as source_fingerprint_module


@pytest.fixture(autouse=True)
def _reset_fingerprint_cache():
    reset_source_fingerprint_cache()
    yield
    reset_source_fingerprint_cache()


def _large_sparse_source(tmp_path: Path) -> Path:
    source = tmp_path / "large-source.mp4"
    with source.open("wb") as stream:
        stream.truncate(2 * 1024 * 1024 * 1024 + 17)
    return source


def test_large_source_hash_count_does_not_scale_with_20_segments(tmp_path, monkeypatch):
    source = _large_sparse_source(tmp_path)
    calls: list[Path] = []

    def fake_hash(path: Path) -> str:
        calls.append(path)
        return "a" * 64

    monkeypatch.setattr(source_fingerprint_module, "_sha256_file", fake_hash)
    cfg = {"library_root": str(tmp_path)}
    paths = [
        _thumbnail_path(cfg, 1, source, float(index), float(index + 1), 0.5)
        for index in range(20)
    ]

    assert source.stat().st_size > 2 * 1024 * 1024 * 1024
    assert all(path is not None for path in paths)
    assert len(set(paths)) == 20
    assert len(calls) == 1
    assert source_fingerprint_metrics()["full_hash_calls"] == 1


def test_concurrent_thumbnail_requests_single_flight_large_source_hash(tmp_path, monkeypatch):
    source = _large_sparse_source(tmp_path)
    calls: list[Path] = []

    def fake_hash(path: Path) -> str:
        calls.append(path)
        time.sleep(0.05)
        return "b" * 64

    monkeypatch.setattr(source_fingerprint_module, "_sha256_file", fake_hash)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: resolve_source_fingerprint(source), range(8)))

    assert {item["sha256"] for item in results} == {"b" * 64}
    assert len(calls) == 1
    assert source_fingerprint_metrics()["full_hash_calls"] == 1
    assert source_fingerprint_metrics()["inflight_waits"] >= 1


def test_source_change_invalidates_thumbnail_key_and_rehashes_once(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"before-content")
    digests = iter(("c" * 64, "d" * 64))
    calls: list[Path] = []

    def fake_hash(path: Path) -> str:
        calls.append(path)
        return next(digests)

    monkeypatch.setattr(source_fingerprint_module, "_sha256_file", fake_hash)
    cfg = {"library_root": str(tmp_path)}
    before = _thumbnail_path(cfg, 1, source, 0, 1, 0.5)
    source.write_bytes(b"after-content")
    after = _thumbnail_path(cfg, 1, source, 0, 1, 0.5)

    assert before is not None and after is not None
    assert before != after
    assert len(calls) == 2


def test_same_path_replacement_same_size_and_mtime_does_not_reuse_fingerprint(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"old-data")
    digests = iter(("e" * 64, "f" * 64))
    monkeypatch.setattr(source_fingerprint_module, "_sha256_file", lambda _path: next(digests))

    old = resolve_source_fingerprint(source)
    old_stat = source_stat(source)
    replacement = tmp_path / "replacement.mp4"
    replacement.write_bytes(b"new-data")
    os.utime(replacement, ns=(old_stat["mtime_ns"], old_stat["mtime_ns"]))
    os.replace(replacement, source)

    new_stat = source_stat(source)
    assert new_stat["size"] == old_stat["size"]
    assert new_stat["mtime_ns"] == old_stat["mtime_ns"]
    assert new_stat["source_identity"] != old_stat["source_identity"]
    assert persisted_fingerprint_for_stat(source, old) is None
    assert _thumbnail_path({"library_root": str(tmp_path)}, 1, source, 0, 1, 0.5, source_fingerprint=old) is None

    new = resolve_source_fingerprint(source, old)
    assert new["sha256"] == "f" * 64
    assert new["source_identity"] == new_stat["source_identity"]
    assert source_fingerprint_metrics()["full_hash_calls"] == 2


def test_hardlink_identity_can_reuse_fingerprint_and_thumbnail_key(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"same-content")
    monkeypatch.setattr(source_fingerprint_module, "_sha256_file", lambda _path: "a" * 64)
    fingerprint = resolve_source_fingerprint(source)
    alias = tmp_path / "renamed-or-hardlinked.mp4"
    alias.hardlink_to(source)

    assert persisted_fingerprint_for_stat(alias, fingerprint) is not None
    cfg = {"library_root": str(tmp_path)}
    source_key = _thumbnail_path(cfg, 1, source, 0, 1, 0.5, source_fingerprint=fingerprint)
    alias_key = _thumbnail_path(cfg, 1, alias, 0, 1, 0.5, source_fingerprint=fingerprint)
    assert source_key == alias_key
    assert source_fingerprint_metrics()["full_hash_calls"] == 1


def test_source_replacement_during_hash_is_not_cached_or_persisted(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"12345678")
    before = source_stat(source)

    def replace_during_hash(path: Path) -> str:
        replacement = path.with_name("replacement-during-hash.mp4")
        replacement.write_bytes(b"abcdefgh")
        os.utime(replacement, ns=(before["mtime_ns"], before["mtime_ns"]))
        os.replace(replacement, path)
        return "a" * 64

    monkeypatch.setattr(source_fingerprint_module, "_sha256_file", replace_during_hash)
    with pytest.raises(SourceFingerprintChangedError):
        resolve_source_fingerprint(source)

    assert source_fingerprint_metrics()["full_hash_calls"] == 0
    monkeypatch.setattr(source_fingerprint_module, "_sha256_file", lambda _path: "b" * 64)
    resolved = resolve_source_fingerprint(source)
    assert resolved["sha256"] == "b" * 64
    assert source_fingerprint_metrics()["full_hash_calls"] == 1


def test_storyboard_api_uses_persisted_fingerprint_without_hashing(tmp_path, monkeypatch):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-for-api")
    video_id = upsert_video(
        db,
        {
            "original_path": str(source),
            "current_path": str(source),
            "filename": source.name,
            "category": "coffee",
            "duration_seconds": 30,
        },
    )
    add_analysis(
        db,
        video_id,
        "mock",
        "rules",
        {
            "segments": [
                {
                    "start_seconds": index,
                    "end_seconds": index + 1,
                    "segment_type": "detail",
                    "title": f"segment-{index}",
                    "reason": "test",
                    "tags": ["coffee"],
                    "score": 0.9,
                    "suggested_use": "main",
                }
                for index in range(20)
            ]
        },
        tmp_path / "raw.json",
    )
    cfg = {"library_root": str(tmp_path), "ffmpeg_path": "ffmpeg"}
    project_id = create_project(db, "API fingerprint", [video_id], category="coffee")
    build_project_plan(cfg, db, project_id)
    generate_storyboard(cfg, db, project_id)
    row = dict(project_videos(db, project_id)[0])
    assert json.loads(row["source_fingerprint_json"])["sha256"]
    reset_source_fingerprint_cache()

    def unexpected_hash(_path: Path) -> str:
        raise AssertionError("storyboard/status API must not hash the source")

    monkeypatch.setattr(source_fingerprint_module, "_sha256_file", unexpected_hash)
    started = time.perf_counter()
    result = storyboard_for_api(cfg, db, project_id)
    elapsed = time.perf_counter() - started

    assert len(result["segments"]) == 20
    assert elapsed < 2
    assert source_fingerprint_metrics()["full_hash_calls"] == 0

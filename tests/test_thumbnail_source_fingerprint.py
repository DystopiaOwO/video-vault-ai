from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time

import pytest

from video_vault.database import add_analysis, init_db, project_videos, upsert_video
from video_vault.project import build_project_plan, create_project
from video_vault.source_fingerprint import (
    reset_source_fingerprint_cache,
    resolve_source_fingerprint,
    source_fingerprint_metrics,
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

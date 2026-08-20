from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import time

import pytest

from video_vault.database import add_analysis, connect, init_db, project_videos, upsert_video
from video_vault.project import build_project_plan, create_project
from video_vault.project_media import revalidate_and_rebind_project_source_fingerprint
from video_vault.source_fingerprint import (
    SourceFingerprintChangedError,
    persisted_fingerprint_for_stat,
    reset_source_fingerprint_cache,
    resolve_source_fingerprint,
    revalidate_source_fingerprint,
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


def _project_media_binding(tmp_path: Path, source: Path) -> tuple[Path, int, str, dict]:
    db = tmp_path / "revalidation.sqlite3"
    init_db(db)
    video_id = upsert_video(db, {"original_path": str(source), "current_path": str(source), "filename": source.name, "category": "coffee", "duration_seconds": 10})
    project_id = create_project(db, "Revalidation", [video_id], category="coffee")
    fingerprint = resolve_source_fingerprint(source)
    with connect(db) as con:
        con.execute("update project_videos set source_fingerprint_json=? where project_id=? and video_id=?", (json.dumps(fingerprint, ensure_ascii=False, sort_keys=True), project_id, video_id))
    row = dict(project_videos(db, project_id)[0])
    return db, project_id, str(row["project_media_uuid"]), json.loads(row["source_fingerprint_json"])


def test_revalidation_strict_current_skips_hash_and_rebind(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"current-content")
    db, project_id, media_id, persisted = _project_media_binding(tmp_path, source)

    def unexpected_hash(_path: Path) -> str:
        raise AssertionError("strict current revalidation must not hash")

    monkeypatch.setattr(source_fingerprint_module, "_sha256_file", unexpected_hash)
    result = revalidate_and_rebind_project_source_fingerprint(db, project_id, media_id, source, persisted)

    assert result["status"] == "current"
    assert result["rebound"] is False
    assert result["full_hash"] is False
    assert dict(project_videos(db, project_id)[0])["source_fingerprint_json"] == json.dumps(persisted, ensure_ascii=False, sort_keys=True)


def test_device_or_inode_drift_same_sha_rebinds_without_changing_media_uuid(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"same-content")
    db, project_id, media_id, persisted = _project_media_binding(tmp_path, source)
    old_stat = source_stat(source)
    replacement = tmp_path / "replacement.mp4"
    replacement.write_bytes(b"same-content")
    os.utime(replacement, ns=(old_stat["mtime_ns"], old_stat["mtime_ns"]))
    os.replace(replacement, source)
    monkeypatch.setattr(source_fingerprint_module, "_sha256_file", lambda _path: persisted["sha256"])

    result = revalidate_and_rebind_project_source_fingerprint(db, project_id, media_id, source, persisted)
    row = dict(project_videos(db, project_id)[0])
    rebound = json.loads(row["source_fingerprint_json"])

    assert result["status"] == "rebound"
    assert result["content_equal"] is True
    assert result["rebound"] is True
    assert rebound["sha256"] == persisted["sha256"]
    assert rebound["source_identity"] == source_stat(source)["source_identity"]
    assert row["project_media_uuid"] == media_id


def test_same_size_same_mtime_different_bytes_revalidation_does_not_mutate_db(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"old-content")
    db, project_id, media_id, persisted = _project_media_binding(tmp_path, source)
    old_stat = source_stat(source)
    replacement = tmp_path / "replacement.mp4"
    replacement.write_bytes(b"new-content")
    os.utime(replacement, ns=(old_stat["mtime_ns"], old_stat["mtime_ns"]))
    os.replace(replacement, source)
    monkeypatch.setattr(source_fingerprint_module, "_sha256_file", lambda _path: "f" * 64)

    with pytest.raises(SourceFingerprintChangedError) as exc_info:
        revalidate_and_rebind_project_source_fingerprint(db, project_id, media_id, source, persisted)

    assert exc_info.value.reason == "content_sha_mismatch"
    with connect(db) as con:
        stored = con.execute("select source_fingerprint_json from project_videos where project_id=? and project_media_uuid=?", (project_id, media_id)).fetchone()[0]
    assert json.loads(stored) == persisted


def test_invalid_persisted_sha_cannot_rebind(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"content")
    invalid = {"contract_version": "source-fingerprint-v2", "sha256": "short", "size": source.stat().st_size, "mtime_ns": source.stat().st_mtime_ns, "source_identity": source_stat(source)["source_identity"]}
    monkeypatch.setattr(source_fingerprint_module, "_sha256_file", lambda _path: (_ for _ in ()).throw(AssertionError("invalid persisted evidence must not hash")))

    with pytest.raises(SourceFingerprintChangedError) as exc_info:
        revalidate_source_fingerprint(source, invalid)

    assert exc_info.value.reason == "invalid_persisted_fingerprint"


def test_concurrent_revalidation_uses_one_full_hash(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"same-content")
    old_stat = source_stat(source)
    persisted = {"contract_version": "source-fingerprint-v2", "sha256": "a" * 64, "size": old_stat["size"], "mtime_ns": old_stat["mtime_ns"], "source_identity": old_stat["source_identity"]}
    replacement = tmp_path / "replacement.mp4"
    replacement.write_bytes(b"same-content")
    os.utime(replacement, ns=(old_stat["mtime_ns"], old_stat["mtime_ns"]))
    os.replace(replacement, source)
    calls: list[Path] = []

    def fake_hash(path: Path) -> str:
        calls.append(path)
        time.sleep(0.05)
        return "a" * 64

    monkeypatch.setattr(source_fingerprint_module, "_sha256_file", fake_hash)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: revalidate_source_fingerprint(source, persisted), range(8)))

    assert len(calls) == 1
    assert {item["status"] for item in results} == {"rebound"}
    assert source_fingerprint_metrics()["full_hash_calls"] == 1


def test_rebind_db_failure_rolls_back_fingerprint_atomically(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"same-content")
    db, project_id, media_id, persisted = _project_media_binding(tmp_path, source)
    old_stat = source_stat(source)
    replacement = tmp_path / "replacement.mp4"
    replacement.write_bytes(b"same-content")
    os.utime(replacement, ns=(old_stat["mtime_ns"], old_stat["mtime_ns"]))
    os.replace(replacement, source)
    monkeypatch.setattr(source_fingerprint_module, "_sha256_file", lambda _path: persisted["sha256"])
    with connect(db) as con:
        con.execute("create trigger reject_source_rebind before update of source_fingerprint_json on project_videos begin select raise(abort, 'reject test rebind'); end")

    with pytest.raises(sqlite3.IntegrityError, match="reject test rebind"):
        revalidate_and_rebind_project_source_fingerprint(db, project_id, media_id, source, persisted)

    with connect(db) as con:
        stored = con.execute("select source_fingerprint_json from project_videos where project_id=? and project_media_uuid=?", (project_id, media_id)).fetchone()[0]
    assert json.loads(stored) == persisted


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

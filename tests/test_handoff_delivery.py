from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
from types import SimpleNamespace

from video_vault.handoff import (
    build_handoff_manifest,
    escape_ffconcat_path,
    import_handoff_package,
    register_handoff_file,
    write_handoff_manifest,
)


def _snapshot(segments):
    return {
        "snapshot_id": "approval-test",
        "snapshot_hash": "snapshot-hash",
        "manifest_hash": "manifest-hash",
        "approved_project_revision": 4,
        "assets": [],
        "manifest": {
            "project_id": 1,
            "project_name": "旅遊",
            "schema_version": "2.0",
            "profile": {"profile_id": "preview_1080p30"},
            "settings": {},
            "segments": segments,
            "visual_items": [{"stable_id": "title-1", "runtime_assets": []}],
            "bgm": [],
            "bgm_credits": [],
            "manifest_hash": "manifest-hash",
        },
    }


def _segments(count=21):
    return [
        {
            "segment_id": f"clip_{index:03}",
            "clip_id": f"clip_{index:03}",
            "video_id": 1,
            "order": index,
            "source_file": f"C:/素材/旅遊 {index}.mp4",
            "source_in_seconds": 0,
            "source_out_seconds": 1,
            "speed": 1,
            "timeline_duration_seconds": 1,
            "group": "day" if index < 11 else "night",
        }
        for index in range(1, count + 1)
    ]


def test_complete_mode_has_no_implicit_twenty_item_cap(monkeypatch, tmp_path):
    snapshot = _snapshot(_segments())
    monkeypatch.setattr("video_vault.handoff.load_approved_handoff_snapshot", lambda cfg, db, project_id: {"snapshot": snapshot, "manifest": snapshot["manifest"]})
    result = build_handoff_manifest({"library_root": str(tmp_path)}, tmp_path / "db.sqlite3", 1, mode="complete")
    assert len(result["exported_ids"]) == 21
    assert result["omitted_ids"] == []
    assert result["selection_mode"] == "complete"


def test_diagnostic_first_n_is_explicit(monkeypatch, tmp_path):
    snapshot = _snapshot(_segments())
    monkeypatch.setattr("video_vault.handoff.load_approved_handoff_snapshot", lambda *args: {"snapshot": snapshot, "manifest": snapshot["manifest"]})
    result = build_handoff_manifest({"library_root": str(tmp_path)}, tmp_path / "db.sqlite3", 1, mode="diagnostic_first_n", first_n=3)
    assert result["handoff_type"] == "diagnostic"
    assert result["exported_ids"] == ["clip_001", "clip_002", "clip_003"]
    assert len(result["omitted_ids"]) == 18
    assert "diagnostic" in result["non_formal_reason"]


def test_same_approved_input_has_same_handoff_identity_and_contract_hash(monkeypatch, tmp_path):
    snapshot = _snapshot(_segments(2))
    monkeypatch.setattr("video_vault.handoff.load_approved_handoff_snapshot", lambda *args: {"snapshot": snapshot, "manifest": snapshot["manifest"]})
    timestamps = iter(
        [
            datetime(2026, 7, 28, 1, tzinfo=timezone.utc),
            datetime(2026, 7, 28, 2, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(
        "video_vault.handoff.datetime",
        SimpleNamespace(now=lambda _timezone: next(timestamps)),
    )
    first = build_handoff_manifest({}, tmp_path / "db.sqlite3", 1, mode="complete")
    second = build_handoff_manifest({}, tmp_path / "db.sqlite3", 1, mode="complete")
    assert first["created_at"] != second["created_at"]
    assert first["handoff_id"] == second["handoff_id"]
    assert first["contract_hash"] == second["contract_hash"]


def test_ffconcat_path_handles_unicode_apostrophe_and_windows_style(tmp_path):
    path = tmp_path / "LUT Files, semi; [final] '中文'.mp4"
    path.write_bytes(b"x")
    escaped = escape_ffconcat_path(path)
    assert "中文" in escaped
    assert "'\\''" in escaped
    assert "\\" not in escaped or "/" in escaped


def test_import_hash_conflict_does_not_modify_project(tmp_path):
    package = tmp_path / "handoff"
    package.mkdir()
    (package / "handoff_manifest.json").write_text('{"contract_version":"handoff-v1","contract_hash":"wrong","exported_ids":["clip_1"]}', encoding="utf-8")
    result = import_handoff_package({"library_root": str(tmp_path)}, 1, package)
    assert result["ok"] is False
    assert result["code"] == "manifest_conflict"
    assert result["report"]["status"] == "needs-review"
    assert not (tmp_path / "source").exists()


def test_import_checks_each_file_hash(tmp_path):
    package = tmp_path / "handoff"
    package.mkdir()
    media = package / "clip '中文'.mp4"
    media.write_bytes(b"approved")
    manifest = {
        "contract_version": "handoff-v1",
        "handoff_id": "handoff-test",
        "created_at": "now",
        "files": [{"path": media.name, "size": media.stat().st_size, "sha256": hashlib.sha256(media.read_bytes()).hexdigest()}],
        "exported_ids": ["clip-1"],
    }
    payload = {key: value for key, value in manifest.items() if key not in {"contract_hash", "created_at", "handoff_id"}}
    manifest["contract_hash"] = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    (package / "handoff_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    assert import_handoff_package({"library_root": str(tmp_path)}, 1, package)["ok"] is True
    media.write_bytes(b"tampered")
    result = import_handoff_package({"library_root": str(tmp_path)}, 1, package)
    assert result["ok"] is False
    assert media.name in result["report"]["changed"]


def test_nested_handoff_file_round_trips_with_relative_path(tmp_path):
    package = tmp_path / "handoff"
    nested = package / "graded_clips" / "nested.mp4"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"approved nested clip")
    manifest = {
        "contract_version": "handoff-v1",
        "handoff_id": "handoff-nested",
        "created_at": "2026-07-28T00:00:00+00:00",
        "exported_ids": ["clip-1"],
        "files": [],
        "file_hashes": {},
    }

    register_handoff_file(
        manifest,
        nested,
        package_root=package,
        stable_id="clip-1",
    )
    write_handoff_manifest(package / "handoff_manifest.json", manifest)

    assert manifest["files"][0]["path"] == "graded_clips/nested.mp4"
    assert "graded_clips/nested.mp4" in manifest["file_hashes"]
    result = import_handoff_package({"library_root": str(tmp_path)}, 1, package)
    assert result["ok"] is True
    assert result["report"]["status"] == "matched"

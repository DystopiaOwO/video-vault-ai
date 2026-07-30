from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from video_vault.artifact_retention import (
    RetentionError,
    build_cleanup_plan,
    ensure_render_free_space,
    execute_cleanup_plan,
    load_inventory,
    reconcile_inventory,
    register_artifact,
    save_inventory,
)
from video_vault.project import project_dir


def _old(value: int = 60) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=value)).isoformat(timespec="seconds")


def test_source_and_pinned_artifacts_are_protected(tmp_path):
    cfg = {"library_root": str(tmp_path)}
    project_dir(cfg, 1)
    source = project_dir(cfg, 1) / "source" / "original.mp4"
    source.write_bytes(b"source")
    preview = project_dir(cfg, 1) / "output" / "preview.mp4"
    preview.write_bytes(b"preview")
    register_artifact(cfg, 1, source, "source_media")
    pinned = register_artifact(cfg, 1, preview, "preview", pinned=True)
    inventory = load_inventory(cfg, 1)
    for record in inventory["artifacts"]:
        record["updated_at"] = _old()
    save_inventory(cfg, 1, inventory)

    plan = build_cleanup_plan(cfg, 1, {"preview_max_age_days": 1})
    assert all(item["artifact_id"] != pinned["artifact_id"] for item in plan["candidates"])
    assert source.exists() and preview.exists()


def test_dry_run_and_execute_use_same_candidate_ids(tmp_path):
    cfg = {"library_root": str(tmp_path)}
    project_dir(cfg, 1)
    preview = project_dir(cfg, 1) / "output" / "old.mp4"
    preview.write_bytes(b"old")
    record = register_artifact(cfg, 1, preview, "preview")
    inventory = load_inventory(cfg, 1)
    inventory["artifacts"][0]["updated_at"] = _old()
    save_inventory(cfg, 1, inventory)
    plan = build_cleanup_plan(cfg, 1, {"preview_max_age_days": 1})
    assert [item["artifact_id"] for item in plan["candidates"]] == [record["artifact_id"]]
    result = execute_cleanup_plan(cfg, plan)
    assert result["results"][0]["status"] == "deleted"
    assert not preview.exists()


def test_cleanup_plan_becomes_stale_when_reference_graph_changes(tmp_path):
    cfg = {"library_root": str(tmp_path)}
    project_dir(cfg, 1)
    preview = project_dir(cfg, 1) / "output" / "old.mp4"
    preview.write_bytes(b"old")
    register_artifact(cfg, 1, preview, "preview")
    inventory = load_inventory(cfg, 1)
    inventory["artifacts"][0]["updated_at"] = _old()
    save_inventory(cfg, 1, inventory)
    plan = build_cleanup_plan(cfg, 1, {"preview_max_age_days": 1})
    inventory = load_inventory(cfg, 1)
    inventory["artifacts"][0]["references"] = ["new-approval"]
    save_inventory(cfg, 1, inventory)
    with pytest.raises(RetentionError, match="過期"):
        execute_cleanup_plan(cfg, plan)
    assert preview.exists()


def test_cleanup_never_removes_project_source_media(tmp_path):
    cfg = {"library_root": str(tmp_path)}
    folder = project_dir(cfg, 1)
    source = folder / "source" / "clip.mp4"
    source.write_bytes(b"source")
    plan = build_cleanup_plan(cfg, 1, {"preview_max_age_days": 0, "cache_max_age_days": 0})
    assert not any(item["path"] == str(source.resolve()) for item in plan["candidates"])
    assert source.exists()


def test_current_lifecycle_is_protected_without_explicit_references(tmp_path):
    cfg = {"library_root": str(tmp_path)}
    project_dir(cfg, 1)
    output = project_dir(cfg, 1) / "output" / "final.mp4"
    output.write_bytes(b"final")
    record = register_artifact(cfg, 1, output, "formal_output", state="current")
    inventory = load_inventory(cfg, 1)
    next(item for item in inventory["artifacts"] if item["artifact_id"] == record["artifact_id"])["updated_at"] = _old()
    save_inventory(cfg, 1, inventory)
    plan = build_cleanup_plan(cfg, 1, {"preview_max_age_days": 1, "cache_max_age_days": 1})
    assert all(item["artifact_id"] != record["artifact_id"] for item in plan["candidates"])
    assert output.exists()


def test_active_producer_job_protects_artifact_without_status_metadata(tmp_path):
    cfg = {"library_root": str(tmp_path)}
    project_dir(cfg, 1)
    preview = project_dir(cfg, 1) / "output" / "active-preview.mp4"
    preview.write_bytes(b"preview")
    record = register_artifact(cfg, 1, preview, "preview", producer_job_id="job-42")
    inventory = load_inventory(cfg, 1)
    stored = next(item for item in inventory["artifacts"] if item["artifact_id"] == record["artifact_id"])
    stored["updated_at"] = _old()
    stored["producer_job_status"] = ""
    save_inventory(cfg, 1, inventory)

    plan = build_cleanup_plan(cfg, 1, {"preview_max_age_days": 1}, active_job_ids={"job-42"})

    assert all(item["artifact_id"] != record["artifact_id"] for item in plan["candidates"])
    assert any(item["artifact_id"] == record["artifact_id"] for item in plan["protected"])
    assert preview.exists()


def test_reconcile_recovers_inventory_after_file_disappears(tmp_path):
    cfg = {"library_root": str(tmp_path)}
    preview = project_dir(cfg, 1) / "output" / "interrupted.mp4"
    preview.write_bytes(b"preview")
    record = register_artifact(cfg, 1, preview, "preview")
    preview.unlink()

    result = reconcile_inventory(cfg, 1)

    assert result["recovered"] == 1
    stored = next(
        item
        for item in load_inventory(cfg, 1)["artifacts"]
        if item["artifact_id"] == record["artifact_id"]
    )
    assert stored["deletion_status"] == "missing"
    assert stored["lifecycle_state"] == "missing"


def test_cleanup_journals_each_deleted_file_before_next_candidate(monkeypatch, tmp_path):
    cfg = {"library_root": str(tmp_path)}
    folder = project_dir(cfg, 1) / "output"
    first = folder / "first.mp4"
    second = folder / "second.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    first_record = register_artifact(cfg, 1, first, "preview")
    register_artifact(cfg, 1, second, "preview")
    inventory = load_inventory(cfg, 1)
    for item in inventory["artifacts"]:
        item["updated_at"] = _old()
    save_inventory(cfg, 1, inventory)
    plan = build_cleanup_plan(
        cfg,
        1,
        {"preview_max_age_days": 1, "keep_last_n": 0},
    )
    real_unlink = Path.unlink

    def interrupt_second(path, *args, **kwargs):
        if path == second:
            raise KeyboardInterrupt()
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", interrupt_second)
    with pytest.raises(KeyboardInterrupt):
        execute_cleanup_plan(cfg, plan)

    stored = next(
        item
        for item in load_inventory(cfg, 1)["artifacts"]
        if item["artifact_id"] == first_record["artifact_id"]
    )
    assert stored["deletion_status"] == "deleted"
    assert not first.exists()
    assert second.exists()

    monkeypatch.setattr(Path, "unlink", real_unlink)
    resumed = execute_cleanup_plan(cfg, plan)
    assert [item["status"] for item in resumed["results"]] == [
        "already_deleted",
        "deleted",
    ]
    assert not second.exists()

    retried = execute_cleanup_plan(cfg, plan)
    assert [item["status"] for item in retried["results"]] == [
        "already_deleted",
        "already_deleted",
    ]
    assert retried["reclaimed_bytes"] == 0


def test_capacity_policy_selects_oldest_unreferenced_cache(tmp_path):
    cfg = {"library_root": str(tmp_path)}
    cache = project_dir(cfg, 1) / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    old = cache / "old.mp4"
    recent = cache / "recent.mp4"
    old.write_bytes(b"1234")
    recent.write_bytes(b"5678")
    old_record = register_artifact(cfg, 1, old, "segment_cache")
    register_artifact(cfg, 1, recent, "segment_cache")
    inventory = load_inventory(cfg, 1)
    inventory["artifacts"][0]["last_accessed_at"] = _old()
    save_inventory(cfg, 1, inventory)

    plan = build_cleanup_plan(
        cfg,
        1,
        {"max_cache_size_bytes": 4, "keep_last_n": 0},
    )

    assert [item["artifact_id"] for item in plan["candidates"]] == [
        old_record["artifact_id"]
    ]


def test_render_preflight_stops_before_expensive_work_when_disk_is_low(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "video_vault.artifact_retention.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=1024),
    )
    with pytest.raises(RetentionError, match="磁碟空間不足"):
        ensure_render_free_space(
            {"render": {"minimum_free_disk_bytes": 2048}},
            {"segments": [{"timeline_duration_seconds": 1, "source_file": ""}]},
            tmp_path / "out.mp4",
        )

from datetime import datetime, timedelta, timezone

import pytest

from video_vault.artifact_retention import RetentionError, build_cleanup_plan, execute_cleanup_plan, load_inventory, register_artifact, save_inventory
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

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import urllib.request

import pytest

from video_vault.database import add_analysis, connect, init_db, upsert_video
from video_vault.project import build_project_plan, create_project, project_dir, project_detail, save_segment_review
from video_vault.project_lifecycle import CancellationRequested, ProjectRevisionConflict, current_revision
from video_vault.story_calibration import calibration_path, compute_calibration, reset_calibration
from video_vault.story_generation import (
    LocalTextStoryProvider,
    StoryGenerationError,
    StoryValidationError,
    apply_story_generation_to_storyboard,
    generate_project_story,
    get_story_generation,
    normalize_story_output,
    recover_interrupted_story_generations,
    update_story_generation_review,
    validate_story_output,
)
from video_vault.story_input import build_story_input_snapshot, story_input_hash
from video_vault.story_profiles import CreatorProfileRevisionConflict, StorySettingsRevisionConflict, load_creator_profile, load_project_story_settings, save_creator_profile, save_project_story_settings
from video_vault.storyboard import generate_storyboard, load_storyboard


def _fixture(tmp_path: Path, *, second_project: bool = False):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    source = tmp_path / "travel.mp4"
    source.write_bytes(b"immutable-source")
    video_id = upsert_video(
        db,
        {
            "original_path": str(source),
            "current_path": str(source),
            "filename": source.name,
            "category": "travel",
            "duration_seconds": 30,
            "status": "uploaded",
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
                    "start_seconds": 0,
                    "end_seconds": 8,
                    "segment_type": "scene",
                    "title": "抵達車站",
                    "reason": "arrival",
                    "tags": ["travel"],
                    "score": 0.9,
                    "suggested_use": "main",
                },
                {
                    "start_seconds": 10,
                    "end_seconds": 18,
                    "segment_type": "detail",
                    "title": "咖啡廳",
                    "reason": "cafe",
                    "tags": ["food"],
                    "score": 0.8,
                    "suggested_use": "B-roll",
                },
            ]
        },
        tmp_path / "raw.json",
    )
    project_id = create_project(db, "旅行故事", [video_id], category="travel", content_type="travel_diary")
    cfg = {
        "library_root": str(tmp_path),
        "story": {"provider": "mock"},
    }
    build_project_plan(cfg, db, project_id)
    generate_storyboard(cfg, db, project_id)
    other_id = create_project(db, "另一個專案", [video_id], category="travel", content_type="travel_diary") if second_project else None
    if other_id:
        build_project_plan(cfg, db, other_id)
        generate_storyboard(cfg, db, other_id)
    return cfg, db, project_id, other_id


def test_story_input_is_deterministic_and_has_no_media_paths(tmp_path: Path):
    cfg, db, project_id, _ = _fixture(tmp_path)
    first = build_story_input_snapshot(cfg, db, project_id)
    second = build_story_input_snapshot(cfg, db, project_id)
    assert first == second
    assert first["input_hash"] == story_input_hash(first)

    def assert_safe(value):
        if isinstance(value, dict):
            assert not set(value) & {"source_file", "source_path", "frame_path", "image_url", "image_base64", "frame_bytes"}
            for child in value.values():
                assert_safe(child)
        elif isinstance(value, list):
            for child in value:
                assert_safe(child)

    assert_safe(first)


def test_story_input_hash_changes_with_human_story_settings(tmp_path: Path):
    cfg, db, project_id, _ = _fixture(tmp_path)
    before = build_story_input_snapshot(cfg, db, project_id)
    settings = load_project_story_settings(cfg, db, project_id)
    save_project_story_settings(cfg, db, project_id, {**settings, "desired_pacing": "更慢、保留等待"})
    after = build_story_input_snapshot(cfg, db, project_id)
    assert after["input_hash"] != before["input_hash"]


def test_profile_versions_change_story_input_hash_but_raw_evidence_does_not(tmp_path: Path):
    cfg, db, project_id, _ = _fixture(tmp_path)
    with connect(db) as con:
        video_id = int(con.execute("select video_id from project_videos where project_id=?", (project_id,)).fetchone()[0])
    before = build_story_input_snapshot(cfg, db, project_id)
    save_creator_profile(cfg, {"wording_style": "更短句", "title_card_density": "medium"})
    creator_changed = build_story_input_snapshot(cfg, db, project_id)
    assert creator_changed["input_hash"] != before["input_hash"]

    settings = load_project_story_settings(cfg, db, project_id)
    save_project_story_settings(cfg, db, project_id, {**settings, "profile_id": "general_diary"})
    profile_changed = build_story_input_snapshot(cfg, db, project_id)
    assert profile_changed["input_hash"] != creator_changed["input_hash"]

    add_analysis(db, video_id, "mock", "new-raw", {"segments": []}, tmp_path / "changed-raw.json")
    raw_changed = build_story_input_snapshot(cfg, db, project_id)
    assert raw_changed["input_hash"] == profile_changed["input_hash"]


def test_story_generation_is_cached_and_does_not_auto_apply_storyboard(tmp_path: Path):
    cfg, db, project_id, _ = _fixture(tmp_path)
    before_storyboard = load_storyboard(cfg, project_id)
    before_revision = current_revision(db, project_id)
    first = generate_project_story(cfg, db, project_id)
    assert first["status"] == "succeeded"
    assert first["cache_hit"] is False
    assert load_storyboard(cfg, project_id) == before_storyboard
    assert current_revision(db, project_id) == before_revision

    second = generate_project_story(cfg, db, project_id)
    assert second["status"] == "succeeded"
    assert second["cache_hit"] is True
    assert second["input_hash"] == first["input_hash"]


def test_generate_story_does_not_run_destructive_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg, db, project_id, _ = _fixture(tmp_path)

    def recovery_must_not_run(*_args, **_kwargs):
        raise AssertionError("generation must not perform destructive recovery")

    monkeypatch.setattr("video_vault.story_generation.recover_interrupted_story_generations", recovery_must_not_run)
    result = generate_project_story(cfg, db, project_id)
    assert result["status"] == "succeeded"


def test_story_settings_do_not_advance_render_revision_or_touch_approval(tmp_path: Path):
    cfg, db, project_id, _ = _fixture(tmp_path)
    before_revision = current_revision(db, project_id)
    review_path = project_dir(cfg, project_id) / "review_status.json"
    before_review = review_path.read_bytes() if review_path.is_file() else None
    settings = load_project_story_settings(cfg, db, project_id)
    save_project_story_settings(cfg, db, project_id, {**settings, "project_intent": "下一次生成才套用的意圖"}, base_revision=before_revision)
    assert current_revision(db, project_id) == before_revision
    assert (review_path.read_bytes() if review_path.is_file() else None) == before_review


def test_story_settings_use_independent_version_for_noop_and_stale_writes(tmp_path: Path):
    cfg, db, project_id, _ = _fixture(tmp_path)
    first_client = load_project_story_settings(cfg, db, project_id)
    second_client = dict(first_client)
    version = int(first_client["profile_version"])
    before_revision = current_revision(db, project_id)

    unchanged = save_project_story_settings(cfg, db, project_id, first_client, expected_version=version)
    assert unchanged["profile_version"] == version

    def write(client: dict, intent: str):
        try:
            saved = save_project_story_settings(
                cfg,
                db,
                project_id,
                {**client, "project_intent": intent},
                expected_version=version,
            )
            return ("saved", saved)
        except StorySettingsRevisionConflict as exc:
            return ("conflict", exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda args: write(*args),
            ((first_client, "第一個 client 的新意圖"), (second_client, "第二個 stale client 不得覆寫")),
        ))
    assert sorted(result[0] for result in results) == ["conflict", "saved"]
    conflict = next(result[1] for result in results if result[0] == "conflict")
    assert conflict.expected == version
    assert conflict.current == version + 1
    assert load_project_story_settings(cfg, db, project_id)["project_intent"] in {"第一個 client 的新意圖", "第二個 stale client 不得覆寫"}
    assert current_revision(db, project_id) == before_revision


def test_story_public_generation_exposes_raw_normalized_effective_audit(tmp_path: Path):
    cfg, db, project_id, _ = _fixture(tmp_path)
    generation = generate_project_story(cfg, db, project_id)
    public = get_story_generation(db, generation["story_generation_uuid"])
    assert set(public["story_audit"]) == {"raw", "normalized", "effective"}
    assert public["story_audit"]["raw"]["input_hash"] == generation["input_hash"]
    assert public["story_audit"]["normalized"]["chapter_count"] >= 1
    assert public["story_audit"]["effective"]["source"] == "normalized"


def test_cancelled_generation_does_not_replace_last_success(tmp_path: Path):
    cfg, db, project_id, _ = _fixture(tmp_path)
    successful = generate_project_story(cfg, db, project_id)
    with pytest.raises(CancellationRequested):
        generate_project_story(cfg, db, project_id, force=True, should_cancel=lambda: True)
    detail = project_detail(cfg, db, project_id)
    assert detail["story"]["last_successful_story_generation_uuid"] == successful["story_generation_uuid"]
    with connect(db) as con:
        latest = con.execute(
            "select status from story_generations where project_id=? order by generation desc limit 1",
            (project_id,),
        ).fetchone()
    assert latest["status"] == "cancelled"


def test_restart_recovery_marks_inflight_generation_interrupted(tmp_path: Path):
    cfg, db, project_id, _ = _fixture(tmp_path)
    successful = generate_project_story(cfg, db, project_id)
    calls = {"count": 0}

    def cancel_after_provider():
        calls["count"] += 1
        return calls["count"] > 1

    with pytest.raises(CancellationRequested):
        generate_project_story(cfg, db, project_id, force=True, should_cancel=cancel_after_provider)
    with connect(db) as con:
        interrupted = con.execute(
            "select story_generation_uuid from story_generations where project_id=? and story_generation_uuid<>? order by generation desc limit 1",
            (project_id, successful["story_generation_uuid"]),
        ).fetchone()
        con.execute(
            "update story_generations set status='publishing' where story_generation_uuid=?",
            (interrupted["story_generation_uuid"],),
        )
        con.execute(
            "update projects set current_story_generation_uuid=? where id=?",
            (interrupted["story_generation_uuid"], project_id),
        )
    assert recover_interrupted_story_generations(db) == 1
    recovered = get_story_generation(db, interrupted["story_generation_uuid"])
    assert recovered["status"] == "interrupted"
    assert project_detail(cfg, db, project_id)["story"]["current_generation"]["story_generation_uuid"] == successful["story_generation_uuid"]
    assert project_detail(cfg, db, project_id)["story"]["last_successful_story_generation_uuid"] == successful["story_generation_uuid"]


def test_story_detail_polling_does_not_recover_inflight_generation(tmp_path: Path):
    cfg, db, project_id, _ = _fixture(tmp_path)
    successful = generate_project_story(cfg, db, project_id)
    with pytest.raises(CancellationRequested):
        generate_project_story(cfg, db, project_id, force=True, should_cancel=lambda: True)
    with connect(db) as con:
        interrupted = con.execute(
            "select story_generation_uuid from story_generations where project_id=? and story_generation_uuid<>? order by generation desc limit 1",
            (project_id, successful["story_generation_uuid"]),
        ).fetchone()
        con.execute("update story_generations set status='publishing' where story_generation_uuid=?", (interrupted["story_generation_uuid"],))
        con.execute("update projects set current_story_generation_uuid=? where id=?", (interrupted["story_generation_uuid"], project_id))
    detail = project_detail(cfg, db, project_id)
    assert detail["story"]["current_generation"]["status"] == "publishing"
    assert detail["story"]["current_story_generation_uuid"] == interrupted["story_generation_uuid"]
    assert get_story_generation(db, interrupted["story_generation_uuid"])["status"] == "publishing"


def test_story_review_payload_is_validated_against_input_snapshot(tmp_path: Path):
    cfg, db, project_id, _ = _fixture(tmp_path)
    generation = generate_project_story(cfg, db, project_id)
    with pytest.raises(StoryValidationError, match="不存在或跨專案"):
        update_story_generation_review(
            db,
            generation["story_generation_uuid"],
            {"chapters": [{"title": "錯誤", "purpose": "錯誤", "segment_uuids": ["not-owned"], "confidence": 0.5}]},
            project_id=project_id,
        )


def test_story_review_lock_is_persisted_without_changing_project_revision(tmp_path: Path):
    cfg, db, project_id, _ = _fixture(tmp_path)
    generation = generate_project_story(cfg, db, project_id)
    before = current_revision(db, project_id)
    reviewed = update_story_generation_review(
        db,
        generation["story_generation_uuid"],
        {"locked": True},
        project_id=project_id,
    )
    assert reviewed["review_state"]["locked"] is True
    assert current_revision(db, project_id) == before


def test_story_review_rejects_stale_project_revision_without_writing(tmp_path: Path):
    cfg, db, project_id, _ = _fixture(tmp_path)
    generation = generate_project_story(cfg, db, project_id)
    stale = current_revision(db, project_id)
    save_project_story_settings(cfg, db, project_id, {"desired_pacing": "更新後"}, base_revision=stale)
    snapshot = build_story_input_snapshot(cfg, db, project_id)
    save_segment_review(
        cfg,
        db,
        project_id,
        [{"segment_id": snapshot["segments"][0]["segment_uuid"], "user_notes": "其他人工修改"}],
        base_revision=stale,
    )
    before = get_story_generation(db, generation["story_generation_uuid"], include_internal=True)["review_state"]
    with pytest.raises(ProjectRevisionConflict):
        update_story_generation_review(db, generation["story_generation_uuid"], {"locked": True}, project_id=project_id, base_revision=stale)
    after = get_story_generation(db, generation["story_generation_uuid"], include_internal=True)["review_state"]
    assert after == before


def test_locked_story_chapter_survives_regeneration(tmp_path: Path):
    cfg, db, project_id, _ = _fixture(tmp_path)
    first = generate_project_story(cfg, db, project_id)
    locked_chapters = [{**chapter, "locked": True} for chapter in first["normalized_response"]["chapters"]]
    update_story_generation_review(
        db,
        first["story_generation_uuid"],
        {"chapters": locked_chapters},
        project_id=project_id,
    )
    second = generate_project_story(cfg, db, project_id, force=True)
    assert all(chapter["locked"] for chapter in second["normalized_response"]["chapters"])
    assert [chapter["segment_uuids"] for chapter in second["normalized_response"]["chapters"]] == [chapter["segment_uuids"] for chapter in first["normalized_response"]["chapters"]]


def test_apply_preserves_manual_group_and_order(tmp_path: Path):
    cfg, db, project_id, _ = _fixture(tmp_path)
    state = load_storyboard(cfg, project_id)
    segment_id = next(iter(state["segments"]))
    manual_group = state["segments"][segment_id]["group_id"]
    state["segments"][segment_id].update({"manual_group": True, "manual_order": True, "group_id": manual_group, "order": 77})
    from video_vault.storyboard import update_storyboard

    update_storyboard(cfg, db, project_id, state)
    generation = generate_project_story(cfg, db, project_id)
    applied = apply_story_generation_to_storyboard(cfg, db, project_id, generation["story_generation_uuid"])
    assert applied["storyboard"]["segments"][segment_id]["group_id"] == manual_group
    assert applied["storyboard"]["segments"][segment_id]["order"] == 77


def test_chapter_identity_ambiguous_match_gets_review_warning():
    snapshot = {
        "schema_version": 1,
        "input_hash": "unused",
        "project_id": 1,
        "story_profile_id": "general_diary",
        "segments": [{"segment_uuid": "a", "human_override": {"include": True}}, {"segment_uuid": "b", "human_override": {"include": True}}],
        "ordered_segment_uuids": ["a", "b"],
    }
    snapshot["input_hash"] = story_input_hash(snapshot)
    output = {
        "schema_version": 1,
        "story_profile": "general_diary",
        "project_summary": "摘要",
        "chapters": [{"title": "章", "purpose": "整理", "segment_uuids": ["a", "b"], "confidence": 0.5}],
        "overall_confidence": 0.5,
    }
    previous = {"chapters": [{"chapter_id": "old-1", "segment_uuids": ["a", "b"]}, {"chapter_id": "old-2", "segment_uuids": ["a", "b"]}]}
    normalized = normalize_story_output(output, snapshot, previous=previous)
    assert normalized["chapters"][0]["chapter_id"] != "old-1"
    assert any("不明確" in item for item in normalized["chapters"][0]["needs_review_reasons"])


def test_story_apply_is_explicit_and_preserves_locked_segment(tmp_path: Path):
    cfg, db, project_id, _ = _fixture(tmp_path)
    state = load_storyboard(cfg, project_id)
    locked_id = next(iter(state["segments"]))
    state["segments"][locked_id]["locked"] = True
    state["segments"][locked_id]["notes"] = "人工固定"
    from video_vault.storyboard import update_storyboard

    update_storyboard(cfg, db, project_id, state)
    generation = generate_project_story(cfg, db, project_id)
    result = apply_story_generation_to_storyboard(cfg, db, project_id, generation["story_generation_uuid"])
    assert result["storyboard"]["segments"][locked_id]["locked"] is True
    assert result["storyboard"]["segments"][locked_id]["notes"] == "人工固定"


def test_story_review_and_apply_reject_generation_from_another_project(tmp_path: Path):
    cfg, db, project_id, other_id = _fixture(tmp_path, second_project=True)
    generation = generate_project_story(cfg, db, project_id)
    from video_vault.story_generation import update_story_generation_review

    with pytest.raises(StoryGenerationError, match="不屬於指定專案"):
        update_story_generation_review(db, generation["story_generation_uuid"], {}, project_id=other_id)
    with pytest.raises(StoryGenerationError, match="不屬於指定專案"):
        apply_story_generation_to_storyboard(cfg, db, other_id, generation["story_generation_uuid"])


def test_story_output_rejects_unknown_duplicate_and_missing_segment_ids(tmp_path: Path):
    cfg, db, project_id, _ = _fixture(tmp_path)
    snapshot = build_story_input_snapshot(cfg, db, project_id)
    ids = [item["segment_uuid"] for item in snapshot["segments"]]
    output = {
        "schema_version": 1,
        "story_profile": snapshot["story_profile_id"],
        "project_summary": "摘要",
        "overall_confidence": 0.8,
        "chapters": [{"title": "章節", "purpose": "整理", "segment_uuids": [ids[0], "not-owned"], "confidence": 0.8}],
    }
    with pytest.raises(StoryValidationError, match="不存在或跨專案"):
        validate_story_output(output, snapshot)
    output["chapters"][0]["segment_uuids"] = ids[:1]
    with pytest.raises(StoryValidationError, match="遺漏可納入片段"):
        validate_story_output(output, snapshot)


def test_duplicate_suppression_requires_same_project_representative(tmp_path: Path):
    cfg, db, project_id, _ = _fixture(tmp_path)
    snapshot = build_story_input_snapshot(cfg, db, project_id)
    ids = [item["segment_uuid"] for item in snapshot["segments"]]
    snapshot["segments"][0]["duplicate_group"] = "arrival"
    snapshot["segments"][1]["duplicate_group"] = "arrival"
    output = {
        "schema_version": 1,
        "story_profile": snapshot["story_profile_id"],
        "project_summary": "摘要",
        "overall_confidence": 0.8,
        "chapters": [{"title": "章節", "purpose": "整理", "segment_uuids": [ids[0]], "confidence": 0.8}],
        "suppressed_segments": [{"segment_uuid": ids[1], "representative_segment_uuid": ids[0], "reason": "duplicate"}],
    }
    normalized = validate_story_output(output, snapshot)
    assert normalized["suppressed_segments"] == output["suppressed_segments"]
    output["suppressed_segments"][0]["representative_segment_uuid"] = "not-owned"
    with pytest.raises(StoryValidationError, match="不存在或跨專案"):
        validate_story_output(output, snapshot)


def test_apply_rejects_stale_snapshot_and_preserves_storyboard_bytes(tmp_path: Path):
    cfg, db, project_id, _ = _fixture(tmp_path)
    generation = generate_project_story(cfg, db, project_id)
    storyboard_file = project_dir(cfg, project_id) / "storyboard.json"
    before = storyboard_file.read_bytes()
    settings = load_project_story_settings(cfg, db, project_id)
    save_project_story_settings(cfg, db, project_id, {**settings, "project_intent": "已變更的最新意圖"})
    with pytest.raises(StoryGenerationError, match="input_hash 已過期"):
        apply_story_generation_to_storyboard(cfg, db, project_id, generation["story_generation_uuid"])
    assert storyboard_file.read_bytes() == before


def test_apply_failure_restores_storyboard_review_plan_and_project_row(tmp_path: Path, monkeypatch):
    cfg, db, project_id, _ = _fixture(tmp_path)
    generation = generate_project_story(cfg, db, project_id)
    folder = project_dir(cfg, project_id)
    tracked = {name: (folder / name).read_bytes() if (folder / name).is_file() else None for name in ("storyboard.json", "review_status.json", "project_plan.json")}
    with connect(db) as con:
        before_project = dict(con.execute("select * from projects where id=?", (project_id,)).fetchone())

    import video_vault.story_generation as story_generation_module
    from video_vault.storyboard import update_storyboard as real_update_storyboard

    def fail_after_mutation(*args, **kwargs):
        real_update_storyboard(*args, **kwargs)
        raise RuntimeError("simulated apply publish failure")

    monkeypatch.setattr(story_generation_module, "update_storyboard", fail_after_mutation)
    with pytest.raises(RuntimeError, match="simulated apply publish failure"):
        story_generation_module.apply_story_generation_to_storyboard(cfg, db, project_id, generation["story_generation_uuid"])

    for name, content in tracked.items():
        path = folder / name
        assert (path.read_bytes() if path.is_file() else None) == content
    with connect(db) as con:
        after_project = dict(con.execute("select * from projects where id=?", (project_id,)).fetchone())
    assert after_project["status"] == before_project["status"]
    assert after_project["project_revision"] == before_project["project_revision"]


def test_human_review_preserves_app_owned_chapter_identity(tmp_path: Path):
    cfg, db, project_id, _ = _fixture(tmp_path)
    generation = generate_project_story(cfg, db, project_id)
    chapters = deepcopy(generation["normalized_response"]["chapters"])
    chapter_id = chapters[0]["chapter_id"]
    chapters[0]["title"] = "人工重新命名"
    chapters[0]["notes"] = "人工保留環境音"
    reviewed = update_story_generation_review(db, generation["story_generation_uuid"], {"chapters": chapters}, project_id=project_id)
    assert reviewed["review_state"]["chapters"][0]["chapter_id"] == chapter_id
    assert reviewed["review_state"]["chapters"][0]["notes"] == "人工保留環境音"
    chapters[0]["chapter_id"] = "fake-model-id"
    with pytest.raises(StoryValidationError, match="app-owned identity"):
        update_story_generation_review(db, generation["story_generation_uuid"], {"chapters": chapters}, project_id=project_id)


def test_creator_profile_noop_keeps_version_and_stale_write_is_rejected(tmp_path: Path):
    cfg, _, _, _ = _fixture(tmp_path)
    current = load_creator_profile(cfg)
    version = int(current["profile_version"])
    unchanged = save_creator_profile(cfg, current, expected_version=version)
    assert unchanged["profile_version"] == version
    save_creator_profile(cfg, {**current, "wording_style": "新的語氣"}, expected_version=version)
    with pytest.raises(CreatorProfileRevisionConflict):
        save_creator_profile(cfg, {**current, "wording_style": "過期寫入"}, expected_version=version)


def test_creator_profile_concurrent_same_version_has_one_winner(tmp_path: Path):
    cfg, _, _, _ = _fixture(tmp_path)
    current = load_creator_profile(cfg)
    version = int(current["profile_version"])

    def write(style: str):
        try:
            return "ok", save_creator_profile(cfg, {**current, "wording_style": style}, expected_version=version)
        except CreatorProfileRevisionConflict:
            return "conflict", None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ["並行寫入 A", "並行寫入 B"]))
    assert sorted(result[0] for result in results) == ["conflict", "ok"]
    assert load_creator_profile(cfg)["profile_version"] == version + 1


def test_calibration_rejects_untrusted_profile_path(tmp_path: Path):
    with pytest.raises(ValueError, match="未知 Story Profile"):
        calibration_path({"library_root": str(tmp_path)}, "..\\outside")


def test_story_input_uses_story_context_provenance_without_filename_semantics(tmp_path: Path):
    cfg, db, project_id, _ = _fixture(tmp_path)
    snapshot = build_story_input_snapshot(cfg, db, project_id)
    assert snapshot["story_context_provenance"]["filename_semantics"] is False

    def assert_no_semantic_filename(value):
        if isinstance(value, dict):
            assert "filename" not in value
            assert "display_name" not in value
            for child in value.values():
                assert_no_semantic_filename(child)
        elif isinstance(value, list):
            for child in value:
                assert_no_semantic_filename(child)

    assert_no_semantic_filename(snapshot)


def test_local_text_provider_allows_at_most_one_corrective_retry_and_audits(monkeypatch):
    valid = {
        "schema_version": 1,
        "project_summary": "摘要",
        "story_profile": "general_diary",
        "chapters": [],
        "overall_confidence": 0.5,
    }
    responses = [
        {"choices": [{"message": {"content": json.dumps({"unknown": True})}}]},
        {"choices": [{"message": {"content": json.dumps(valid)}}]},
    ]
    requests = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

    def fake_urlopen(request, **_kwargs):
        requests.append(json.loads(request.data.decode("utf-8")))
        return Response(responses.pop(0))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    output, raw = LocalTextStoryProvider("http://127.0.0.1:1234/v1", "test-model").generate_story({"story_profile_id": "general_diary"})
    assert output == valid
    assert raw["provider_audit"]["calls"] == 2
    assert raw["provider_audit"]["retries"] == 1
    assert len(raw["provider_audit"]["call_latencies_ms"]) == 2
    assert raw["provider_audit"]["strict_schema"] is True
    assert all(request["response_format"]["type"] == "json_schema" for request in requests)
    assert all(request["response_format"]["json_schema"]["strict"] is True for request in requests)
    assert all(request["response_format"]["json_schema"]["schema"]["properties"]["story_profile"]["enum"] == ["travel_diary", "coffee_matcha_diary", "roasting_diary", "general_diary"] for request in requests)


def test_calibration_uses_approved_outputs_only_and_can_reset(tmp_path: Path):
    records = [
        {"approved": False, "story_profile": "travel_diary", "shot_durations": [1, 2], "segment_count": 2},
        {"approved": True, "story_profile": "travel_diary", "shot_durations": [2, 4], "segment_count": 2, "title_card_count": 1},
    ]
    calibration = compute_calibration(records, "travel_diary")
    assert calibration["status"] == "ready"
    assert calibration["sample_count"] == 1
    assert calibration["metrics"]["shot_duration_median"] == 3
    assert compute_calibration(records[:1], "travel_diary")["status"] == "insufficient_data"
    from video_vault.story_calibration import save_calibration, calibration_path

    save_calibration({"library_root": str(tmp_path)}, "travel_diary", calibration)
    assert calibration_path({"library_root": str(tmp_path)}, "travel_diary").is_file()
    reset_calibration({"library_root": str(tmp_path)}, "travel_diary")
    assert not calibration_path({"library_root": str(tmp_path)}, "travel_diary").exists()

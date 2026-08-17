from pathlib import Path
from types import SimpleNamespace

import pytest

from video_vault.creative_brief import (
    CREATIVE_BRIEF_STATUS_APPROVED,
    FRAMING_STRATEGY_REGISTRY,
    MISMATCH_DIRECTION_REGISTRY,
    OUTPUT_CONTRACT_REGISTRY,
    FramingStrategyRegistry,
    MismatchDirectionRegistry,
    OutputContractRegistry,
    _recommendation,
    _normalize_approved,
    creative_brief_options,
    ensure_creative_brief,
    load_creative_brief,
    recommend_creative_brief,
    save_approved_creative_brief,
    validate_materialized_approved,
)
from video_vault.database import add_analysis, connect, init_db, upsert_video
from video_vault.project import build_project_plan, create_project
from video_vault.render_manifest import build_render_manifest
from video_vault.project_lifecycle import current_revision
from video_vault.story_input import story_input_hash


def _fixture(tmp_path: Path, monkeypatch):
    db = tmp_path / "db.sqlite3"
    init_db(db)
    sources = []
    video_ids = []
    for index, (width, height) in enumerate(((1080, 1920), (1920, 1080))):
        source = tmp_path / f"source-{index}.mp4"
        source.write_bytes(b"immutable-source")
        sources.append(source)
        video_id = upsert_video(db, {
            "original_path": str(source), "current_path": str(source), "filename": source.name,
            "category": "coffee", "duration_seconds": 10, "width": width, "height": height,
        })
        add_analysis(db, video_id, "mock", "rules", {"segments": [{
            "start_seconds": 0, "end_seconds": 2, "segment_type": "scene", "title": f"segment-{index}",
            "reason": "fixture", "tags": ["fixture"], "score": .9, "suggested_use": "main",
        }]}, tmp_path / f"raw-{index}.json")
        video_ids.append(video_id)
    cfg = {"library_root": str(tmp_path), "ffprobe_path": "ffprobe"}
    project_id = create_project(db, "Creative Brief fixture", video_ids, category="coffee")
    build_project_plan(cfg, db, project_id)

    def probe(_ffprobe, path, mode="fast"):
        row = next(index for index, source in enumerate(sources) if source == path)
        width, height = ((1080, 1920), (1920, 1080))[row]
        return SimpleNamespace(
            display_width=width, display_height=height, display_ratio=width / height,
            display_aspect_ratio=f"{width}:{height}", sample_aspect_ratio="1:1",
            rotation_degrees=0, display_matrix="",
        )

    monkeypatch.setattr("video_vault.creative_brief.probe_media_metadata", probe)
    return cfg, db, project_id


def test_recommendation_keeps_orientation_evidence_and_requires_human_approval(tmp_path, monkeypatch):
    cfg, db, project_id = _fixture(tmp_path, monkeypatch)
    brief = ensure_creative_brief(cfg, db, project_id)
    assert brief["status"] == "needs_confirmation"
    assert brief["recommendation"]["output"]["orientation"] == "landscape"  # deterministic tie default
    assert brief["recommendation"]["source_orientation_summary"] == {"portrait": 1, "landscape": 1, "square": 0, "unknown": 0}
    assert not brief["approved"]

    before_revision = current_revision(db, project_id)
    approved = save_approved_creative_brief(
        cfg, db, project_id,
        {"output": {"orientation": "portrait"}, "framing_intent": {
            "portrait_source_in_landscape": {"approved_strategy": "background_treatment"},
            "landscape_source_in_portrait": {"approved_strategy": "crop_reframe"},
        }},
        approval_source="human_override",
    )
    assert approved["status"] == CREATIVE_BRIEF_STATUS_APPROVED
    assert approved["approved"]["output"]["aspect_ratio"] == "9:16"
    assert approved["approved"]["output"]["width"] == 1080
    assert approved["approved"]["framing_intent"]["portrait_source_in_landscape"]["approved_strategy"] == "background_treatment"
    assert current_revision(db, project_id) == before_revision
    assert load_creative_brief(db, project_id) == approved


def test_recommendation_refresh_does_not_overwrite_human_override(tmp_path, monkeypatch):
    cfg, db, project_id = _fixture(tmp_path, monkeypatch)
    save_approved_creative_brief(cfg, db, project_id, {"output": {"orientation": "portrait"}}, approval_source="human_override")
    refreshed = recommend_creative_brief(cfg, db, project_id)
    assert refreshed["recommendation"]["output"]["orientation"] == "landscape"
    assert refreshed["approved"]["output"]["orientation"] == "portrait"
    assert refreshed["status"] == "approved"


def test_visual_brief_is_not_story_input_hash_semantics(tmp_path):
    base = {"schema_version": 1, "project_id": 1, "segments": [], "creative_brief": {"status": "needs_confirmation"}}
    first = {**base, "input_hash": story_input_hash(base)}
    second = {**base, "creative_brief": {"status": "approved", "visual_contract_hash": "changed"}, "input_hash": story_input_hash({**base, "creative_brief": {"status": "approved", "visual_contract_hash": "changed"}})}
    assert first["input_hash"] == second["input_hash"]


def test_approved_brief_is_render_manifest_source_of_truth(tmp_path, monkeypatch):
    cfg, db, project_id = _fixture(tmp_path, monkeypatch)
    save_approved_creative_brief(cfg, db, project_id, {"output": {"orientation": "portrait"}}, approval_source="human_override")
    manifest = build_render_manifest(cfg, db, project_id)
    assert manifest["profile"]["profile_id"] == "final_1080p_portrait"
    assert manifest["profile"]["width"] == 1080
    assert manifest["profile"]["height"] == 1920
    assert manifest["approved_creative_brief"]["output"]["orientation"] == "portrait"


def _synthetic_registries():
    directions = MismatchDirectionRegistry(MISMATCH_DIRECTION_REGISTRY.entries())
    outputs = OutputContractRegistry(OUTPUT_CONTRACT_REGISTRY.entries())
    outputs.register({
        "output_contract_id": "square_1_1_test",
        "version": "1",
        "orientation": "square",
        "aspect_ratio": "1:1",
        "width": 1080,
        "height": 1080,
        "render_profile_id": "test_square_1080",
        "enabled_for_round1_ui": False,
        "label": "Test square 1:1",
        "capability": {"test_only": True},
    })
    strategies = FramingStrategyRegistry(directions, FRAMING_STRATEGY_REGISTRY.entries())
    strategies.register({
        "strategy_id": "edge_extend_test",
        "version": "1",
        "supported_direction_ids": ["portrait_source_in_landscape", "landscape_source_in_portrait"],
        "label": "Test edge extension",
        "description": "synthetic registry-only strategy",
        "semantic": {"kind": "edge_extend", "test_only": True},
    })
    return outputs, directions, strategies


def test_horizontal_registry_materializes_synthetic_output_and_strategy_without_special_case():
    outputs, directions, strategies = _synthetic_registries()
    recommendation = _recommendation({"orientation_counts": {"portrait": 1, "landscape": 1}, "source_count": 2})
    materialized = _normalize_approved(
        {
            "output": {"output_contract_id": "square_1_1_test", "output_contract_version": "1", "aspect_ratio": "1:1", "width": 1080, "height": 1080},
            "framing_intent": {
                direction["direction_id"]: {"approved_strategy_id": "edge_extend_test", "approved_strategy_version": "1"}
                for direction in directions.entries()
            },
        },
        recommendation,
        output_registry=outputs,
        direction_registry=directions,
        strategy_registry=strategies,
        allow_disabled=True,
    )
    assert materialized["output"]["output_contract_id"] == "square_1_1_test"
    assert materialized["output"]["width"] == 1080
    assert materialized["framing_intent"]["portrait_source_in_landscape"]["approved_strategy_id"] == "edge_extend_test"
    assert validate_materialized_approved(materialized, output_registry=outputs, direction_registry=directions, strategy_registry=strategies)["valid"]


def test_registry_unknown_ids_and_versions_fail_closed():
    outputs, directions, strategies = _synthetic_registries()
    recommendation = _recommendation({"orientation_counts": {}, "source_count": 0})
    with pytest.raises(ValueError, match="unknown output_contract_id"):
        _normalize_approved({"output": {"output_contract_id": "missing"}}, recommendation, output_registry=outputs, direction_registry=directions, strategy_registry=strategies)
    with pytest.raises(ValueError, match="unsupported output contract version"):
        _normalize_approved({"output": {"output_contract_id": "square_1_1_test", "output_contract_version": "99"}}, recommendation, output_registry=outputs, direction_registry=directions, strategy_registry=strategies)
    with pytest.raises(ValueError, match="Creative Brief"):
        _normalize_approved({"output": {"output_contract_id": "square_1_1_test"}, "framing_intent": {"portrait_source_in_landscape": {"approved_strategy_id": "missing"}}}, recommendation, output_registry=outputs, direction_registry=directions, strategy_registry=strategies, allow_disabled=True)


def test_approved_materialization_is_stable_when_new_registry_default_is_added():
    outputs, directions, strategies = _synthetic_registries()
    recommendation = _recommendation({"orientation_counts": {}, "source_count": 0})
    approved = _normalize_approved(
        {"output": {"output_contract_id": "landscape_16_9", "output_contract_version": "1"}, "framing_intent": {}},
        recommendation,
        output_registry=outputs,
        direction_registry=directions,
        strategy_registry=strategies,
    )
    updated_outputs = OutputContractRegistry(outputs.entries())
    updated_outputs.register({
        "output_contract_id": "landscape_16_9_v2",
        "version": "2",
        "orientation": "landscape",
        "aspect_ratio": "2:1",
        "width": 2000,
        "height": 1000,
        "render_profile_id": "test_landscape_v2",
        "enabled_for_round1_ui": False,
    })
    assert validate_materialized_approved(approved, output_registry=updated_outputs, direction_registry=directions, strategy_registry=strategies)["valid"]
    assert approved["output"]["output_contract_id"] == "landscape_16_9"
    assert approved["output"]["aspect_ratio"] == "16:9"


def test_api_options_are_materialized_from_registry():
    options = creative_brief_options()
    assert {item["output_contract_id"] for item in options["output_contracts"]} == {"landscape_16_9", "portrait_9_16"}
    assert {item["strategy_id"] for item in options["framing_strategies"]} >= {"crop_reframe", "background_treatment", "preserve_full_frame", "auto_recommended"}

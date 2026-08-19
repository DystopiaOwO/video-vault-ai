import pytest

from video_vault.editor_disclosure import DisclosureRegistry, disclosure_metadata, default_disclosure_registry


def test_default_disclosure_metadata_is_versioned_and_ordered():
    metadata = disclosure_metadata()
    assert metadata["schema_version"] == "editor-disclosure-v1"
    assert metadata["registry_version"] == "editor-disclosure-registry-v1"
    assert [item["section_id"] for item in metadata["sections"][:2]] == ["output_direction", "visual_style"]
    assert all(item["summary_resolver"] and item["semantic_domain"] and item["invalidation_class"] for item in metadata["sections"])
    assert metadata["sections"][0]["include_in_final_summary"] is True
    assert metadata["sections"][2]["action"]["target"] == "creative_brief.framing"


def test_synthetic_sibling_section_uses_same_registry_without_core_branch():
    registry = default_disclosure_registry()
    registry.register({
        "section_id": "audio_policy_test",
        "version": "1",
        "label": "音訊偏好（測試）",
        "disclosure_level": "advanced",
        "order": 70,
        "summary_resolver": "audio.policy",
        "semantic_domain": "audio_policy",
        "invalidation_class": "audio_render_only",
    })
    item = registry.resolve("audio_policy_test", "1")
    assert item["label"] == "音訊偏好（測試）"
    assert [entry["section_id"] for entry in registry.entries(disclosure_level="advanced")][-1] == "audio_policy_test"


def test_disclosure_registry_rejects_unknown_level_and_duplicate():
    registry = DisclosureRegistry()
    base = {
        "section_id": "example",
        "version": "1",
        "label": "Example",
        "disclosure_level": "advanced",
        "order": 1,
        "summary_resolver": "example.summary",
        "semantic_domain": "example",
        "invalidation_class": "none",
    }
    registry.register(base)
    with pytest.raises(ValueError, match="duplicate disclosure"):
        registry.register(base)
    with pytest.raises(ValueError, match="unsupported disclosure level"):
        registry.register({**base, "section_id": "other", "disclosure_level": "primaryish"})

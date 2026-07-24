from __future__ import annotations

from video_vault.story_context import story_context


def test_user_arrival_context_overrides_food_tags_and_records_avoidance():
    result = story_context(
        "這段是抵達飯店，不要放在早餐",
        "早餐桌上的咖啡與食物",
        "飲食",
    )

    assert result["effective_summary"] == "這段是抵達飯店，不要放在早餐"
    assert result["effective_summary_source"] == "user"
    assert result["activity"] == "抵達／住宿"
    assert result["activity_source"] == "user_summary"
    assert "飲食" in result["avoided_activities"]
    assert result["guidance_applied"] is True


def test_ai_summary_is_fallback_when_user_context_is_empty():
    result = story_context("", "車站月台與列車進站", "交通")

    assert result["effective_summary"] == "車站月台與列車進站"
    assert result["effective_summary_source"] == "ai"
    assert result["activity"] == "交通"
    assert result["activity_source"] == "ai_tags"
    assert result["guidance_applied"] is False


def test_negative_guidance_can_veto_fallback_without_inventing_another_group():
    result = story_context("不要放在早餐", "桌上的食物", "飲食")

    assert result["activity"] == "其他"
    assert result["activity_source"] == "user_summary_avoidance"
    assert result["avoided_activities"] == ["飲食"]


def test_positive_user_context_does_not_rewrite_ai_observation():
    result = story_context("這段是入住旅館", "車站外觀", "交通")

    assert result["user_summary"] == "這段是入住旅館"
    assert result["ai_visual_summary"] == "車站外觀"
    assert result["activity"] == "抵達／住宿"

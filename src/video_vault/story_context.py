from __future__ import annotations

import re

_ACTIVITY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("抵達／住宿", ("抵達", "到達", "入住", "飯店", "酒店", "旅館", "住宿", "check-in", "check in")),
    ("交通", ("車站", "機場", "捷運", "地鐵", "火車", "列車", "公車", "巴士", "搭車", "交通", "移動")),
    ("飲食", ("早餐", "午餐", "晚餐", "餐廳", "咖啡", "抹茶", "食物", "吃飯", "用餐", "甜點", "飲料")),
    ("風景", ("風景", "景色", "海邊", "山景", "夜景", "公園", "自然", "景點")),
    ("逛街", ("逛街", "購物", "商店", "百貨", "市場", "散步", "街道")),
    ("特寫", ("特寫", "細節", "近拍", "產品鏡頭")),
)

_NEGATION_PREFIX = r"(?:不要(?:放|歸|算|分)?(?:在|到|成)?|別(?:放|歸|算|分)?(?:在|到|成)?|不是|非|排除)"


def story_context(user_summary: str, ai_visual_summary: str, fallback_activity: str) -> dict:
    """Resolve project-local user guidance without mutating AI perception.

    User text is treated as high-priority story context. Explicit negative
    guidance can veto an AI/tag-derived activity, while positive context can
    select a more suitable story group. The full provenance is returned so
    plans and UIs can explain why a group was chosen.
    """
    user_text = str(user_summary or "").strip()
    ai_text = str(ai_visual_summary or "").strip()
    effective_summary = user_text or ai_text
    effective_source = "user" if user_text else ("ai" if ai_text else "none")
    avoided = _avoided_activities(user_text)
    positive_text = _without_negated_phrases(user_text)
    preferred = _preferred_activity(positive_text)
    activity = preferred or str(fallback_activity or "其他")
    activity_source = "user_summary" if preferred else "ai_tags"
    if activity in avoided:
        activity = "其他"
        activity_source = "user_summary_avoidance"
    return {
        "user_summary": user_text,
        "ai_visual_summary": ai_text,
        "effective_summary": effective_summary,
        "effective_summary_source": effective_source,
        "activity": activity,
        "activity_source": activity_source,
        "avoided_activities": avoided,
        "guidance_applied": bool(preferred or avoided),
    }


def _avoided_activities(text: str) -> list[str]:
    if not text:
        return []
    avoided: list[str] = []
    for activity, keywords in _ACTIVITY_KEYWORDS:
        for keyword in keywords:
            pattern = rf"{_NEGATION_PREFIX}[^，。；,;\n]{{0,10}}{re.escape(keyword)}"
            if re.search(pattern, text, flags=re.IGNORECASE):
                avoided.append(activity)
                break
    return avoided


def _without_negated_phrases(text: str) -> str:
    if not text:
        return ""
    return re.sub(
        rf"{_NEGATION_PREFIX}[^，。；,;\n]*",
        " ",
        text,
        flags=re.IGNORECASE,
    )


def _preferred_activity(text: str) -> str:
    lowered = text.lower()
    for activity, keywords in _ACTIVITY_KEYWORDS:
        if any(keyword.lower() in lowered for keyword in keywords):
            return activity
    return ""

"""Versioned creator and project story profile contracts."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from typing import Any, Mapping

from .project_lifecycle import project_commit


CREATOR_PROFILE_SCHEMA_VERSION = 1
STORY_PROFILE_SCHEMA_VERSION = 1
STORY_PROFILE_IDS = ("travel_diary", "coffee_matcha_diary", "roasting_diary", "general_diary")
_TITLE_CARD_DENSITIES = {"low", "medium", "high"}
_CREATOR_PROFILE_LOCK = threading.RLock()


class CreatorProfileRevisionConflict(RuntimeError):
    code = "stale_creator_profile"

    def __init__(self, expected: int | None, current: int):
        self.expected = expected
        self.current = int(current)
        super().__init__(f"Creator Profile 已更新：目前 version {current}，請重新載入後再儲存")


class StorySettingsRevisionConflict(RuntimeError):
    code = "stale_story_settings"

    def __init__(self, expected: int | None, current: int):
        self.expected = expected
        self.current = int(current)
        expected_text = "缺少" if expected is None else str(expected)
        super().__init__(f"Project Story Settings 已更新：目前 version {current}，請重新載入後再儲存（收到 {expected_text}）")


STORY_PROFILES: dict[str, dict[str, Any]] = {
    "travel_diary": {
        "profile_id": "travel_diary",
        "profile_version": 1,
        "label": "旅行日記",
        "roles": ["time_of_day", "activity", "walking", "transport", "food", "attraction", "atmosphere", "transition"],
        "rules": ["保留移動與氛圍鏡頭", "不套用 Shorts 快速剪輯門檻"],
    },
    "coffee_matcha_diary": {
        "profile_id": "coffee_matcha_diary",
        "profile_version": 1,
        "label": "咖啡／抹茶日記",
        "roles": ["setup", "process", "waiting", "brewing", "whisking", "pouring", "result", "atmosphere"],
        "rules": ["允許等待與慢節奏", "不得自動變成教學或業配"],
    },
    "roasting_diary": {
        "profile_id": "roasting_diary",
        "profile_version": 1,
        "label": "烘豆日記",
        "roles": ["preparation", "roast_process", "visible_change", "result", "record"],
        "rules": ["只描述畫面可證實的變化", "不得虛構烘焙參數或專業判斷"],
    },
    "general_diary": {
        "profile_id": "general_diary",
        "profile_version": 1,
        "label": "一般日記",
        "roles": ["scene", "action", "atmosphere", "transition", "result"],
        "rules": ["保守 fallback", "不套用 Shorts 或教學片固定門檻"],
    },
}


def default_creator_profile() -> dict[str, Any]:
    return {
        "schema_version": CREATOR_PROFILE_SCHEMA_VERSION,
        "profile_version": 1,
        "language": "zh-TW",
        "wording_style": "自然、簡潔、像日記而不是教學稿",
        "visual_style": "乾淨、生活感、保留環境氛圍",
        "title_card_density": "low",
        "transition_preference": "以 cut 為主，地點切換才加字卡",
        "natural_audio_policy": "保留可辨識的環境音與關鍵動作聲",
        "tutorial_tone_allowed": False,
        "sponsored_tone_allowed": False,
        "disliked_styles": [],
        "calibration": {},
    }


def creator_profile_path(cfg: Mapping[str, Any]) -> Path:
    return Path(str(cfg["library_root"])) / "08_projects" / "creator_profile.json"


def project_story_settings_path(cfg: Mapping[str, Any], project_id: int) -> Path:
    return Path(str(cfg["library_root"])) / "08_projects" / f"project_{int(project_id)}" / "story_profile.json"


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)
    return path


def normalize_creator_profile(profile: Mapping[str, Any] | None) -> dict[str, Any]:
    source = {**default_creator_profile(), **dict(profile or {})}
    source["schema_version"] = CREATOR_PROFILE_SCHEMA_VERSION
    source["profile_version"] = max(1, int(source.get("profile_version") or 1))
    source["language"] = str(source.get("language") or "zh-TW")
    source["wording_style"] = str(source.get("wording_style") or "").strip()
    source["visual_style"] = str(source.get("visual_style") or "").strip()
    density = str(source.get("title_card_density") or "low")
    if density not in _TITLE_CARD_DENSITIES:
        raise ValueError("title_card_density 必須是 low、medium 或 high")
    source["title_card_density"] = density
    source["transition_preference"] = str(source.get("transition_preference") or "").strip()
    source["natural_audio_policy"] = str(source.get("natural_audio_policy") or "").strip()
    source["tutorial_tone_allowed"] = bool(source.get("tutorial_tone_allowed", False))
    source["sponsored_tone_allowed"] = bool(source.get("sponsored_tone_allowed", False))
    disliked = source.get("disliked_styles") or []
    if not isinstance(disliked, list) or not all(isinstance(item, str) for item in disliked):
        raise ValueError("disliked_styles 必須是文字列表")
    source["disliked_styles"] = [item.strip() for item in disliked if item.strip()]
    source["calibration"] = dict(source.get("calibration") or {})
    return source


def load_creator_profile(cfg: Mapping[str, Any]) -> dict[str, Any]:
    path = creator_profile_path(cfg)
    if not path.is_file():
        return default_creator_profile()
    try:
        return normalize_creator_profile(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return default_creator_profile()


def save_creator_profile(
    cfg: Mapping[str, Any],
    profile: Mapping[str, Any],
    *,
    expected_version: int | None = None,
) -> dict[str, Any]:
    with _CREATOR_PROFILE_LOCK:
        return _save_creator_profile_unlocked(cfg, profile, expected_version=expected_version)


def _save_creator_profile_unlocked(
    cfg: Mapping[str, Any],
    profile: Mapping[str, Any],
    *,
    expected_version: int | None = None,
) -> dict[str, Any]:
    current = load_creator_profile(cfg)
    incoming = normalize_creator_profile(profile)
    current_version = int(current.get("profile_version") or 1)
    if expected_version is not None and int(expected_version) != current_version:
        raise CreatorProfileRevisionConflict(int(expected_version), current_version)
    comparable_current = {key: value for key, value in current.items() if key not in {"profile_version", "updated_at"}}
    comparable_incoming = {key: value for key, value in incoming.items() if key not in {"profile_version", "updated_at"}}
    if comparable_current == comparable_incoming:
        if not creator_profile_path(cfg).is_file():
            _atomic_json(creator_profile_path(cfg), current)
        return current
    incoming["profile_version"] = current_version + 1
    incoming["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _atomic_json(creator_profile_path(cfg), incoming)
    return incoming


def story_profile_definition(profile_id: str) -> dict[str, Any]:
    key = str(profile_id or "").strip()
    if key not in STORY_PROFILES:
        raise ValueError(f"未知 Story Profile：{key}")
    return deepcopy(STORY_PROFILES[key])


def default_project_story_settings(content_type: str = "diary_montage") -> dict[str, Any]:
    content = str(content_type or "").lower()
    profile_id = {
        "travel_diary": "travel_diary",
        "coffee_process": "coffee_matcha_diary",
        "matcha_process": "coffee_matcha_diary",
        "roasting_diary": "roasting_diary",
    }.get(content, "general_diary")
    definition = story_profile_definition(profile_id)
    return {
        "schema_version": STORY_PROFILE_SCHEMA_VERSION,
        "profile_id": profile_id,
        "profile_version": int(definition["profile_version"]),
        "project_intent": "",
        "itinerary": "",
        "desired_sequence": [],
        "desired_pacing": "",
        "title_card_preference_override": "",
        "natural_audio_override": "",
        "must_keep": [],
        "exclude_guidance": [],
        "creator_profile_override": {},
    }


def _normalize_project_story_settings(settings: Mapping[str, Any], content_type: str = "diary_montage") -> dict[str, Any]:
    normalized = {**default_project_story_settings(content_type), **dict(settings)}
    profile_id = str(normalized.get("profile_id") or "general_diary")
    story_profile_definition(profile_id)
    normalized["schema_version"] = STORY_PROFILE_SCHEMA_VERSION
    normalized["profile_id"] = profile_id
    normalized["profile_version"] = max(1, int(normalized.get("profile_version") or 1))
    for key in ("project_intent", "itinerary", "desired_pacing", "title_card_preference_override", "natural_audio_override"):
        normalized[key] = str(normalized.get(key) or "")
    for key in ("desired_sequence", "must_keep", "exclude_guidance"):
        value = normalized.get(key) or []
        normalized[key] = [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []
    normalized["creator_profile_override"] = dict(normalized.get("creator_profile_override") or {})
    return normalized


def load_project_story_settings(cfg: Mapping[str, Any], db: Path, project_id: int) -> dict[str, Any]:
    from .database import project

    row = project(db, int(project_id))
    default = default_project_story_settings(str(row["content_type"]) if row else "diary_montage")
    path = project_story_settings_path(cfg, project_id)
    if path.is_file():
        try:
            default.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
    return _normalize_project_story_settings(default, str(row["content_type"]) if row else "diary_montage")


def save_project_story_settings(
    cfg: Mapping[str, Any],
    db: Path,
    project_id: int,
    patch: Mapping[str, Any],
    *,
    base_revision: int | None = None,
    expected_version: int | None = None,
) -> dict[str, Any]:
    with project_commit(db, int(project_id), base_revision) as commit:
        current = load_project_story_settings(cfg, db, project_id)
        current_version = int(current.get("profile_version") or 1)
        if expected_version is not None and int(expected_version) != current_version:
            raise StorySettingsRevisionConflict(int(expected_version), current_version)
        row_content_type = "diary_montage"
        from .database import project

        row = project(db, int(project_id))
        if row:
            row_content_type = str(row["content_type"] or "diary_montage")
        updated = _normalize_project_story_settings({**current, **dict(patch)}, row_content_type)
        comparable_current = {key: value for key, value in current.items() if key != "profile_version"}
        comparable_updated = {key: value for key, value in updated.items() if key != "profile_version"}
        changed = comparable_updated != comparable_current
        updated["profile_version"] = current_version + 1 if changed else current_version
        if changed or not project_story_settings_path(cfg, project_id).is_file():
            _atomic_json(project_story_settings_path(cfg, project_id), updated)
            # Story settings affect the next StoryInputSnapshot, but do not
            # change the approved render state. Only Apply/storyboard writes
            # should advance project_revision and invalidate approval.
            commit.record_changed(False)
        else:
            commit.record_changed(False)
    return load_project_story_settings(cfg, db, project_id)


def resolved_creator_profile(cfg: Mapping[str, Any], project_settings: Mapping[str, Any]) -> dict[str, Any]:
    base = load_creator_profile(cfg)
    override = dict(project_settings.get("creator_profile_override") or {})
    return normalize_creator_profile({**base, **override, "profile_version": base.get("profile_version", 1)})


__all__ = [
    "CREATOR_PROFILE_SCHEMA_VERSION",
    "CreatorProfileRevisionConflict",
    "StorySettingsRevisionConflict",
    "STORY_PROFILE_SCHEMA_VERSION",
    "STORY_PROFILE_IDS",
    "STORY_PROFILES",
    "creator_profile_path",
    "default_creator_profile",
    "default_project_story_settings",
    "load_creator_profile",
    "load_project_story_settings",
    "normalize_creator_profile",
    "project_story_settings_path",
    "resolved_creator_profile",
    "save_creator_profile",
    "save_project_story_settings",
    "story_profile_definition",
]

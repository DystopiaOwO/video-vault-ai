"""Versioned metadata for the simple-first creative editor disclosure layers.

This registry describes presentation and invalidation boundaries only.  It is
deliberately separate from Creative Brief and Visual Style semantic state so
opening an accordion can never become a second source of truth.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any, Mapping


EDITOR_DISCLOSURE_SCHEMA_VERSION = "editor-disclosure-v1"
EDITOR_DISCLOSURE_REGISTRY_VERSION = "editor-disclosure-registry-v1"
DISCLOSURE_LEVELS = {"primary", "advanced", "diagnostic"}


class DisclosureRegistry:
    """Small deterministic registry for editor sections."""

    def __init__(self, entries: list[Mapping[str, Any]] | None = None, *, version: str = EDITOR_DISCLOSURE_REGISTRY_VERSION):
        self.version = str(version)
        self._entries: dict[str, dict[str, Any]] = {}
        for entry in entries or []:
            self.register(entry)

    def register(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        normalized = {
            "section_id": str(entry.get("section_id") or "").strip(),
            "version": str(entry.get("version") or "").strip(),
            "label": str(entry.get("label") or "").strip(),
            "disclosure_level": str(entry.get("disclosure_level") or "").strip().lower(),
            "order": int(entry.get("order") or 0),
            "enabled": bool(entry.get("enabled", True)),
            "capability": deepcopy(entry.get("capability") if isinstance(entry.get("capability"), Mapping) else {}),
            "summary_resolver": str(entry.get("summary_resolver") or "").strip(),
            "semantic_domain": str(entry.get("semantic_domain") or "").strip(),
            "invalidation_class": str(entry.get("invalidation_class") or "").strip(),
            "action": deepcopy(entry.get("action") if isinstance(entry.get("action"), Mapping) else {}),
            "include_in_final_summary": bool(entry.get("include_in_final_summary", False)),
            "summary_order": int(entry.get("summary_order") or entry.get("order") or 0),
        }
        if not normalized["section_id"] or not normalized["version"] or not normalized["label"]:
            raise ValueError("disclosure section requires stable id, version, and label")
        if normalized["disclosure_level"] not in DISCLOSURE_LEVELS:
            raise ValueError(f"unsupported disclosure level: {normalized['disclosure_level'] or 'empty'}")
        if not normalized["summary_resolver"] or not normalized["semantic_domain"] or not normalized["invalidation_class"]:
            raise ValueError(f"disclosure section missing semantic metadata: {normalized['section_id']}")
        if normalized["section_id"] in self._entries:
            raise ValueError(f"duplicate disclosure section: {normalized['section_id']}")
        self._entries[normalized["section_id"]] = normalized
        return deepcopy(normalized)

    def resolve(self, section_id: str, version: str | None = None) -> dict[str, Any]:
        key = str(section_id or "").strip()
        entry = self._entries.get(key)
        if entry is None:
            raise ValueError(f"unknown disclosure section: {key or 'empty'}")
        if version not in (None, "") and str(version) != entry["version"]:
            raise ValueError(f"unsupported disclosure section version: {key}@{version}")
        return deepcopy(entry)

    def entries(self, *, disclosure_level: str | None = None, enabled_only: bool = False) -> list[dict[str, Any]]:
        entries = list(self._entries.values())
        if disclosure_level is not None:
            level = str(disclosure_level).strip().lower()
            entries = [entry for entry in entries if entry["disclosure_level"] == level]
        if enabled_only:
            entries = [entry for entry in entries if entry["enabled"]]
        return deepcopy(sorted(entries, key=lambda item: (int(item["order"]), item["section_id"])))

    def hash(self) -> str:
        payload = {"version": self.version, "entries": self.entries()}
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def default_disclosure_registry() -> DisclosureRegistry:
    return DisclosureRegistry([
        {
            "section_id": "output_direction",
            "version": "1",
            "label": "影片方向",
            "disclosure_level": "primary",
            "order": 10,
            "summary_resolver": "creative_brief.output@1",
            "semantic_domain": "creative_brief.visual",
            "invalidation_class": "visual_render_only",
            "include_in_final_summary": True,
            "summary_order": 10,
        },
        {
            "section_id": "visual_style",
            "version": "1",
            "label": "視覺風格",
            "disclosure_level": "primary",
            "order": 20,
            "summary_resolver": "visual_style.approved@1",
            "semantic_domain": "visual_style",
            "invalidation_class": "visual_preview_and_render",
            "include_in_final_summary": True,
            "summary_order": 20,
        },
        {
            "section_id": "framing",
            "version": "1",
            "label": "畫面配置",
            "disclosure_level": "advanced",
            "order": 30,
            "summary_resolver": "creative_brief.framing@1",
            "semantic_domain": "creative_brief.visual",
            "invalidation_class": "visual_render_only",
            "action": {"type": "open_semantic_editor@1", "target": "creative_brief.framing"},
            "include_in_final_summary": True,
            "summary_order": 30,
        },
        {
            "section_id": "grading",
            "version": "1",
            "label": "視覺與調色",
            "disclosure_level": "advanced",
            "order": 40,
            "summary_resolver": "visual_style.grading@1",
            "semantic_domain": "visual_style",
            "invalidation_class": "visual_preview_and_render",
            "action": {"type": "open_semantic_editor@1", "target": "visual_style.grading"},
            "summary_order": 40,
        },
        {
            "section_id": "title",
            "version": "1",
            "label": "字卡",
            "disclosure_level": "advanced",
            "order": 50,
            "summary_resolver": "visual_style.title_style@1",
            "semantic_domain": "visual_style.title",
            "invalidation_class": "visual_preview_and_render",
            "action": {"type": "open_semantic_editor@1", "target": "visual_style.title_style"},
            "include_in_final_summary": True,
            "summary_order": 50,
        },
        {
            "section_id": "captions",
            "version": "1",
            "label": "字幕",
            "disclosure_level": "advanced",
            "order": 60,
            "summary_resolver": "creative_brief.captions@1",
            "semantic_domain": "caption_policy",
            "invalidation_class": "caption_renderer",
            "capability": {"status": "summary_only", "owner": "future_subtitle_task"},
            "action": {"type": "open_semantic_editor@1", "target": "caption.summary"},
            "summary_order": 60,
        },
        {
            "section_id": "technical",
            "version": "1",
            "label": "技術資訊",
            "disclosure_level": "diagnostic",
            "order": 90,
            "summary_resolver": "diagnostic.contracts@1",
            "semantic_domain": "diagnostic",
            "invalidation_class": "none",
            "summary_order": 90,
        },
    ])


def disclosure_metadata(*, registry: DisclosureRegistry | None = None) -> dict[str, Any]:
    active = registry or default_disclosure_registry()
    return {
        "schema_version": EDITOR_DISCLOSURE_SCHEMA_VERSION,
        "registry_version": active.version,
        "registry_hash": active.hash(),
        "sections": active.entries(),
    }


__all__ = [
    "DISCLOSURE_LEVELS",
    "EDITOR_DISCLOSURE_REGISTRY_VERSION",
    "EDITOR_DISCLOSURE_SCHEMA_VERSION",
    "DisclosureRegistry",
    "default_disclosure_registry",
    "disclosure_metadata",
]

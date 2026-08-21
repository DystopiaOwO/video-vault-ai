"""Versioned visual-style and title contracts for preview/render parity.

This module deliberately keeps the contract semantic.  FFmpeg filters are
resolved from those values at the final boundary, so an approved project does
not depend on mutable registry objects or ad-hoc UI values.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import functools
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import subprocess
import os
import tempfile
import unicodedata
from typing import Any, Mapping

from .creative_brief import approved_creative_brief
from .color_pipeline import build_color_filter, color_mode_contract, validate_lut_resource
from .database import connect, init_db
from .paths import root
from .project import project_dir
from .project_lifecycle import project_commit
from .project_media import revalidate_and_rebind_project_source_fingerprint
from .source_fingerprint import SourceFingerprintChangedError, parse_source_fingerprint
from .media_probe import probe_media_metadata


VISUAL_STYLE_SCHEMA_VERSION = "visual-style-v1"
VISUAL_STYLE_REGISTRY_VERSION = "visual-style-registry-v1"
TITLE_STYLE_SCHEMA_VERSION = "title-style-v1"
TITLE_STYLE_REGISTRY_VERSION = "title-style-registry-v1"
VISUAL_STYLE_STATE_SCHEMA_VERSION = 1
VISUAL_RENDER_CONTRACT_VERSION = "visual-render-v1"
VISUAL_PREVIEW_SCOPE_VERSION = "visual-preview-scope-v1"
VISUAL_PREVIEW_RENDER_CONTRACT_VERSION = "visual-preview-render-v3"
TITLE_PIXEL_EVIDENCE_VERSION = "title-pixel-evidence-v1"
PREVIEW_SCOPES = ("primary", "extended")
PREVIEW_SEEK_STRATEGY = "bounded-pre-roll-v1"
PREVIEW_SEEK_PREROLL_SECONDS = 2.0
PREVIEW_TITLE_SAMPLE_SECONDS = 0.35
TITLE_ANCHORS = ("top-left", "top-center", "top-right", "center", "bottom-left", "bottom-center", "bottom-right")
TITLE_MOTION_PRESETS = ("none", "fade", "fade_rise", "slide_fade")
TITLE_SIZE_PRESETS = {"small": 0.85, "normal": 1.0, "large": 1.2}
TITLE_WEIGHT_VALUES = (400, 500, 600, 700)
TITLE_FONT_FAMILIES = ("system-sans", "Noto Sans CJK TC", "Segoe UI", "Arial")
TITLE_ROLE_LABELS = {
    "chapter_title": "章節標題", "section_title": "段落標題", "location_title": "地點標題",
    "date_time_title": "日期時間", "lower_third": "下三分之一字幕", "caption_subtitle": "說明字幕",
}
TITLE_ANCHOR_LABELS = {"top-left": "左上", "top-center": "上中", "top-right": "右上", "center": "中央", "bottom-left": "左下", "bottom-center": "下中", "bottom-right": "右下"}
TITLE_MOTION_LABELS = {"none": "無", "fade": "淡入淡出", "fade_rise": "淡入上移", "slide_fade": "滑入淡出"}
TITLE_SIZE_LABELS = {"small": "小", "normal": "標準", "large": "大"}
TITLE_PALETTE_LABELS = {"default": "預設", "muted": "低彩度", "high_contrast": "高對比"}
TITLE_READABILITY_LABELS = {"none": "無表面", "translucent": "半透明", "solid": "實色"}
TITLE_FONT_LABELS = {"system-sans": "System Sans", "Noto Sans CJK TC": "Noto Sans CJK TC", "Segoe UI": "Segoe UI", "Arial": "Arial"}
_APPROVAL_FIELDS = {
    "approved_preview_variant_id",
    "approved_preview_plan_hash",
    "approved_preview_evidence_identity",
    "approval_envelope",
}


class VisualStyleError(ValueError):
    """Raised when a style cannot be safely resolved or rendered."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RegistryEntry:
    item_id: str
    version: str
    payload: dict[str, Any]


class VisualStyleRegistry:
    """Small registry; additions do not require resolver branches."""

    def __init__(self, entries: Mapping[str, Mapping[str, Any]] | None = None):
        source = entries or _DEFAULT_VISUAL_STYLES
        if isinstance(source, Mapping):
            values = source.items()
        else:
            values = ((str(value.get("style_id") or ""), value) for value in source)
        self._entries = {}
        for key, value in values:
            item = deepcopy(dict(value))
            item["style_id"] = str(item.get("style_id") or key)
            self._entries[item["style_id"]] = item

    def register(self, style_id: str, payload: Mapping[str, Any]) -> None:
        style_id = str(style_id).strip()
        if not style_id or str(payload.get("version") or "") == "":
            raise VisualStyleError("style_contract_invalid", "visual style 必須有 stable id/version")
        item = deepcopy(dict(payload))
        item["style_id"] = style_id
        _validate_style_definition(item)
        self._entries[style_id] = item

    def resolve(self, style_id: str, version: str | int | None = None) -> dict[str, Any]:
        item = self._entries.get(str(style_id))
        if item is None:
            raise VisualStyleError("style_unknown", f"unknown visual style: {style_id}")
        expected = str(version or item["version"])
        if str(item["version"]) != expected:
            raise VisualStyleError("style_version_unsupported", f"unsupported visual style version: {style_id}@{expected}")
        _validate_style_definition(item)
        return deepcopy(item)

    def list(self, *, include_internal: bool = True) -> list[dict[str, Any]]:
        values = self._entries.values() if include_internal else (item for item in self._entries.values() if item.get("enabled_for_round1_ui", True))
        return [deepcopy(item) for item in values]

    def hash(self) -> str:
        return _hash(self._entries)


class TitleStyleRegistry:
    def __init__(self, entries: Mapping[str, Mapping[str, Any]] | None = None):
        source = entries or _DEFAULT_TITLE_STYLES
        if isinstance(source, Mapping):
            values = source.items()
        else:
            values = ((str(value.get("title_style_id") or ""), value) for value in source)
        self._entries = {}
        for key, value in values:
            item = deepcopy(dict(value))
            item["title_style_id"] = str(item.get("title_style_id") or key)
            self._entries[item["title_style_id"]] = item

    def register(self, style_id: str, payload: Mapping[str, Any]) -> None:
        style_id = str(style_id).strip()
        item = deepcopy(dict(payload))
        item["title_style_id"] = style_id
        if item.get("extends"):
            _validate_title_inheritance_definition(item)
            previous = self._entries.get(style_id)
            self._entries[style_id] = item
            try:
                resolved, _ = self._resolve_inheritance(style_id, item.get("version"), [])
                if resolved is None:
                    raise VisualStyleError("title_style_parent_unknown", f"unknown title style parent: {style_id}")
                _validate_title_definition(resolved)
            except Exception:
                if previous is None:
                    self._entries.pop(style_id, None)
                else:
                    self._entries[style_id] = previous
                raise
            return
        _validate_title_definition(item)
        self._entries[style_id] = item

    def resolve(self, style_id: str, version: str | int | None = None, *, role: str = "chapter_title", aspect: str = "landscape") -> dict[str, Any]:
        resolved, chain = self._resolve_inheritance(str(style_id), version, [])
        if resolved is None:
            raise VisualStyleError("title_style_unknown", f"unknown title style: {style_id}")
        item = resolved
        if role not in item["supported_roles"]:
            raise VisualStyleError("title_role_unsupported", f"title style {style_id} does not support role {role}")
        _validate_title_definition(item)
        resolved = _resolve_title_role_contract(item, role, aspect)
        resolved["resolved_parent_chain"] = chain
        return resolved

    def _resolve_inheritance(self, style_id: str, version: str | int | None, stack: list[str]) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
        if style_id in stack:
            raise VisualStyleError("title_style_inheritance_cycle", "title style inheritance cycle: " + " -> ".join([*stack, style_id]))
        item = self._entries.get(style_id)
        if item is None:
            raise VisualStyleError("title_style_parent_unknown", f"unknown title style parent: {style_id}")
        expected = str(version or item.get("version") or "")
        if str(item.get("version") or "") != expected:
            raise VisualStyleError("title_style_version_unsupported", f"unsupported title style version: {style_id}@{expected}")
        parent_spec = item.get("extends")
        if not parent_spec:
            return deepcopy(item), [{"title_style_id": style_id, "version": expected}]
        if not isinstance(parent_spec, Mapping):
            raise VisualStyleError("title_style_inheritance_invalid", f"invalid parent for title style: {style_id}")
        parent_id = str(parent_spec.get("title_style_id") or parent_spec.get("id") or "")
        parent_version = str(parent_spec.get("version") or "")
        if not parent_id or not parent_version:
            raise VisualStyleError("title_style_inheritance_invalid", f"parent id/version missing for title style: {style_id}")
        parent, chain = self._resolve_inheritance(parent_id, parent_version, [*stack, style_id])
        if parent is None:
            raise VisualStyleError("title_style_parent_unknown", f"unknown title style parent: {parent_id}")
        child_overrides = {key: value for key, value in item.items() if key != "extends" and key != "overrides"}
        if isinstance(item.get("overrides"), Mapping):
            child_overrides = _deep_merge(child_overrides, item["overrides"])
        merged = _deep_merge(parent, child_overrides)
        explicit = {key: value for key, value in child_overrides.items() if key not in {"title_style_id", "version", "label"}}
        inherited_explicit = parent.get("_inherited_explicit_overrides") if isinstance(parent.get("_inherited_explicit_overrides"), Mapping) else {}
        merged["_inherited_explicit_overrides"] = _deep_merge(inherited_explicit, explicit)
        return merged, [*chain, {"title_style_id": style_id, "version": expected}]

    def list(self) -> list[dict[str, Any]]:
        return [deepcopy(item) for item in self._entries.values()]

    def resolved_metadata(self) -> list[dict[str, Any]]:
        """Return capability metadata from fully resolved title contracts."""
        result = []
        for style_id, item in self._entries.items():
            resolved, chain = self._resolve_inheritance(style_id, item.get("version"), [])
            if resolved is None:
                raise VisualStyleError("title_style_parent_unknown", f"unknown title style: {style_id}")
            _validate_title_definition(resolved)
            result.append({
                "title_style_id": str(style_id),
                "version": str(resolved.get("version") or ""),
                "supported_roles": [str(role) for role in resolved.get("supported_roles") or ()],
                "resolved_parent_chain": chain,
            })
        return result

    def hash(self) -> str:
        return _hash(self._entries)


def materialize_visual_style(
    style_id: str,
    approved_brief: Mapping[str, Any],
    *,
    style_version: str | int | None = None,
    title_style_id: str | None = None,
    title_style_version: str | int | None = None,
    title_role: str = "chapter_title",
    capabilities: Mapping[str, bool] | None = None,
    color_settings: Mapping[str, Any] | None = None,
    registry: VisualStyleRegistry | None = None,
    title_registry: TitleStyleRegistry | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve a complete immutable semantic snapshot from an approved brief."""

    if "approved" in approved_brief and "output" not in approved_brief:
        approved_brief = _approved_contract(approved_brief)
    if str(approved_brief.get("status") or "approved") != "approved":
        raise VisualStyleError("creative_brief_required", "visual preview/render 需要 approved Creative Brief")
    output = dict(approved_brief.get("output") or {})
    contract_id = str(output.get("output_contract_id") or "")
    aspect = str(output.get("orientation") or "landscape")
    if not contract_id or not output.get("width") or not output.get("height"):
        raise VisualStyleError("creative_brief_invalid", "approved Creative Brief 缺少 output contract")
    style_registry = registry or VISUAL_STYLES
    resolved_title_registry = title_registry or TITLE_STYLES
    style = style_registry.resolve(style_id, style_version)
    normalized_overrides = _normalize_visual_overrides(overrides)
    title_id = str(normalized_overrides.get("title_style_id") or title_style_id or style["default_title_style_id"])
    title_version = normalized_overrides.get("title_style_version") or title_style_version
    title = resolved_title_registry.resolve(title_id, title_version, role=title_role, aspect=aspect)
    if normalized_overrides.get("title_role"):
        title_role = str(normalized_overrides["title_role"])
        title = resolved_title_registry.resolve(title_id, title_version, role=title_role, aspect=aspect)
    title = _apply_title_overrides(title, normalized_overrides, aspect)
    composition = str(normalized_overrides.get("composition") or style["composition"])
    if composition not in {"overlay", "standalone"}:
        raise VisualStyleError("visual_override_invalid", "composition must be overlay or standalone")
    palette = _apply_palette_override(style["palette"], normalized_overrides)
    required_caps = style.get("required_capabilities") or {}
    for capability, required in required_caps.items():
        if required and capabilities is not None and not capabilities.get(capability, False):
            raise VisualStyleError("style_capability_unsupported", f"visual style {style_id} needs unsupported capability {capability}")
    framing = _approved_framing(approved_brief)
    snapshot = {
        "schema_version": VISUAL_STYLE_SCHEMA_VERSION,
        "registry_version": VISUAL_STYLE_REGISTRY_VERSION,
        "registry_hash": style_registry.hash(),
        "visual_style_id": style["style_id"],
        "visual_style_version": str(style["version"]),
        "label": style["label"],
        "composition": composition,
        "palette": palette,
        "grading": deepcopy(style["grading"]),
        "creative_look": deepcopy(style["grading"]),
        "technical_transform": _technical_transform_snapshot(color_settings),
        "framing": framing,
        "title_style": {
            "schema_version": TITLE_STYLE_SCHEMA_VERSION,
            "registry_version": TITLE_STYLE_REGISTRY_VERSION,
            "registry_hash": resolved_title_registry.hash(),
            **title,
        },
        "output": {
            "output_contract_id": contract_id,
            "output_contract_version": str(output.get("output_contract_version") or ""),
            "orientation": aspect,
            "aspect_ratio": str(output.get("aspect_ratio") or ""),
            "width": int(output["width"]),
            "height": int(output["height"]),
            "render_profile_id": str(output.get("render_profile_id") or ""),
        },
        "creative_brief_revision": int(approved_brief.get("brief_version") or 1),
        "creative_brief_hash": str(approved_brief.get("visual_contract_hash") or _hash(approved_brief)),
        "overrides": normalized_overrides,
    }
    snapshot["semantic_hash"] = _semantic_hash(snapshot)
    snapshot["resolved_hash"] = snapshot["semantic_hash"]
    return snapshot


def validate_materialized_visual_style(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    for field in ("schema_version", "visual_style_id", "visual_style_version", "registry_hash", "resolved_hash", "output", "framing", "grading", "title_style"):
        if not snapshot.get(field):
            errors.append(f"missing {field}")
    try:
        VISUAL_STYLES.resolve(str(snapshot.get("visual_style_id")), snapshot.get("visual_style_version"))
        title = snapshot.get("title_style") or {}
        TITLE_STYLES.resolve(str(title.get("title_style_id")), title.get("version"), role=str(title.get("role") or "chapter_title"), aspect=str((snapshot.get("output") or {}).get("orientation") or "landscape"))
    except VisualStyleError as exc:
        errors.append(str(exc))
    expected_hash = _semantic_hash(snapshot)
    if snapshot.get("resolved_hash") != expected_hash or snapshot.get("semantic_hash", expected_hash) != expected_hash:
        errors.append("semantic hash mismatch")
    return {"ok": not errors, "errors": errors}


def visual_style_control_defaults(
    approved_brief: Mapping[str, Any],
    *,
    style_id: str | None = None,
    title_style_id: str | None = None,
    title_role: str | None = None,
    aspect: str | None = None,
    registry: VisualStyleRegistry | None = None,
    title_registry: TitleStyleRegistry | None = None,
) -> list[dict[str, Any]]:
    """Expose control defaults from the same registries used by rendering.

    The client receives resolved values only.  It must not reconstruct title
    inheritance, role tokens, aspect responsive values, or palette semantics.
    """

    brief = _approved_contract(approved_brief) if "approved" in approved_brief and "output" not in approved_brief else dict(approved_brief)
    if str(brief.get("status") or "approved") != "approved":
        return []
    output = dict(brief.get("output") or {})
    resolved_aspect = str(aspect or output.get("orientation") or "landscape")
    styles = (registry or VISUAL_STYLES).list(include_internal=True)
    styles = [item for item in styles if item.get("style_id") != "test_soft_panel" and (style_id is None or str(item.get("style_id")) == str(style_id))]
    title_resolver = title_registry or TITLE_STYLES
    titles = title_resolver.resolved_metadata()
    titles = [item for item in titles if title_style_id is None or str(item.get("title_style_id")) == str(title_style_id)]
    result: list[dict[str, Any]] = []
    for style in styles:
        resolved_style = (registry or VISUAL_STYLES).resolve(str(style["style_id"]), style.get("version"))
        for title in titles:
            title_id = str(title.get("title_style_id") or "")
            supported_roles = [str(value) for value in title.get("supported_roles") or ()]
            for role in ([str(title_role)] if title_role else supported_roles):
                resolved_title = title_resolver.resolve(title_id, title.get("version"), role=role, aspect=resolved_aspect)
                responsive = dict(resolved_title.get("responsive") or {})
                palette = _apply_palette_override(resolved_style.get("palette") or {}, {"palette_variant": "default"})
                result.append({
                    "key": f"{resolved_style['style_id']}:{title_id}:{role}:{resolved_aspect}",
                    "visual_style_id": str(resolved_style["style_id"]),
                    "visual_style_version": str(resolved_style["version"]),
                    "title_style_id": title_id,
                    "title_style_version": str(resolved_title["version"]),
                    "is_default_title_style": title_id == str(resolved_style.get("default_title_style_id") or ""),
                    "role": role,
                    "aspect": resolved_aspect,
                    "font_family": str(resolved_title.get("font_family") or ""),
                    "weight": int(resolved_title.get("weight") or 0),
                    "size_ratio": float(responsive.get("size_ratio") or 0.0),
                    "size_preset": "normal",
                    "anchor": str(responsive.get("anchor") or ""),
                    "max_width_ratio": float(resolved_title.get("max_width_ratio") or 0.0),
                    "composition": str(resolved_style.get("composition") or ""),
                    "readability": deepcopy(resolved_title.get("readability") or {}),
                    "motion": deepcopy(resolved_title.get("motion") or {}),
                    "palette_variant": "default",
                    "palette": palette,
                    "capability": {"letter_spacing_supported": False, "installed_font_resolution": True},
                    "registry_version": TITLE_STYLE_REGISTRY_VERSION,
                    "registry_hash": title_resolver.hash(),
                })
    return result


def visual_style_options(*, registry: VisualStyleRegistry | None = None, title_registry: TitleStyleRegistry | None = None) -> dict[str, Any]:
    def options(values: Any, labels: Mapping[str, str], *, stringify: bool = False) -> list[dict[str, Any]]:
        result = []
        for value in values:
            identifier = str(value) if stringify else value
            result.append({"id": identifier, "label": labels.get(str(identifier), str(identifier)), "enabled": True, "capability": {}})
        return result

    style_resolver = registry or VISUAL_STYLES
    title_resolver = title_registry or TITLE_STYLES
    return {
        "schema_version": VISUAL_STYLE_SCHEMA_VERSION,
        "registry_version": VISUAL_STYLE_REGISTRY_VERSION,
        "registry_hash": style_resolver.hash(),
        "preview_scope_version": VISUAL_PREVIEW_SCOPE_VERSION,
        # Standalone is a deliberate human comparison option.  Test-only
        # entries remain hidden from the product surface.
        "styles": [item for item in style_resolver.list(include_internal=True) if item.get("style_id") != "test_soft_panel"],
        "title_styles": title_resolver.list(),
        "title_roles": options(sorted({role for item in title_resolver.resolved_metadata() for role in item["supported_roles"]}), TITLE_ROLE_LABELS),
        "title_anchors": options(TITLE_ANCHORS, TITLE_ANCHOR_LABELS),
        "title_motion_presets": options(TITLE_MOTION_PRESETS, TITLE_MOTION_LABELS),
        "title_easing": options(("linear", "ease-out"), {"linear": "Linear", "ease-out": "Ease-out"}),
        "title_letter_spacing": {"supported": False, "fixed_value": 0},
        "title_weight_values": options(TITLE_WEIGHT_VALUES, {str(item): str(item) for item in TITLE_WEIGHT_VALUES}, stringify=True),
        "title_size_presets": options(sorted(TITLE_SIZE_PRESETS), TITLE_SIZE_LABELS),
        "palette_variants": options(("default", "muted", "high_contrast"), TITLE_PALETTE_LABELS),
        "readability_surfaces": options(("none", "translucent", "solid"), TITLE_READABILITY_LABELS),
        "title_font_families": options(TITLE_FONT_FAMILIES, TITLE_FONT_LABELS),
        "compositions": options(("overlay", "standalone"), {"overlay": "Overlay", "standalone": "Standalone"}),
        "override_schema_version": "visual-style-override-v1",
    }


def visual_style_api_payload(
    state: Mapping[str, Any] | None,
    *,
    approved_brief: Mapping[str, Any] | None = None,
    registry: VisualStyleRegistry | None = None,
    title_registry: TitleStyleRegistry | None = None,
) -> dict[str, Any]:
    current = dict(state or {})
    for field in ("recommendation", "approved", "source_provenance"):
        if isinstance(current.get(field), str):
            try:
                current[field] = json.loads(current[field])
            except ValueError:
                current[field] = {}
    if isinstance(current.get("source_provenance"), list):
        current["source_provenance"] = [_public_source(item) for item in current["source_provenance"]]
    current["options"] = visual_style_options(registry=registry, title_registry=title_registry)
    current["options"]["control_defaults"] = visual_style_control_defaults(approved_brief, registry=registry, title_registry=title_registry) if approved_brief else []
    return current


def ensure_visual_style_state(cfg: Mapping[str, Any], db: Path, project_id: int) -> dict[str, Any]:
    init_db(db)
    with connect(db) as con:
        row = con.execute("select * from visual_style_states where project_id=?", (int(project_id),)).fetchone()
        if row:
            state = _state_row(row)
            return _refresh_visual_style_currentity(cfg, db, project_id, state)
        brief = _load_brief(db, project_id)
        recommendation = {
            "visual_style_id": "diary_natural",
            "visual_style_version": "1",
            "reason": "先以日記式、畫面連續的 overlay 視覺作為預設建議；正式預覽仍需 human-approved Creative Brief。",
        }
        status = "needs_confirmation"
        if brief and brief.get("status") == "approved":
            snapshot = materialize_visual_style("diary_natural", brief)
            recommendation = snapshot
        con.execute(
            "insert into visual_style_states(project_id, schema_version, status, recommendation_json, approved_json, source_provenance_json, creative_brief_revision, creative_brief_hash, preview_revision, updated_at) values(?,?,?,?,?,?,?,?,?,?)",
            (int(project_id), VISUAL_STYLE_STATE_SCHEMA_VERSION, status, json.dumps(recommendation, ensure_ascii=False, sort_keys=True), "{}", "{}", int((brief or {}).get("brief_version") or 0), str((brief or {}).get("visual_contract_hash") or ""), 0, _now()),
        )
    return load_visual_style_state(db, project_id)


def load_visual_style_state(db: Path, project_id: int) -> dict[str, Any]:
    init_db(db)
    with connect(db) as con:
        row = con.execute("select * from visual_style_states where project_id=?", (int(project_id),)).fetchone()
    return _state_row(row) if row else {"project_id": int(project_id), "status": "needs_confirmation"}


def _refresh_visual_style_currentity(cfg: Mapping[str, Any], db: Path, project_id: int, state: dict[str, Any]) -> dict[str, Any]:
    """Persist stale status when approved visual semantics no longer match."""

    if str(state.get("status") or "") != "approved" or not isinstance(state.get("approved"), Mapping):
        return state
    brief = _load_brief(db, project_id) or {}
    approved = dict(state.get("approved") or {})
    current_brief_hash = str(brief.get("visual_contract_hash") or "")
    stored_brief_hash = str(state.get("creative_brief_hash") or approved.get("creative_brief_hash") or "")
    reason = ""
    if current_brief_hash != stored_brief_hash:
        reason = "creative_brief_visual_contract_changed"
    else:
        try:
            from .color_consistency import effective_color_settings, load_project_color_state
            current_color = effective_color_settings(load_project_color_state(dict(cfg), int(project_id)))
            current_technical = _technical_transform_snapshot(current_color)
            approved_technical = dict(approved.get("technical_transform") or {})
            if current_technical != approved_technical:
                reason = "technical_transform_changed"
        except Exception as exc:
            reason = f"technical_transform_unavailable:{exc}"
    if not reason:
        return state
    with connect(db) as con:
        con.execute("update visual_style_states set status='stale', updated_at=? where project_id=?", (_now(), int(project_id)))
    return {**state, "status": "stale", "stale_reason": reason}


def save_visual_style_approval(cfg: Mapping[str, Any], db: Path, project_id: int, payload: Mapping[str, Any], *, base_revision: int | None = None) -> dict[str, Any]:
    brief = _load_brief(db, project_id)
    if not brief or brief.get("status") != "approved":
        raise VisualStyleError("creative_brief_required", "Creative Brief 尚未由 human 核准，不能核准 Visual Style")
    style_id = str(payload.get("visual_style_id") or "")
    from .color_consistency import effective_color_settings, load_project_color_state
    project_color = effective_color_settings(load_project_color_state(dict(cfg), project_id))
    snapshot = materialize_visual_style(style_id, brief, style_version=payload.get("visual_style_version"), title_style_id=payload.get("title_style_id"), title_style_version=payload.get("title_style_version"), title_role=str(payload.get("title_role") or "chapter_title"), color_settings=project_color, overrides=payload.get("overrides") if isinstance(payload.get("overrides"), Mapping) else None)
    preview_variant_id = str(payload.get("preview_variant_id") or "").strip()
    preview_plan_hash = str(payload.get("preview_plan_hash") or "").strip()
    current = load_visual_style_state(db, project_id)
    if not preview_variant_id or not preview_plan_hash:
        raise VisualStyleError("visual_style_preview_required", "必須選取目前真實 preview variant，不能只提交任意 hash")
    evidence = _load_preview_evidence(db, project_id, preview_variant_id)
    _validate_preview_evidence(cfg, db, project_id, evidence, snapshot, brief, project_color, preview_plan_hash)
    approval_identity = {
        "preview_variant_id": preview_variant_id,
        "preview_image_sha256": str(evidence["preview_image_sha256"]),
        "source_media_uuid": str(evidence["source_media_uuid"]),
        "generated_at": str(evidence["generated_at"]),
    }
    snapshot["approval_envelope"] = {
        "schema_version": "visual-style-approval-v1",
        "preview_variant_id": preview_variant_id,
        "preview_plan_hash": preview_plan_hash,
        "evidence_identity": approval_identity,
        "approved_at": _now(),
    }
    # These compatibility fields are audit-only.  They are deliberately
    # excluded from semantic_hash/resolved_hash so approval cannot change the
    # pixels or cache identity the human reviewed.
    snapshot["approved_preview_variant_id"] = preview_variant_id
    snapshot["approved_preview_plan_hash"] = preview_plan_hash
    snapshot["approved_preview_evidence_identity"] = approval_identity
    preview_revision = int(current.get("preview_revision") or 0) + 1
    source = _source_provenance(db, project_id)
    # Visual Style is render-only state.  The write still participates in the
    # project optimistic-concurrency boundary, but does not advance Story or
    # Perception semantics.
    with project_commit(db, project_id, base_revision) as commit:
        with connect(db) as con:
            updated = con.execute(
                "update visual_style_states set status='approved', approved_json=?, source_provenance_json=?, creative_brief_revision=?, creative_brief_hash=?, preview_revision=?, updated_at=? where project_id=?",
                (json.dumps(snapshot, ensure_ascii=False, sort_keys=True), json.dumps(source, ensure_ascii=False, sort_keys=True), int(brief.get("brief_version") or 1), str(brief.get("visual_contract_hash") or ""), preview_revision, _now(), int(project_id)),
            )
            if updated.rowcount != 1:
                raise VisualStyleError("visual_style_state_missing", "visual style state is missing")
        commit.record_changed(False)
    return load_visual_style_state(db, project_id)


def _load_preview_evidence(db: Path, project_id: int, preview_variant_id: str) -> dict[str, Any]:
    init_db(db)
    with connect(db) as con:
        row = con.execute("select * from visual_style_preview_evidence where project_id=? and preview_variant_id=?", (int(project_id), preview_variant_id)).fetchone()
    if not row:
        raise VisualStyleError("visual_style_preview_stale", "preview variant 不存在或不屬於此 project")
    result = dict(row)
    for field in ("technical_transform_json", "source_fingerprint_json", "representative_frame_json", "title_render_evidence_json"):
        try:
            result[field.removesuffix("_json")] = json.loads(result.get(field) or "{}")
        except (TypeError, ValueError):
            result[field.removesuffix("_json")] = {}
    return result


def _validate_preview_evidence(
    cfg: Mapping[str, Any],
    db: Path,
    project_id: int,
    evidence: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    brief: Mapping[str, Any],
    project_color: Mapping[str, Any],
    preview_plan_hash: str,
) -> None:
    if str(evidence.get("preview_plan_hash") or "") != preview_plan_hash:
        raise VisualStyleError("visual_style_preview_stale", "preview plan hash 與 variant evidence 不一致")
    if str(evidence.get("visual_style_id") or "") != str(snapshot.get("visual_style_id") or "") or str(evidence.get("visual_style_version") or "") != str(snapshot.get("visual_style_version") or ""):
        raise VisualStyleError("visual_style_preview_stale", "preview variant 的 visual style 不符合目前核准選項")
    if str(evidence.get("visual_style_hash") or "") != str(snapshot.get("semantic_hash") or snapshot.get("resolved_hash") or ""):
        raise VisualStyleError("visual_style_preview_stale", "preview variant 的 style snapshot 已過期")
    if str(evidence.get("creative_brief_hash") or "") != str(brief.get("visual_contract_hash") or ""):
        raise VisualStyleError("visual_style_preview_stale", "Creative Brief visual contract 已過期")
    current_technical = _technical_transform_snapshot(project_color)
    if dict(evidence.get("technical_transform") or {}) != current_technical:
        raise VisualStyleError("visual_style_preview_stale", "technical/LUT transform 已變更，必須重新預覽")
    expected_title_identity = _hash(snapshot.get("title_style") or {})
    if str(evidence.get("title_style_identity") or "") != expected_title_identity:
        raise VisualStyleError("visual_style_preview_stale", "title style identity 與 preview 不一致")
    source_uuid = str(evidence.get("source_media_uuid") or "")
    current_sources = _source_provenance(db, project_id)
    current_source = next((item for item in current_sources if str(item.get("project_media_uuid") or "") == source_uuid), None)
    if current_source is None or dict(current_source.get("fingerprint") or {}) != dict(evidence.get("source_fingerprint") or {}):
        raise VisualStyleError("visual_style_preview_stale", "preview source fingerprint 已變更")
    preview_path = visual_style_preview_path(cfg, project_id, str(evidence.get("preview_filename") or ""))
    if not preview_path.is_file() or _file_hash(preview_path) != str(evidence.get("preview_image_sha256") or ""):
        raise VisualStyleError("visual_style_preview_stale", "preview image evidence 不存在或已變更")
    title_evidence = evidence.get("title_render_evidence") or {}
    if not isinstance(title_evidence, Mapping) or str(title_evidence.get("version") or "") != TITLE_PIXEL_EVIDENCE_VERSION or title_evidence.get("status") != "pass" or int(title_evidence.get("changed_pixels") or 0) <= 0 or not bool(title_evidence.get("in_frame")):
        raise VisualStyleError("visual_style_title_not_visible", "preview 沒有可稽核的 title pixel evidence")


def build_preview_filter(snapshot: Mapping[str, Any], *, width: int, height: int, title_text: str = "") -> str:
    """Return the exact filter graph used by the shared render resolver."""
    plan = resolve_visual_render_plan(snapshot, width=width, height=height, title_text=title_text)
    if str(plan.get("graph_type") or "linear") != "linear":
        return materialize_visual_graph(plan, input_label="[0:v]", output_label="[vout]")
    return str(plan.get("filter_graph") or "")


def materialize_visual_graph(plan: Mapping[str, Any], *, input_label: str, output_label: str) -> str:
    """Materialize a validated visual graph at an explicit input/output port.

    The semantic plan never assumes whether it is being attached to Preview's
    ``[0:v]`` or Formal Render's trimmed ``[visual_in]`` stream.  Both callers
    use this one port-aware boundary instead of guessing with string replaces.
    """

    contract = plan.get("graph_contract") if isinstance(plan.get("graph_contract"), Mapping) else {}
    template = str(contract.get("template") or plan.get("filter_complex") or "")
    if not template or not input_label.startswith("[") or not input_label.endswith("]") or not output_label.startswith("[") or not output_label.endswith("]"):
        raise VisualStyleError("visual_graph_contract_invalid", "visual graph input/output ports are invalid")
    if "[[VV_INPUT]]" not in template or "[[VV_OUTPUT]]" not in template:
        raise VisualStyleError("visual_graph_contract_invalid", "visual graph is missing explicit input/output ports")
    return template.replace("[[VV_INPUT]]", input_label).replace("[[VV_OUTPUT]]", output_label)


def resolve_visual_render_plan(
    snapshot: Mapping[str, Any],
    *,
    width: int,
    height: int,
    title_text: str = "",
    color_settings: Mapping[str, Any] | None = None,
    source_display_ratio: float | None = None,
    source_geometry: Mapping[str, Any] | None = None,
    title_enable: str = "",
    title_role: str | None = None,
    title_duration_seconds: float | None = None,
    title_time_offset_seconds: float = 0.0,
) -> dict[str, Any]:
    """Resolve one immutable, renderer-ready Visual Style contract.

    Preview and formal segment rendering both consume this structure.  The
    semantic snapshot remains the approval input; this function is the only
    place that turns it into pixel-affecting filters.  In particular, DJI/LUT
    technical transforms are delegated to ``color_pipeline`` and are never
    represented as a generic EQ look.
    """
    missing = [field for field in ("resolved_hash", "output", "framing", "grading", "title_style", "palette") if not snapshot.get(field)]
    expected_snapshot_hash = _semantic_hash(snapshot)
    if missing or str(snapshot.get("semantic_hash") or snapshot.get("resolved_hash") or "") != expected_snapshot_hash:
        raise VisualStyleError("visual_render_contract_invalid", "materialized visual style snapshot is incomplete or tampered")
    width = int(width)
    height = int(height)
    if width <= 0 or height <= 0:
        raise VisualStyleError("visual_render_dimensions_invalid", "visual render dimensions must be positive")
    framing = _resolve_source_framing(snapshot, source_display_ratio, source_geometry)
    strategy = str(framing.get("strategy_id") or "preserve_full_frame")
    canonical_geometry_filter = _canonical_display_geometry_filter(source_geometry)
    if strategy == "crop_reframe":
        framing_filter = ",".join(part for part in (canonical_geometry_filter, f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1") if part)
        graph_type = "linear"
        filter_complex = ""
    elif strategy == "background_treatment":
        # A real background treatment: the displayed, square-pixel source is
        # split into a blurred fill branch and an aspect-preserving foreground.
        # Keeping this as a graph (rather than hiding it in -vf) makes the
        # Preview and Formal Render boundaries identical and auditable.
        background = (
            f"[[VV_INPUT]]{canonical_geometry_filter}{',' if canonical_geometry_filter else ''}split=2[style_bg][style_fg];"
            f"[style_bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}:(iw-ow)/2:(ih-oh)/2,boxblur=luma_radius=18:luma_power=1,"
            "eq=brightness=-0.08:saturation=0.78[style_bg_fill];"
            f"[style_fg]scale={width}:{height}:force_original_aspect_ratio=decrease[style_fg_fit];"
            "[style_bg_fill][style_fg_fit]overlay=(W-w)/2:(H-h)/2,setsar=1"
        )
        framing_filter = ""
        graph_type = "split_background_overlay"
        filter_complex = background
    elif strategy == "preserve_full_frame":
        framing_filter = ",".join(part for part in (canonical_geometry_filter, f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x20242a,setsar=1") if part)
        graph_type = "linear"
        filter_complex = ""
    else:
        raise VisualStyleError("framing_unknown", f"unknown framing strategy: {strategy}")

    grading = dict(snapshot.get("grading") or {})
    actual_color = dict(color_settings or {})
    technical_mode = str(actual_color.get("mode") or "none")
    technical_filter = build_color_filter(actual_color)
    if technical_mode in {"dji_lut", "dji_dlog", "dji_dlog_m"}:
        # validate_lut_resource also enforces user-managed .cube existence;
        # the formal path must not silently fall back to a fake grade.
        lut = validate_lut_resource(actual_color)
        lut_identity = {"sha256": _file_hash(lut), "size": lut.stat().st_size}
    else:
        lut_identity = {}
    creative_settings = {
        "mode": "manual",
        "exposure": float(grading.get("brightness") or 0.0) / 0.12,
        "contrast": float(grading.get("contrast") or 1.0),
        "saturation": float(grading.get("saturation") or 1.0),
        "temperature": float(grading.get("temperature") or 0.0),
        "tint": float(grading.get("tint") or 0.0),
        "gamma": float(grading.get("gamma") or 1.0),
        "highlights": float(grading.get("highlights") or 0.0),
        "shadows": float(grading.get("shadows") or 0.0),
    }
    creative_filter = build_color_filter(creative_settings)
    color_filter = ",".join(value for value in (technical_filter, creative_filter) if value)
    title_plan = _resolve_title_plan(snapshot, width, height, title_text, title_enable=title_enable, title_role=title_role, duration_seconds=title_duration_seconds, time_offset_seconds=title_time_offset_seconds)
    filter_parts = [framing_filter]
    if color_filter:
        filter_parts.append(color_filter)
    if title_plan["filter"]:
        filter_parts.append(title_plan["filter"])
    if str(snapshot.get("composition") or "overlay") == "standalone":
        palette = str((snapshot.get("palette") or {}).get("surface_overlay_strong") or "#20242a").lstrip("#")[:6]
        filter_parts.insert(0, f"drawbox=x=0:y=0:w=iw:h=ih:color=0x{palette}@0.82:t=fill")
    if graph_type == "split_background_overlay":
        graph_suffix = ",".join(value for value in filter_parts if value)
        if graph_suffix:
            filter_complex = filter_complex + "," + graph_suffix
        filter_complex = filter_complex + "[[VV_OUTPUT]]"
    plan = {
        "contract_version": VISUAL_RENDER_CONTRACT_VERSION,
        "visual_style_hash": str(snapshot.get("semantic_hash") or snapshot.get("resolved_hash") or ""),
        "width": width,
        "height": height,
        "source_display_ratio": float((framing.get("source_display_ratio") or source_display_ratio or 0.0) or 0.0),
        "source_geometry": dict(source_geometry or {}),
        "framing": {**framing, "filter": framing_filter},
        "technical_transform": {
            **color_mode_contract(technical_mode),
            "source_colorspace": str(actual_color.get("source_colorspace") or grading.get("source_colorspace") or "unknown"),
            "lut_identity": lut_identity,
            "applied_once": technical_mode in {"dji_lut", "dji_dlog", "dji_dlog_m"},
        },
        "creative_look": {key: value for key, value in creative_settings.items() if key != "mode"},
        "color_filter": color_filter,
        "title": title_plan,
        "composition": str(snapshot.get("composition") or "overlay"),
        "motion": deepcopy((snapshot.get("title_style") or {}).get("motion") or {}),
        "filter_graph": ",".join(filter_parts),
        "graph_type": graph_type,
        "filter_complex": filter_complex,
        "graph_contract": ({"version": "visual-graph-v1", "graph_type": graph_type, "input_port": "video_in", "output_port": "video_out", "template": filter_complex} if graph_type != "linear" else {}),
        "execution_mode": "mixed_cpu_visual_filters" if (color_filter or title_plan["filter"] or graph_type != "linear") else "native_renderer_filters",
    }
    plan["semantic_hash"] = _hash({key: value for key, value in plan.items() if key not in {"resolved_hash", "semantic_hash"}})
    plan["resolved_hash"] = plan["semantic_hash"]
    return plan


def _resolve_source_framing(
    snapshot: Mapping[str, Any],
    source_display_ratio: float | None,
    source_geometry: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Resolve the approved mismatch matrix for this source segment.

    The stored snapshot contains both directions.  ``source_display_ratio``
    is runtime evidence from VID-39, not a UI guess; same-orientation sources
    deliberately receive neutral full-frame treatment.
    """

    stored = dict(snapshot.get("framing") or {})
    output = dict(snapshot.get("output") or {})
    target = str(output.get("orientation") or stored.get("target_orientation") or "landscape")
    geometry = dict(source_geometry or {})
    ratio = float(geometry.get("display_ratio") or source_display_ratio or 0.0)
    source = str(geometry.get("source_orientation") or "")
    if source not in {"portrait", "landscape"}:
        source = "portrait" if ratio and ratio < 1 else "landscape" if ratio else str(stored.get("source_orientation") or "unknown")
    direction_id = ""
    if source in {"portrait", "landscape"} and target in {"portrait", "landscape"} and source != target:
        direction_id = f"{source}_source_in_{target}"
    policies = stored.get("policy_matrix") if isinstance(stored.get("policy_matrix"), Mapping) else {}
    selected = dict(policies.get(direction_id) or {}) if direction_id and (source in {"portrait", "landscape"} and target in {"portrait", "landscape"} and source != target) else {}
    if not selected and source in {"portrait", "landscape"} and source == target:
        selected = {"strategy_id": "preserve_full_frame", "strategy_version": "1", "resolved_semantic": {}}
    if not selected:
        # Backward-compatible materialized snapshots only contain the target
        # mismatch policy.  Real render calls pass geometry and new approvals
        # always contain the full matrix.
        selected = dict(stored)
    strategy = str(selected.get("strategy_id") or selected.get("approved_strategy_id") or selected.get("approved_strategy") or "preserve_full_frame")
    if strategy == "auto_recommended":
        strategy = "crop_reframe"
    if strategy not in {"crop_reframe", "background_treatment", "preserve_full_frame"}:
        raise VisualStyleError("framing_unknown", f"unknown approved framing strategy: {strategy}")
    return {
        "direction_id": direction_id or "same_orientation",
        "source_orientation": source,
        "target_orientation": target,
        "source_display_ratio": ratio,
        "strategy_id": strategy,
        "strategy_version": str(selected.get("strategy_version") or selected.get("approved_strategy_version") or "1"),
        "resolved_semantic": deepcopy(selected.get("resolved_semantic") or {}),
    }


def _canonical_display_geometry_filter(source_geometry: Mapping[str, Any] | None) -> str:
    """Normalize SAR after FFmpeg's CPU autorotate/display axes are applied."""

    geometry = dict(source_geometry or {})
    raw_sar = str(geometry.get("sample_aspect_ratio") or "1:1").strip().replace("/", ":")
    try:
        numerator_text, denominator_text = raw_sar.split(":", 1)
        numerator, denominator = int(numerator_text), int(denominator_text)
    except (TypeError, ValueError):
        numerator, denominator = 1, 1
    if numerator <= 0 or denominator <= 0:
        numerator, denominator = 1, 1
    if abs(int(geometry.get("rotation_degrees") or 0)) % 180 == 90:
        numerator, denominator = denominator, numerator
    if numerator == denominator:
        return "setsar=1"
    return f"scale=ceil(iw*{numerator}/{denominator}/2)*2:ih:eval=init,setsar=1"


def _resolve_title_plan(
    snapshot: Mapping[str, Any],
    width: int,
    height: int,
    title_text: str,
    *,
    title_enable: str = "",
    title_role: str | None = None,
    duration_seconds: float | None = None,
    time_offset_seconds: float = 0.0,
) -> dict[str, Any]:
    aspect = str((snapshot.get("output") or {}).get("orientation") or "landscape")
    title_style = dict(snapshot.get("title_style") or {})
    role = str(title_role or title_style.get("role") or "chapter_title")
    title_style = _resolve_title_role_contract(title_style, role, aspect)
    responsive = dict(title_style.get("responsive") or {})
    safe = dict(title_style.get("safe_zone") or {})
    anchor = str(responsive.get("anchor") or "bottom-left")
    if anchor not in TITLE_ANCHORS:
        raise VisualStyleError("title_anchor_unsupported", f"unsupported title anchor: {anchor}")
    size_ratio = float(responsive.get("size_ratio") or 0.052)
    left = float(safe.get("left") or 0.05)
    right = float(safe.get("right") or 0.05)
    top = float(safe.get("top") or 0.06)
    bottom = float(safe.get("bottom") or 0.08)
    if anchor == "top-left":
        x, y = f"w*{left:.6f}", f"h*{top:.6f}"
    elif anchor == "top-center":
        x, y = "(w-text_w)/2", f"h*{top:.6f}"
    elif anchor == "top-right":
        x, y = f"w*(1-{right:.6f})-text_w", f"h*{top:.6f}"
    elif anchor == "center":
        x, y = "(w-text_w)/2", "(h-text_h)/2"
    elif anchor == "bottom-right":
        x, y = f"w*(1-{right:.6f})-text_w", f"h*(1-{bottom:.6f})-text_h"
    elif anchor == "bottom-center":
        x, y = "(w-text_w)/2", f"h*(1-{bottom:.6f})-text_h"
    elif anchor == "bottom-left":
        x, y = f"w*{left:.6f}", f"h*(1-{bottom:.6f})-text_h"
    else:
        raise VisualStyleError("title_anchor_unsupported", f"unsupported title anchor: {anchor}")
    palette = dict(snapshot.get("palette") or {})
    text_token = str(title_style.get("text_color_token") or "text_primary")
    color = str(palette.get(text_token) or palette.get("text_primary") or "#FFFFFF").lstrip("#")[:6]
    shadow = str(palette.get("shadow") or "#00000099").lstrip("#")[:6]
    font_identity = _resolve_font(
        [title_style.get("font_family"), *(title_style.get("fallback_chain") or [])],
        int(title_style.get("weight") or 500),
        text=str(title_text),
    )
    font_path = font_identity["path"]
    if font_path is None:
        raise VisualStyleError("title_font_unresolved", "declared title font and fallback chain are not installed")
    font_option = f":fontfile='{_escape_filter_path(font_path)}'" if font_path else ""
    readability = dict(title_style.get("readability") or {})
    max_width_ratio = min(0.95, max(0.2, float(title_style.get("max_width_ratio") or 0.78)))
    max_width_pixels = max(1, int(width * max_width_ratio))
    font_size = max(20, int(height * size_ratio))
    wrapped_text, wrap_lines = _wrap_title_text(str(title_text), max_width_pixels, font_size)
    # Keep actual newlines in the filter value.  The Windows FFmpeg builds used
    # by the product treat a single ``\\n`` as an escaped literal ``n`` rather
    # than a line break, which made a valid CJK/English title render as ``/n``
    # and clip the second line.  Other filter metacharacters still need the
    # normal escaping below.
    escaped = wrapped_text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    letter_spacing = float(title_style.get("letter_spacing") or 0.0)
    if abs(letter_spacing) > 0.001:
        # Current supported FFmpeg drawtext builds expose line_spacing but not
        # letter_spacing.  Do not advertise a token that pixels cannot consume.
        raise VisualStyleError("title_letter_spacing_unsupported", "non-zero letter_spacing is not supported by the active FFmpeg drawtext contract")
    options = [f"text='{escaped}'", font_option.lstrip(":"), f"fontcolor=0x{color}", f"fontsize={font_size}", f"x={x}", f"y={y}", f"text_align={str(title_style.get('alignment') or 'left')}", f"line_spacing={int(float(title_style.get('line_height') or 1.18) * 10)}", f"boxw={max_width_pixels}", "fix_bounds=1"]
    if bool(readability.get("shadow", True)):
        options.extend([f"shadowcolor=0x{shadow}", "shadowx=2", "shadowy=2"])
    outline = int(readability.get("outline") or 0)
    if outline:
        options.extend(["borderw=" + str(outline), f"bordercolor=0x{shadow}"])
    if str(readability.get("surface") or "") in {"translucent", "solid"}:
        surface = str(palette.get("surface_overlay") or "#00000099").lstrip("#")[:8]
        options.extend(["box=1", f"boxcolor=0x{surface}", "boxborderw=18"])
    motion = dict(title_style.get("motion") or {})
    preset = str(motion.get("preset") or "none")
    if preset not in {"none", "fade", "fade_rise", "slide_fade"}:
        raise VisualStyleError("title_motion_unsupported", f"unsupported title motion preset: {preset}")
    easing = str(motion.get("easing") or "ease-out")
    if easing not in {"linear", "ease-out"}:
        raise VisualStyleError("title_easing_unsupported", f"unsupported title easing: {easing}")
    enter = max(0.001, float(motion.get("enter_seconds") or 0.28))
    exit_seconds = max(0.001, float(motion.get("exit_seconds") or 0.22))
    duration = max(enter + exit_seconds, float(duration_seconds or 1.0))
    # A non-zero value is a deterministic preview sample time, not an
    # addition to the source PTS.  Static previews are taken after a seek and
    # FFmpeg may expose either the source or output timestamp to drawtext;
    # using a literal sample time keeps animated title visibility independent
    # of that implementation detail.  Formal rendering leaves this at zero
    # and continues to use the frame clock (`t`).
    time_expr = "t" if abs(float(time_offset_seconds or 0.0)) < 0.000001 else f"{max(0.0, float(time_offset_seconds)):.6f}"
    enter_progress = f"min({time_expr}/{enter:.6f}\\,1)"
    exit_progress = f"min(max(0\\,({duration:.6f}-{time_expr})/{exit_seconds:.6f})\\,1)"
    if easing == "ease-out":
        enter_progress = f"(1-pow(1-{enter_progress}\\,2))"
        exit_progress = f"pow({exit_progress}\\,2)"
    alpha = f"if(lt({time_expr}\\,{enter:.6f})\\,{enter_progress}\\,if(lt({time_expr}\\,{max(enter, duration-exit_seconds):.6f})\\,1\\,{exit_progress}))"
    if preset in {"fade", "fade_rise", "slide_fade"}:
        options.append(f"alpha={alpha}")
    if preset == "fade_rise":
        options[-1:] = [f"alpha={alpha}", f"y=({y})+((1-min({time_expr}/{enter:.6f}\\,1))*{max(4, int(height * 0.018))})"]
    elif preset == "slide_fade":
        options[-1:] = [f"alpha={alpha}", f"x=({x})-((1-min({time_expr}/{enter:.6f}\\,1))*{max(6, int(width * 0.025))})"]
    if title_enable:
        options.append(f"enable='{title_enable}'")
    drawtext = "drawtext=" + ":".join(option for option in options if option)
    title_plan = {"role": role, "text": str(title_text), "wrapped_text": wrapped_text, "wrap_lines": wrap_lines, "anchor": anchor, "safe_zone": safe, "max_width_ratio": max_width_ratio, "max_width_pixels": max_width_pixels, "max_width_enforced": True, "font_path": str(font_path or ""), "font_identity": {key: value for key, value in font_identity.items() if key != "path"}, "font_family": str(title_style.get("font_family") or "system-sans"), "letter_spacing": letter_spacing, "weight": int(title_style.get("weight") or 500), "font_size": font_size, "line_height": float(title_style.get("line_height") or 1.18), "readability": deepcopy(readability), "filter": drawtext if title_text else "", "time_offset_seconds": max(0.0, float(time_offset_seconds or 0.0)), "motion": {**motion, "easing": easing, "resolved_enter_seconds": enter, "resolved_exit_seconds": exit_seconds, "resolved_duration_seconds": duration}}
    title_plan["bbox"] = _title_bbox(title_plan, width=width, height=height) if title_text else {"x": 0, "y": 0, "width": 0, "height": 0, "in_frame": True}
    return title_plan


def _title_bbox(title_plan: Mapping[str, Any], *, width: int, height: int) -> dict[str, Any]:
    """Return a conservative, deterministic pixel region for title evidence."""

    safe = dict(title_plan.get("safe_zone") or {})
    left = float(safe.get("left") or 0.05)
    right = float(safe.get("right") or 0.05)
    top = float(safe.get("top") or 0.06)
    bottom = float(safe.get("bottom") or 0.08)
    padding = 18 if str((title_plan.get("readability") or {}).get("surface") or "translucent") in {"translucent", "solid"} else 4
    text_width = min(int(title_plan.get("max_width_pixels") or width), max(1, int(title_plan.get("font_size") or 20) * 2))
    text_height = max(1, int(round(float(title_plan.get("font_size") or 20) * float(title_plan.get("line_height") or 1.18) * int(title_plan.get("wrap_lines") or 1))))
    box_width = min(width, max(text_width, int(title_plan.get("max_width_pixels") or text_width)) + padding * 2)
    box_height = min(height, text_height + padding * 2)
    anchor = str(title_plan.get("anchor") or "bottom-left")
    if anchor in {"top-left", "bottom-left"}:
        x = int(round(width * left)) - padding
    elif anchor in {"top-right", "bottom-right"}:
        x = int(round(width * (1 - right))) - box_width + padding
    else:
        x = (width - box_width) // 2
    if anchor in {"top-left", "top-center", "top-right"}:
        y = int(round(height * top)) - padding
    elif anchor in {"bottom-left", "bottom-center", "bottom-right"}:
        y = int(round(height * (1 - bottom))) - box_height
    else:
        y = (height - box_height) // 2
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(width, x + box_width), min(height, y + box_height)
    return {"x": x0, "y": y0, "width": max(0, x1 - x0), "height": max(0, y1 - y0), "in_frame": x >= 0 and y >= 0 and x + box_width <= width and y + box_height <= height}


def _wrap_title_text(text: str, max_width_pixels: int, font_size: int, *, max_lines: int = 3) -> tuple[str, int]:
    """Wrap and bound titles before FFmpeg drawtext sees them.

    FFmpeg's ``boxw`` bounds the box, but does not make the title text itself
    wrap.  This deterministic display-unit wrapper is shared by Preview and
    Formal Render through the resolved title plan and clamps overlong content
    to the approved safe width instead of allowing silent clipping.
    """

    limit = max(4, int(max_width_pixels / max(1.0, float(font_size) * 0.92)))
    def display_width(value: str) -> int:
        return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in value)

    def hard_chunks(value: str) -> list[str]:
        chunks: list[str] = []
        current = ""
        units = 0
        for char in value:
            width = 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
            if current and units + width > limit:
                chunks.append(current)
                current, units = "", 0
            current += char
            units += width
        if current or not chunks:
            chunks.append(current)
        return chunks

    lines: list[str] = []
    for raw_line in str(text).split("\n"):
        words = raw_line.split(" ")
        current = ""
        if len(words) == 1:
            lines.extend(hard_chunks(raw_line))
            continue
        for word in words:
            if not word:
                continue
            if display_width(word) > limit:
                if current:
                    lines.append(current.rstrip())
                    current = ""
                chunks = hard_chunks(word)
                lines.extend(chunks[:-1])
                current = chunks[-1]
                continue
            candidate = word if not current else f"{current} {word}"
            if current and display_width(candidate) > limit:
                lines.append(current.rstrip())
                current = word
            else:
                current = candidate
        lines.append(current.rstrip())
    if not lines:
        lines.append("")
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1].rstrip()
        if not last.endswith("…"):
            lines[-1] = (last[:-1] if last else "") + "…"
    return "\n".join(lines), len(lines)


def render_true_frame_preview(
    cfg: Mapping[str, Any],
    project_id: int,
    source: Path,
    timestamp_seconds: float,
    snapshot: Mapping[str, Any],
    output: Path,
    *,
    runner: Any | None = None,
    color_settings: Mapping[str, Any] | None = None,
    source_display_ratio: float | None = None,
    source_geometry: Mapping[str, Any] | None = None,
    title_text: str = "咖啡日記 / Coffee Diary",
    title_role: str = "chapter_title",
    title_duration_seconds: float | None = None,
) -> dict[str, Any]:
    """Render one real source frame through the same semantic filter resolver."""
    output.parent.mkdir(parents=True, exist_ok=True)
    width = int((snapshot.get("output") or {}).get("width") or 1920)
    height = int((snapshot.get("output") or {}).get("height") or 1080)
    motion_preset = str(((snapshot.get("title_style") or {}).get("motion") or {}).get("preset") or "none")
    title_time_offset = PREVIEW_TITLE_SAMPLE_SECONDS if title_text and motion_preset != "none" else 0.0
    # Keep the resolved title semantics identical to formal rendering.  The
    # preview-only clock is applied by the seek/output graph below because
    # ffmpeg's output-side -ss can otherwise leave drawtext's `t` at the
    # source timestamp (making fade/fade_rise appear already finished).
    plan = resolve_visual_render_plan(snapshot, width=width, height=height, title_text=title_text, color_settings=color_settings, source_display_ratio=source_display_ratio, source_geometry=source_geometry, title_role=title_role, title_duration_seconds=title_duration_seconds, title_time_offset_seconds=title_time_offset)
    filter_graph = _preview_filter_graph(plan)
    result = _run_preview_frame(cfg, source, timestamp_seconds, output, plan, filter_graph, runner=runner)
    if int(getattr(result, "returncode", 0)) != 0 or not output.is_file() or output.stat().st_size <= 0:
        stderr = str(getattr(result, "stderr", ""))[-1200:]
        raise VisualStyleError("preview_render_failed", f"true-frame preview failed: {stderr}")
    title_evidence: dict[str, Any]
    if title_text and runner is None:
        baseline_handle = tempfile.NamedTemporaryFile(prefix=f".{output.stem}.", suffix=".no-title.png", dir=str(output.parent), delete=False)
        baseline = Path(baseline_handle.name)
        baseline_handle.close()
        try:
            baseline_plan = resolve_visual_render_plan(snapshot, width=width, height=height, title_text="", color_settings=color_settings, source_display_ratio=source_display_ratio, source_geometry=source_geometry, title_role=title_role, title_duration_seconds=title_duration_seconds)
            baseline_filter = _preview_filter_graph(baseline_plan)
            baseline_result = _run_preview_frame(cfg, source, timestamp_seconds, baseline, baseline_plan, baseline_filter, runner=None)
            if int(getattr(baseline_result, "returncode", 0)) != 0 or not baseline.is_file() or baseline.stat().st_size <= 0:
                stderr = str(getattr(baseline_result, "stderr", ""))[-1200:]
                raise VisualStyleError("preview_render_failed", f"no-title baseline failed: {stderr}")
            title_evidence = _title_pixel_evidence(str(cfg["ffmpeg_path"]), output, baseline, plan, width=width, height=height)
        finally:
            baseline.unlink(missing_ok=True)
        if title_evidence.get("status") != "pass":
            raise VisualStyleError("title_visibility_failed", "title render completed without a measurable title pixel delta")
    else:
        title_evidence = {"version": TITLE_PIXEL_EVIDENCE_VERSION, "status": "unmeasured", "reason": "injected_runner" if runner is not None else "title_text_empty", "bbox": dict((plan.get("title") or {}).get("bbox") or {})}
    return {"file": str(output), "sha256": _file_hash(output), "timestamp_seconds": float(timestamp_seconds), "width": width, "height": height, "filter_contract": filter_graph, "visual_render_plan": plan, "visual_style_hash": snapshot.get("semantic_hash") or snapshot.get("resolved_hash"), "title_text": title_text, "title_render_evidence": title_evidence, "title_preview_time_offset_seconds": title_time_offset}


def render_animated_title_preview(
    cfg: Mapping[str, Any],
    project_id: int,
    source: Path,
    timestamp_seconds: float,
    snapshot: Mapping[str, Any],
    output: Path,
    *,
    runner: Any | None = None,
    color_settings: Mapping[str, Any] | None = None,
    source_geometry: Mapping[str, Any] | None = None,
    title_text: str = "咖啡日記 / Coffee Diary",
    title_role: str = "chapter_title",
    duration_seconds: float = 2.0,
) -> dict[str, Any]:
    """Render a bounded MP4 title preview through the exact shared plan."""
    duration = min(2.5, max(1.5, float(duration_seconds)))
    width = int((snapshot.get("output") or {}).get("width") or 1920)
    height = int((snapshot.get("output") or {}).get("height") or 1080)
    plan = resolve_visual_render_plan(snapshot, width=width, height=height, title_text=title_text, color_settings=color_settings, source_geometry=source_geometry, title_role=title_role, title_duration_seconds=duration)
    command = _preview_ffmpeg_prefix(cfg, source, timestamp_seconds)
    if str(plan.get("graph_type") or "linear") == "split_background_overlay":
        command.extend(["-filter_complex", _preview_filter_graph(plan), "-map", "[vout]"])
    else:
        command.extend(["-vf", _preview_filter_graph(plan)])
    command.extend(["-t", f"{duration:.3f}", "-an", "-r", "24", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output)])
    output.parent.mkdir(parents=True, exist_ok=True)
    result = runner(command) if runner else subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)
    if int(getattr(result, "returncode", 0)) != 0 or not output.is_file() or output.stat().st_size <= 0:
        stderr = str(getattr(result, "stderr", ""))[-1200:]
        raise VisualStyleError("preview_render_failed", f"animated title preview failed: {stderr}")
    return {"file": str(output), "sha256": _file_hash(output), "timestamp_seconds": float(timestamp_seconds), "width": width, "height": height, "duration_seconds": duration, "filter_contract": str(plan.get("filter_graph") or plan.get("filter_complex") or ""), "visual_render_plan": plan, "visual_style_hash": snapshot.get("semantic_hash") or snapshot.get("resolved_hash"), "title_text": title_text, "preview_kind": "animated"}


def _preview_seek_args(timestamp_seconds: float, *, preroll_seconds: float = PREVIEW_SEEK_PREROLL_SECONDS) -> list[str]:
    """Return one shared bounded-seek contract for static and animated previews.

    Input-side seeking bounds the expensive decode to a small pre-roll window;
    the residual output-side seek retains frame accuracy.  Near the beginning
    the input seek stays at zero so short/synthetic fixtures remain decodable.
    """
    target = max(0.0, float(timestamp_seconds))
    preroll = max(0.0, float(preroll_seconds))
    input_timestamp = max(0.0, target - preroll)
    residual = target - input_timestamp
    return ["-ss", f"{input_timestamp:.6f}", "-i", "__SOURCE__", "-ss", f"{residual:.6f}"]


def _preview_ffmpeg_prefix(cfg: Mapping[str, Any], source: Path, timestamp_seconds: float) -> list[str]:
    seek = _preview_seek_args(timestamp_seconds)
    seek[3] = str(source)
    return [str(cfg["ffmpeg_path"]), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *seek]


def _preview_filter_graph(plan: Mapping[str, Any]) -> str:
    """Reset the seek clock for a deterministic static/animated preview."""

    # Reset the seek clock for animated previews; static title sampling is
    # materialized in the title plan as a literal time expression.
    if str(plan.get("graph_type") or "linear") == "split_background_overlay":
        graph = materialize_visual_graph(plan, input_label="[0:v]", output_label="[vout]")
        graph = graph.replace("[0:v]", "[0:v]setpts=PTS-STARTPTS[preview_clock];[preview_clock]", 1)
        return graph
    graph = str(plan.get("filter_graph") or "")
    return f"setpts=PTS-STARTPTS,{graph}" if graph else graph


def _run_preview_frame(cfg: Mapping[str, Any], source: Path, timestamp_seconds: float, output: Path, plan: Mapping[str, Any], filter_graph: str, *, runner: Any | None) -> Any:
    command = _preview_ffmpeg_prefix(cfg, source, timestamp_seconds)
    if str(plan.get("graph_type") or "linear") == "split_background_overlay":
        command.extend(["-filter_complex", filter_graph, "-map", "[vout]"])
    else:
        command.extend(["-vf", filter_graph])
    command.extend(["-frames:v", "1", str(output)])
    return runner(command) if runner else subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)


def _read_rgb_frame(ffmpeg_path: str, image: Path, *, width: int, height: int) -> bytes:
    result = subprocess.run([ffmpeg_path, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(image), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=False, timeout=30)
    if result.returncode != 0 or len(result.stdout) < width * height * 3:
        raise VisualStyleError("title_pixel_evidence_failed", f"pixel evidence decode failed: {result.stderr[-1200:].decode('utf-8', errors='replace')}")
    return bytes(result.stdout[: width * height * 3])


def _title_pixel_evidence(ffmpeg_path: str, rendered: Path, baseline: Path, plan: Mapping[str, Any], *, width: int, height: int) -> dict[str, Any]:
    bbox = dict((plan.get("title") or {}).get("bbox") or {})
    x0, y0 = max(0, int(bbox.get("x") or 0)), max(0, int(bbox.get("y") or 0))
    x1, y1 = min(width, x0 + max(0, int(bbox.get("width") or 0))), min(height, y0 + max(0, int(bbox.get("height") or 0)))
    if x1 <= x0 or y1 <= y0:
        return {"version": TITLE_PIXEL_EVIDENCE_VERSION, "status": "failed", "reason": "empty_title_bbox", "bbox": bbox, "in_frame": False}
    current = _read_rgb_frame(ffmpeg_path, rendered, width=width, height=height)
    previous = _read_rgb_frame(ffmpeg_path, baseline, width=width, height=height)
    changed_pixels = 0
    total_abs_delta = 0
    max_channel_delta = 0
    for y in range(y0, y1):
        row = (y * width + x0) * 3
        for offset in range((x1 - x0) * 3):
            delta = abs(current[row + offset] - previous[row + offset])
            total_abs_delta += delta
            max_channel_delta = max(max_channel_delta, delta)
        for x in range(x0, x1):
            index = (y * width + x) * 3
            if current[index:index + 3] != previous[index:index + 3]:
                changed_pixels += 1
    area = (x1 - x0) * (y1 - y0)
    return {
        "version": TITLE_PIXEL_EVIDENCE_VERSION,
        "status": "pass" if changed_pixels > 0 else "failed",
        "reason": "pixel_delta_in_expected_title_region" if changed_pixels > 0 else "no_pixel_delta_in_expected_title_region",
        "bbox": {**bbox, "x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0},
        "in_frame": bool(bbox.get("in_frame", False)),
        "changed_pixels": changed_pixels,
        "changed_pixel_ratio": round(changed_pixels / max(1, area), 8),
        "mean_abs_channel_delta": round(total_abs_delta / max(1, area * 3), 6),
        "max_channel_delta": max_channel_delta,
        "rendered_sha256": _file_hash(rendered),
        "baseline_sha256": _file_hash(baseline),
        "font_identity": dict((plan.get("title") or {}).get("font_identity") or {}),
        "title_role": str((plan.get("title") or {}).get("role") or ""),
        "title_text": str((plan.get("title") or {}).get("text") or ""),
        "sample_time_seconds": float((plan.get("title") or {}).get("time_offset_seconds") or 0.0),
    }


def visual_style_preview_path(cfg: Mapping[str, Any], project_id: int, filename: str) -> Path:
    token = Path(str(filename)).name
    if token != str(filename) or not token or token.startswith("."):
        raise VisualStyleError("preview_path_invalid", "invalid visual style preview filename")
    return project_dir(dict(cfg), int(project_id)) / "output" / "visual_style_previews" / token


def _preview_style_entries(*, scope: str, registry: VisualStyleRegistry | None = None) -> list[dict[str, Any]]:
    if scope not in PREVIEW_SCOPES:
        raise VisualStyleError("preview_scope_invalid", f"unknown visual preview scope: {scope}")
    entries = [item for item in (registry or VISUAL_STYLES).list(include_internal=True) if item.get("style_id") != "test_soft_panel"]
    if scope == "primary":
        return [item for item in entries if item.get("enabled_for_round1_ui", True) and item.get("preview_scope") == "primary" and item.get("public_primary") is True]
    return [item for item in entries if item.get("enabled_for_round1_ui", True)]


def _primary_preview_complete(styles: list[dict[str, Any]], variants: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    missing: list[str] = []
    for style in styles:
        style_id = str(style.get("style_id") or "")
        usable = any(
            str((item.get("visual_style") or {}).get("visual_style_id") or "") == style_id
            and str(item.get("preview_kind") or "static") == "static"
            and str(item.get("title_role") or "") == "chapter_title"
            and bool(str(item.get("file") or "").strip())
            and bool(str(item.get("preview_plan_hash") or "").strip())
            and bool(str(item.get("preview_variant_id") or "").strip())
            and isinstance(item.get("title_render_evidence"), Mapping)
            and str((item.get("title_render_evidence") or {}).get("version") or "") == TITLE_PIXEL_EVIDENCE_VERSION
            and (item.get("title_render_evidence") or {}).get("status") == "pass"
            and int((item.get("title_render_evidence") or {}).get("changed_pixels") or 0) > 0
            and bool((item.get("title_render_evidence") or {}).get("in_frame"))
            and Path(str(item.get("file") or "")).is_file()
            for item in variants
        )
        if not usable:
            missing.append(style_id)
    return not missing, missing


def preview_visual_styles(
    cfg: Mapping[str, Any],
    db: Path,
    project_id: int,
    *,
    force: bool = False,
    overrides: Mapping[str, Any] | None = None,
    scope: str = "primary",
    registry: VisualStyleRegistry | None = None,
) -> dict[str, Any]:
    """Generate registry-scoped true-frame variants after Creative Brief approval."""
    if scope not in PREVIEW_SCOPES:
        raise VisualStyleError("preview_scope_invalid", f"unknown visual preview scope: {scope}")
    brief = _load_brief(db, project_id)
    state = ensure_visual_style_state(cfg, db, project_id)
    if not brief or brief.get("status") != "approved":
        return {"ok": False, "status": "needs_confirmation", "code": "creative_brief_required", "recommendation": state.get("recommendation") or {}, "visual_style": visual_style_api_payload(state)}
    sources = _source_provenance(db, project_id)
    representative_frames = _select_representative_frames(cfg, sources)
    if not representative_frames:
        raise VisualStyleError("preview_source_missing", "找不到可用的 approved source")
    variants: list[dict[str, Any]] = []
    from .color_consistency import effective_color_settings, load_project_color_state
    color_settings = effective_color_settings(load_project_color_state(dict(cfg), project_id))
    styles = _preview_style_entries(scope=scope, registry=registry)
    frames = representative_frames[:1] if scope == "primary" else representative_frames
    roles = (("chapter_title", "Chapter"),) if scope == "primary" else (("chapter_title", "Chapter"), ("location_title", "Location / Lower Third"))
    for frame in frames:
        source = frame["source"]
        timestamp = float(frame["timestamp_seconds"])
        for style in styles:
            for title_role, role_label in roles:
                role_text = "咖啡日記 / Coffee Diary" if title_role == "chapter_title" else "台北 · Coffee Shop"
                snapshot = materialize_visual_style(str(style["style_id"]), brief, color_settings=color_settings, title_role=title_role, overrides=overrides, registry=registry or VISUAL_STYLES)
                preview_plan = resolve_visual_render_plan(snapshot, width=int((snapshot.get("output") or {}).get("width") or 1920), height=int((snapshot.get("output") or {}).get("height") or 1080), title_text=role_text, color_settings=color_settings, source_geometry=frame.get("display_geometry") if isinstance(frame.get("display_geometry"), Mapping) else None, title_role=title_role)
                title_style_identity = _hash(snapshot.get("title_style") or {})
                token = _hash({"preview_render_contract": VISUAL_PREVIEW_RENDER_CONTRACT_VERSION, "source": source, "timestamp": timestamp, "style": snapshot["semantic_hash"], "plan": preview_plan["semantic_hash"], "role": title_role, "title_text": role_text, "title_style_identity": title_style_identity, "overrides": snapshot.get("overrides"), "creative_brief_hash": brief.get("visual_contract_hash"), "frame": frame["selection_reason"]})[:20]
                output = visual_style_preview_path(cfg, project_id, f"{style['style_id']}-{title_role}-{token}.png")
                preview_plan_hash = str(preview_plan.get("semantic_hash") or preview_plan.get("resolved_hash") or "")
                preview_variant_id = _hash({"preview_render_contract": VISUAL_PREVIEW_RENDER_CONTRACT_VERSION, "project_id": project_id, "style": snapshot.get("semantic_hash"), "plan": preview_plan_hash, "source": source.get("project_media_uuid"), "timestamp": timestamp, "role": title_role, "title_text": role_text, "kind": "static", "overrides": snapshot.get("overrides"), "frame": frame.get("selection_reason")})[:32]
                if force:
                    output.unlink(missing_ok=True)
                if output.is_file() and output.stat().st_size > 0:
                    with connect(db) as con:
                        evidence_row = con.execute("select title_render_evidence_json from visual_style_preview_evidence where project_id=? and preview_variant_id=?", (int(project_id), preview_variant_id)).fetchone()
                    title_render_evidence = {}
                    if evidence_row:
                        try:
                            title_render_evidence = json.loads(evidence_row["title_render_evidence_json"] or "{}")
                        except (TypeError, ValueError):
                            title_render_evidence = {}
                    item = {"file": str(output), "sha256": _file_hash(output), "cache_hit": True, "visual_style": snapshot, "source": _public_source(source), "timestamp_seconds": timestamp, "title_role": title_role, "role_label": role_label, "preview_kind": "static", "title_render_evidence": title_render_evidence}
                else:
                    item = render_true_frame_preview(cfg, project_id, Path(str(source["path"])), timestamp, snapshot, output, color_settings=color_settings, source_display_ratio=float((frame.get("display_geometry") or {}).get("display_ratio") or 0.0) or None, source_geometry=frame.get("display_geometry") if isinstance(frame.get("display_geometry"), Mapping) else None, title_text=role_text, title_role=title_role)
                    item.update({"cache_hit": False, "visual_style": snapshot, "source": _public_source(source), "title_role": title_role, "role_label": role_label, "preview_kind": "static"})
                item["preview_plan_hash"] = preview_plan_hash
                item["technical_transform"] = dict(preview_plan.get("technical_transform") or {})
                item["title_bbox"] = dict((preview_plan.get("title") or {}).get("bbox") or {})
                item["font_resolution"] = {"path": str((preview_plan.get("title") or {}).get("font_path") or ""), **dict((preview_plan.get("title") or {}).get("font_identity") or {})}
                item["representative_frame"] = {**{key: value for key, value in frame.items() if key != "source"}, "title_role": title_role, "role_label": role_label, "preview_kind": "static"}
                item["preview_variant_id"] = preview_variant_id
                item["title_style_identity"] = title_style_identity
                item["preview_render_contract_version"] = VISUAL_PREVIEW_RENDER_CONTRACT_VERSION
                item["creative_brief_hash"] = str(brief.get("visual_contract_hash") or "")
                item["source_media_uuid"] = str(source.get("project_media_uuid") or "")
                item["source_fingerprint"] = dict(source.get("fingerprint") or {})
                item["generated_at"] = _now()
                _persist_preview_evidence(db, project_id, state, item, snapshot)
                variants.append(item)
                # Animated, location, dark/complex and standalone evidence is
                # extended-only so the primary CTA returns the first-look
                # comparison without waiting for the diagnostic matrix.
                if scope == "extended" and frame is representative_frames[0] and title_role == "chapter_title":
                    animated_plan = resolve_visual_render_plan(snapshot, width=int((snapshot.get("output") or {}).get("width") or 1920), height=int((snapshot.get("output") or {}).get("height") or 1080), title_text=role_text, color_settings=color_settings, source_geometry=frame.get("display_geometry") if isinstance(frame.get("display_geometry"), Mapping) else None, title_role=title_role, title_duration_seconds=2.0)
                    animated_token = _hash({"preview_render_contract": VISUAL_PREVIEW_RENDER_CONTRACT_VERSION, "source": source, "timestamp": timestamp, "style": snapshot["semantic_hash"], "plan": animated_plan["semantic_hash"], "role": title_role, "title_text": role_text, "kind": "animated", "overrides": snapshot.get("overrides")})[:20]
                    animated_output = visual_style_preview_path(cfg, project_id, f"{style['style_id']}-{title_role}-{animated_token}.mp4")
                    if force:
                        animated_output.unlink(missing_ok=True)
                    if animated_output.is_file() and animated_output.stat().st_size > 0:
                        animated = {"file": str(animated_output), "sha256": _file_hash(animated_output), "cache_hit": True, "visual_style": snapshot, "source": _public_source(source), "timestamp_seconds": timestamp, "title_role": title_role, "role_label": role_label, "preview_kind": "animated", "duration_seconds": 2.0}
                    else:
                        animated = render_animated_title_preview(cfg, project_id, Path(str(source["path"])), timestamp, snapshot, animated_output, color_settings=color_settings, source_geometry=frame.get("display_geometry") if isinstance(frame.get("display_geometry"), Mapping) else None, title_text=role_text, title_role=title_role, duration_seconds=2.0)
                        animated.update({"cache_hit": False, "visual_style": snapshot, "source": _public_source(source), "title_role": title_role, "role_label": role_label})
                    animated["preview_plan_hash"] = str(animated_plan.get("semantic_hash") or animated_plan.get("resolved_hash") or "")
                    animated["technical_transform"] = dict(animated_plan.get("technical_transform") or {})
                    animated["representative_frame"] = {**{key: value for key, value in frame.items() if key != "source"}, "title_role": title_role, "role_label": role_label, "preview_kind": "animated", "duration_seconds": 2.0}
                    animated["preview_variant_id"] = _hash({"preview_render_contract": VISUAL_PREVIEW_RENDER_CONTRACT_VERSION, "project_id": project_id, "style": snapshot.get("semantic_hash"), "plan": animated["preview_plan_hash"], "source": source.get("project_media_uuid"), "timestamp": timestamp, "role": title_role, "title_text": role_text, "kind": "animated", "overrides": snapshot.get("overrides")})[:32]
                    animated["title_style_identity"] = _hash(snapshot.get("title_style") or {})
                    animated["creative_brief_hash"] = str(brief.get("visual_contract_hash") or "")
                    animated["source_media_uuid"] = str(source.get("project_media_uuid") or "")
                    animated["source_fingerprint"] = dict(source.get("fingerprint") or {})
                    animated["generated_at"] = _now()
                    _persist_preview_evidence(db, project_id, state, animated, snapshot)
                    variants.append(animated)
    if scope == "primary":
        complete, missing = _primary_preview_complete(styles, variants)
        if not complete:
            return {"ok": False, "status": "needs_confirmation", "code": "preview_primary_incomplete", "missing_style_ids": missing, "preview_scope": scope, "preview_scope_version": VISUAL_PREVIEW_SCOPE_VERSION, "variants": variants}
    return {"ok": True, "status": "ready", "preview_scope": scope, "preview_scope_version": VISUAL_PREVIEW_SCOPE_VERSION, "preview_revision": int(state.get("preview_revision") or 0), "source": _public_source(frames[0]["source"]), "representative_frames": [{key: value for key, value in frame.items() if key != "source"} for frame in frames], "variants": variants}


def _persist_preview_evidence(db: Path, project_id: int, state: Mapping[str, Any], item: Mapping[str, Any], snapshot: Mapping[str, Any]) -> None:
    with connect(db) as con:
        con.execute(
            "insert or replace into visual_style_preview_evidence(preview_variant_id, project_id, preview_revision, preview_plan_hash, visual_style_id, visual_style_version, visual_style_hash, title_style_identity, creative_brief_hash, technical_transform_json, source_media_uuid, source_fingerprint_json, timestamp_seconds, representative_frame_json, title_render_evidence_json, preview_filename, preview_image_sha256, generated_at) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(item["preview_variant_id"]), int(project_id), int(state.get("preview_revision") or 0), str(item["preview_plan_hash"]), str(snapshot.get("visual_style_id") or ""), str(snapshot.get("visual_style_version") or ""), str(snapshot.get("resolved_hash") or ""), str(item["title_style_identity"]), str(item["creative_brief_hash"]), json.dumps(item.get("technical_transform") or {}, ensure_ascii=False, sort_keys=True), str(item["source_media_uuid"]), json.dumps(item.get("source_fingerprint") or {}, ensure_ascii=False, sort_keys=True), float(item.get("timestamp_seconds") or 0), json.dumps(item.get("representative_frame") or {}, ensure_ascii=False, sort_keys=True), json.dumps(item.get("title_render_evidence") or {}, ensure_ascii=False, sort_keys=True), Path(str(item.get("file") or "")).name, str(item["sha256"]), str(item["generated_at"]),
            ),
        )


def _select_representative_frames(cfg: Mapping[str, Any], sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select stable bright and dark/complex frames from real project media."""
    candidates: list[dict[str, Any]] = []
    for source in sources:
        path = Path(str(source.get("path") or ""))
        if not path.is_file():
            continue
        if not source.get("duration_seconds") or not source.get("display_geometry"):
            try:
                probe = probe_media_metadata(str(cfg.get("ffprobe_path") or "ffprobe"), path)
                source = {**source, "duration_seconds": probe.duration_seconds, "display_geometry": {
                    "coded_width": probe.coded_width or probe.width,
                    "coded_height": probe.coded_height or probe.height,
                    "sample_aspect_ratio": probe.sample_aspect_ratio,
                    "display_aspect_ratio": probe.display_aspect_ratio,
                    "display_ratio": probe.display_ratio,
                    "rotation_degrees": probe.rotation_degrees,
                    "display_matrix": probe.display_matrix,
                }}
            except Exception:
                continue
        # Avoid a fixed first-frame bias.  The endpoints are only candidate
        # timestamps; luma is measured from the real media and selection is
        # deterministic across reruns.
        duration = float(source.get("duration_seconds") or 0.0)
        timestamps = [max(0.0, duration * 0.2), max(0.0, duration * 0.7)] if duration > 0 else [0.5, 1.5]
        for timestamp in timestamps:
            metric = _measure_source_luma(str(cfg.get("ffmpeg_path") or "ffmpeg"), path, timestamp)
            if metric is None:
                continue
            candidates.append({"source": source, "timestamp_seconds": round(timestamp, 6), "luma": round(metric, 6), "display_geometry": dict(source.get("display_geometry") or {}), "selection_reason": "bright_high_luma_candidate" if metric >= 0.5 else "dark_complex_low_luma_candidate"})
    if not candidates:
        return []
    bright = max(candidates, key=lambda item: (float(item["luma"]), str(item["source"].get("project_media_uuid") or ""), -float(item["timestamp_seconds"])))
    dark_pool = [item for item in candidates if item is not bright]
    dark = min(dark_pool or candidates, key=lambda item: (float(item["luma"]), str(item["source"].get("project_media_uuid") or ""), float(item["timestamp_seconds"])))
    bright["selection_reason"] = "bright_high_luma_representative"
    dark["selection_reason"] = "dark_complex_low_luma_representative"
    result = [bright]
    if dark is not bright:
        result.append(dark)
    return result


def _measure_source_luma(ffmpeg_path: str, source: Path, timestamp: float) -> float | None:
    command = [ffmpeg_path, "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", f"{timestamp:.6f}", "-i", str(source), "-vf", "scale=32:32,format=gray", "-frames:v", "1", "-f", "rawvideo", "-"]
    try:
        result = subprocess.run(command, capture_output=True, check=False, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    return sum(result.stdout) / (255.0 * len(result.stdout))


def _DEFAULT_VISUAL_STYLE_DATA() -> dict[str, dict[str, Any]]:
    return {
        "diary_natural": {"version": "1", "label": "Diary Natural", "composition": "overlay", "default_title_style_id": "diary_natural_overlay", "preview_scope": "primary", "public_primary": True, "palette": {"text_primary": "#FFF8EE", "text_secondary": "#E8DED0", "accent": "#E1A46A", "surface_overlay": "#241B14CC", "surface_overlay_strong": "#17110DEE", "shadow": "#00000099"}, "grading": {"look_id": "diary-warm-neutral", "look_version": "1", "source_colorspace": "bt709", "brightness": 0.015, "contrast": 1.03, "saturation": 1.04}, "supported_aspects": ["landscape", "portrait"], "enabled_for_round1_ui": True, "required_capabilities": {}},
        "clean_minimal": {"version": "1", "label": "Clean Minimal", "composition": "overlay", "default_title_style_id": "clean_minimal_overlay", "preview_scope": "primary", "public_primary": True, "palette": {"text_primary": "#FFFFFF", "text_secondary": "#E7E7E7", "accent": "#9ED8FF", "surface_overlay": "#111111B8", "surface_overlay_strong": "#111111DD", "shadow": "#00000080"}, "grading": {"look_id": "clean-neutral", "look_version": "1", "source_colorspace": "bt709", "brightness": 0.0, "contrast": 1.0, "saturation": 0.96}, "supported_aspects": ["landscape", "portrait"], "enabled_for_round1_ui": True, "required_capabilities": {}},
        "cinematic": {"version": "1", "label": "Cinematic", "composition": "overlay", "default_title_style_id": "cinematic_overlay", "preview_scope": "primary", "public_primary": True, "palette": {"text_primary": "#FFF7DD", "text_secondary": "#E0D4B8", "accent": "#D6A85C", "surface_overlay": "#0B1820C7", "surface_overlay_strong": "#071016E6", "shadow": "#000000AA"}, "grading": {"look_id": "cinematic-teal-gold", "look_version": "1", "source_colorspace": "bt709", "brightness": -0.015, "contrast": 1.08, "saturation": 1.08}, "supported_aspects": ["landscape", "portrait"], "enabled_for_round1_ui": True, "required_capabilities": {}},
        "standalone_card_compare": {"version": "1", "label": "Standalone Card Compare", "composition": "standalone", "default_title_style_id": "standalone_card", "preview_scope": "extended", "public_primary": False, "palette": {"text_primary": "#FFF8EE", "text_secondary": "#D9D9D9", "accent": "#E1A46A", "surface_overlay": "#20242AFF", "surface_overlay_strong": "#20242AFF", "shadow": "#000000AA"}, "grading": {"look_id": "card-neutral", "look_version": "1", "source_colorspace": "bt709", "brightness": 0.0, "contrast": 1.0, "saturation": 1.0}, "supported_aspects": ["landscape", "portrait"], "enabled_for_round1_ui": True, "required_capabilities": {}},
        "test_soft_panel": {"version": "1", "label": "Test Soft Panel", "composition": "overlay", "default_title_style_id": "test_soft_panel", "preview_scope": "extended", "public_primary": False, "palette": {"text_primary": "#FFFFFF", "text_secondary": "#E8F4FF", "accent": "#7BDFF2", "surface_overlay": "#153047CC", "surface_overlay_strong": "#102033E6", "shadow": "#00000088"}, "grading": {"look_id": "test-soft-panel", "look_version": "1", "source_colorspace": "bt709", "brightness": 0.02, "contrast": 1.01, "saturation": 1.02}, "supported_aspects": ["landscape", "portrait"], "enabled_for_round1_ui": False, "required_capabilities": {}}
    }


def _DEFAULT_TITLE_STYLES_DATA() -> dict[str, dict[str, Any]]:
    roles = ["chapter_title", "section_title", "location_title", "date_time_title", "lower_third", "caption_subtitle"]
    base = {"supported_roles": roles, "font_family": "system-sans", "fallback_chain": ["Noto Sans CJK TC", "Noto Sans CJK JP", "Segoe UI", "Arial", "sans-serif"], "weight": 600, "line_height": 1.18, "letter_spacing": 0.0, "alignment": "left", "max_width_ratio": 0.78, "safe_zone": {"left": 0.05, "right": 0.05, "top": 0.06, "bottom": 0.08}, "readability": {"shadow": True, "outline": 0, "surface": "translucent"}, "motion": {"preset": "fade", "enter_seconds": 0.28, "exit_seconds": 0.22, "easing": "ease-out"}, "responsive": {"landscape": {"anchor": "bottom-left", "size_ratio": 0.052}, "portrait": {"anchor": "bottom-left", "size_ratio": 0.046}}}
    def item(style_id: str, label: str, accent: str, weight: int, motion: str) -> dict[str, Any]:
        value = deepcopy(base)
        value.update({"title_style_id": style_id, "version": "1", "label": label, "text_color_token": "text_primary", "accent_color": accent, "weight": weight})
        value["role_tokens"] = {
            "chapter_title": {"max_width_ratio": 0.78, "responsive": deepcopy(value["responsive"])},
            "section_title": {"max_width_ratio": 0.72, "responsive": deepcopy(value["responsive"])},
            "location_title": {"max_width_ratio": 0.68, "responsive": {"landscape": {"anchor": "top-left", "size_ratio": 0.038}, "portrait": {"anchor": "top-left", "size_ratio": 0.034}}},
            "date_time_title": {"max_width_ratio": 0.62, "responsive": {"landscape": {"anchor": "top-right", "size_ratio": 0.032}, "portrait": {"anchor": "top-right", "size_ratio": 0.03}}},
            "lower_third": {"max_width_ratio": 0.58, "responsive": {"landscape": {"anchor": "bottom-left", "size_ratio": 0.034}, "portrait": {"anchor": "bottom-left", "size_ratio": 0.03}}},
            "caption_subtitle": {"max_width_ratio": 0.86, "responsive": {"landscape": {"anchor": "bottom-center", "size_ratio": 0.03}, "portrait": {"anchor": "bottom-center", "size_ratio": 0.028}}},
        }
        value["motion"] = {**value["motion"], "preset": motion}
        return value
    return {
        "diary_natural_overlay": item("diary_natural_overlay", "Diary Natural Overlay", "accent", 500, "fade"),
        "clean_minimal_overlay": item("clean_minimal_overlay", "Clean Minimal Overlay", "accent", 600, "none"),
        "cinematic_overlay": item("cinematic_overlay", "Cinematic Overlay", "accent", 650, "fade_rise"),
        "standalone_card": item("standalone_card", "Standalone Card", "accent", 600, "fade"),
        "test_soft_panel": item("test_soft_panel", "Test Soft Panel", "accent", 600, "slide_fade"),
    }


_DEFAULT_VISUAL_STYLES = _DEFAULT_VISUAL_STYLE_DATA()
_DEFAULT_TITLE_STYLES = _DEFAULT_TITLE_STYLES_DATA()
VISUAL_STYLES = VisualStyleRegistry()
TITLE_STYLES = TitleStyleRegistry()


def _validate_style_definition(item: Mapping[str, Any]) -> None:
    for field in ("style_id", "version", "label", "composition", "default_title_style_id", "palette", "grading", "supported_aspects"):
        if not item.get(field):
            raise VisualStyleError("style_contract_invalid", f"style missing {field}")
    if item["composition"] not in {"overlay", "standalone"}:
        raise VisualStyleError("style_contract_invalid", "unknown visual composition")
    if set(item["palette"]) < {"text_primary", "text_secondary", "accent", "surface_overlay", "surface_overlay_strong", "shadow"}:
        raise VisualStyleError("style_contract_invalid", "palette tokens incomplete")


def _validate_title_definition(item: Mapping[str, Any]) -> None:
    for field in ("title_style_id", "version", "label", "supported_roles", "fallback_chain", "weight", "safe_zone", "readability", "motion", "responsive"):
        if not item.get(field):
            raise VisualStyleError("title_style_contract_invalid", f"title style missing {field}")


_TITLE_STYLE_INHERITANCE_FIELDS = {
    "title_style_id", "version", "extends", "overrides", "label", "supported_roles",
    "font_family", "fallback_chain", "weight", "line_height", "letter_spacing", "alignment",
    "max_width_ratio", "safe_zone", "readability", "motion", "responsive", "role_tokens",
    "text_color_token", "accent_color",
}


def _validate_title_inheritance_definition(item: Mapping[str, Any]) -> None:
    for field in ("title_style_id", "version", "extends"):
        if not item.get(field):
            raise VisualStyleError("title_style_inheritance_invalid", f"inherited title style missing {field}")
    parent = item.get("extends")
    if not isinstance(parent, Mapping) or not str(parent.get("title_style_id") or parent.get("id") or "") or not str(parent.get("version") or ""):
        raise VisualStyleError("title_style_inheritance_invalid", "inherited title style parent requires exact id/version")
    unknown = sorted(set(item) - _TITLE_STYLE_INHERITANCE_FIELDS)
    if unknown:
        raise VisualStyleError("title_style_inheritance_invalid", "unknown inherited title override: " + ", ".join(unknown))
    overrides = item.get("overrides")
    if overrides is not None and not isinstance(overrides, Mapping):
        raise VisualStyleError("title_style_inheritance_invalid", "title style overrides must be an object")
    if isinstance(overrides, Mapping):
        unknown_overrides = sorted(set(overrides) - (_TITLE_STYLE_INHERITANCE_FIELDS - {"title_style_id", "version", "extends", "overrides", "label"}))
        if unknown_overrides:
            raise VisualStyleError("title_style_inheritance_invalid", "unknown inherited title override: " + ", ".join(unknown_overrides))


def _resolve_title_role_contract(title_style: Mapping[str, Any], role: str, aspect: str) -> dict[str, Any]:
    """Materialize one role exactly once from the immutable title contract.

    The marker is semantic evidence, not a comparison with the original role.
    This lets a materialized snapshot be safely reused for another preview role
    while preventing the same role token from being applied twice.
    """

    role = str(role or "chapter_title")
    result = deepcopy(dict(title_style))
    if role not in (result.get("supported_roles") or []):
        raise VisualStyleError("title_role_unsupported", f"title style does not support role {role}")
    if str(result.get("role_materialized_for") or "") != role:
        role_tokens = result.get("role_tokens") if isinstance(result.get("role_tokens"), Mapping) else {}
        token = role_tokens.get(role) if isinstance(role_tokens, Mapping) else None
        if token is not None and not isinstance(token, Mapping):
            raise VisualStyleError("title_role_contract_invalid", f"role token must be an object: {role}")
        if isinstance(token, Mapping):
            token = deepcopy(dict(token))
            token_responsive = token.get("responsive")
            if isinstance(token_responsive, Mapping):
                if isinstance(token_responsive.get(aspect), Mapping):
                    token["responsive"] = deepcopy(token_responsive[aspect])
                elif "anchor" not in token_responsive:
                    token["responsive"] = deepcopy(token_responsive.get("landscape") or {})
            result = _deep_merge(result, token)
            if isinstance(token.get("responsive"), Mapping):
                result["responsive"] = deepcopy(token["responsive"])
        explicit = result.get("_inherited_explicit_overrides")
        if isinstance(explicit, Mapping):
            result = _deep_merge(result, explicit)
        result["role_materialized_for"] = role
    responsive = result.get("responsive") or {}
    if isinstance(responsive, Mapping):
        if isinstance(responsive.get(aspect), Mapping):
            result["responsive"] = deepcopy(responsive[aspect])
        elif "anchor" not in responsive:
            result["responsive"] = deepcopy(responsive.get("landscape") or {})
    result["role"] = role
    return result


def _approved_framing(brief: Mapping[str, Any]) -> dict[str, Any]:
    output = brief.get("output") or {}
    target = str(output.get("orientation") or "landscape")
    direction_id = "portrait_source_in_landscape" if target == "landscape" else "landscape_source_in_portrait"
    all_directions = brief.get("framing_intent") if isinstance(brief.get("framing_intent"), Mapping) else {}
    direction = dict(all_directions.get(direction_id) or {})
    strategy = str(direction.get("approved_strategy_id") or direction.get("approved_strategy") or "preserve_full_frame")
    if strategy not in {"auto_recommended", "crop_reframe", "background_treatment", "preserve_full_frame"}:
        raise VisualStyleError("framing_unknown", f"unknown approved framing strategy: {strategy}")
    if strategy == "auto_recommended":
        strategy = "crop_reframe"
    policy_matrix = {}
    for key, value in all_directions.items():
        if not isinstance(value, Mapping):
            continue
        policy_matrix[str(key)] = {
            "direction_id": str(value.get("direction_id") or key),
            "source_orientation": str(value.get("source_orientation") or "unknown"),
            "target_orientation": str(value.get("target_orientation") or "unknown"),
            "strategy_id": str(value.get("approved_strategy_id") or value.get("approved_strategy") or "preserve_full_frame"),
            "strategy_version": str(value.get("approved_strategy_version") or "1"),
            "resolved_semantic": deepcopy(value.get("resolved_semantic") or {}),
        }
    return {"direction_id": direction_id, "source_orientation": direction.get("source_orientation") or "unknown", "target_orientation": target, "strategy_id": strategy, "strategy_version": str(direction.get("approved_strategy_version") or "1"), "resolved_semantic": deepcopy(direction.get("resolved_semantic") or {}), "policy_matrix": policy_matrix}


def _load_brief(db: Path, project_id: int) -> dict[str, Any] | None:
    from .creative_brief import load_creative_brief, creative_brief_api_payload
    value = load_creative_brief(db, project_id)
    return creative_brief_api_payload(value) if value else None


def _approved_contract(brief: Mapping[str, Any]) -> dict[str, Any]:
    approved = dict(brief.get("approved") or {})
    approved["status"] = str(brief.get("status") or "")
    approved["brief_version"] = int(brief.get("brief_version") or 1)
    approved["visual_contract_hash"] = str(brief.get("visual_contract_hash") or "")
    return approved


def _technical_transform_snapshot(color_settings: Mapping[str, Any] | None) -> dict[str, Any]:
    data = dict(color_settings or {})
    mode = str(data.get("mode") or "none")
    identity: dict[str, Any] = {}
    if mode in {"dji_lut", "dji_dlog", "dji_dlog_m"}:
        lut = validate_lut_resource(data)
        identity = {"sha256": _file_hash(lut), "size": lut.stat().st_size}
    return {
        **color_mode_contract(mode),
        "source_colorspace": str(data.get("source_colorspace") or "unknown"),
        "lut_identity": identity,
        "applied_once": mode in {"dji_lut", "dji_dlog", "dji_dlog_m"},
    }


def _state_row(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for target, source in (("recommendation", "recommendation_json"), ("approved", "approved_json"), ("source_provenance", "source_provenance_json")):
        try:
            result[target] = json.loads(result.get(source) or "{}")
        except (TypeError, ValueError):
            result[target] = {}
    return result


def _source_provenance(db: Path, project_id: int) -> list[dict[str, Any]]:
    with connect(db) as con:
        rows = con.execute("select pv.project_media_uuid, pv.video_id, pv.source_fingerprint_json, v.current_path from project_videos pv join videos v on v.id=pv.video_id where pv.project_id=? order by pv.sort_order, pv.video_id", (int(project_id),)).fetchall()
    result = []
    for row in rows:
        path = Path(str(row["current_path"] or ""))
        persisted = parse_source_fingerprint(str(row["source_fingerprint_json"] or "{}"))
        if not path.is_file() or not persisted:
            raise VisualStyleError("source_changed", f"source fingerprint changed or source missing: {path}")
        try:
            revalidation = revalidate_and_rebind_project_source_fingerprint(
                db,
                project_id,
                str(row["project_media_uuid"] or ""),
                path,
                persisted,
            )
        except SourceFingerprintChangedError as exc:
            raise VisualStyleError("source_changed", f"source fingerprint changed: {path} ({exc.reason})") from exc
        fingerprint = dict(revalidation["fingerprint"])
        result.append({"project_media_uuid": str(row["project_media_uuid"] or ""), "video_id": int(row["video_id"]), "path": str(path), "fingerprint": fingerprint, "source_revalidation": {"status": revalidation["status"], "content_equal": bool(revalidation["content_equal"]), "rebound": bool(revalidation["rebound"]), "full_hash": bool(revalidation["full_hash"])}})
    return result


def _public_source(source: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in source.items() if key != "path"}
    path = str(source.get("path") or "")
    if path:
        result["source_filename"] = Path(path).name
    return result


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _system_font_path(fallback_chain: Any = None, weight: int = 500, text: str = "") -> Path | None:
    return _resolve_font(fallback_chain, weight, text=text)["path"]


def _font_requires_cjk(text: str) -> bool:
    return any(
        any(start <= ord(char) <= end for start, end in ((0x2E80, 0x2FFF), (0x3000, 0x30FF), (0x31A0, 0x31BF), (0x3400, 0x9FFF), (0xF900, 0xFAFF)))
        for char in str(text or "")
    )


@functools.lru_cache(maxsize=64)
def _font_codepoint_ranges(path_string: str, size: int, mtime_ns: int) -> tuple[tuple[int, int], ...]:
    """Read Unicode cmap coverage without depending on a font package.

    The production contract is installed-font-only.  Parsing the TrueType
    cmap locally lets us reject a present-but-inadequate font (for example
    Segoe UI for Traditional Chinese) before FFmpeg can silently render tofu.
    The cache key includes file identity metadata so a replaced font cannot
    reuse stale coverage.
    """

    del size, mtime_ns  # They intentionally participate in the cache key.
    data = Path(path_string).read_bytes()

    def u16(offset: int) -> int:
        return struct.unpack_from(">H", data, offset)[0]

    def u32(offset: int) -> int:
        return struct.unpack_from(">I", data, offset)[0]

    face_offsets = [0]
    if data[:4] == b"ttcf":
        count = u32(8)
        face_offsets = [u32(12 + index * 4) for index in range(count)]
    ranges: list[tuple[int, int]] = []
    points: set[int] = set()

    for face_offset in face_offsets:
        if face_offset < 0 or face_offset + 12 > len(data):
            continue
        table_count = u16(face_offset + 4)
        cmap_offset = None
        cmap_length = 0
        for index in range(table_count):
            record = face_offset + 12 + index * 16
            if record + 16 > len(data):
                break
            tag = data[record:record + 4]
            table_offset = u32(record + 8)
            table_length = u32(record + 12)
            if tag == b"cmap":
                cmap_offset, cmap_length = table_offset, table_length
                break
        if cmap_offset is None or cmap_offset + 4 > len(data):
            continue
        cmap_end = min(len(data), cmap_offset + cmap_length)
        subtable_count = u16(cmap_offset + 2)
        subtables: list[tuple[int, int, int]] = []
        for index in range(subtable_count):
            record = cmap_offset + 4 + index * 8
            if record + 8 > cmap_end:
                break
            platform = u16(record)
            encoding = u16(record + 2)
            relative_offset = u32(record + 4)
            subtable = cmap_offset + relative_offset
            if platform in {0, 3} and (platform == 0 or encoding in {0, 1, 10}) and subtable + 2 <= cmap_end:
                subtables.append((platform, encoding, subtable))
        for _platform, _encoding, subtable in subtables:
            fmt = u16(subtable)
            if fmt == 4 and subtable + 16 <= cmap_end:
                length = u16(subtable + 2)
                end = min(cmap_end, subtable + length)
                seg_count = u16(subtable + 6) // 2
                end_codes = subtable + 14
                start_codes = end_codes + seg_count * 2 + 2
                deltas = start_codes + seg_count * 2
                range_offsets = deltas + seg_count * 2
                if range_offsets + seg_count * 2 > end:
                    continue
                for segment in range(seg_count):
                    start = u16(start_codes + segment * 2)
                    finish = u16(end_codes + segment * 2)
                    if start > finish or start == 0xFFFF:
                        continue
                    delta = struct.unpack_from(">h", data, deltas + segment * 2)[0]
                    range_offset = u16(range_offsets + segment * 2)
                    if range_offset == 0:
                        for codepoint in range(start, finish + 1):
                            if (codepoint + delta) & 0xFFFF:
                                points.add(codepoint)
                    else:
                        range_word = range_offsets + segment * 2
                        for codepoint in range(start, finish + 1):
                            glyph_offset = range_word + range_offset + (codepoint - start) * 2
                            if glyph_offset + 2 <= end and u16(glyph_offset) != 0:
                                points.add(codepoint)
            elif fmt in {12, 13} and subtable + 16 <= cmap_end:
                length = u32(subtable + 4)
                end = min(cmap_end, subtable + length)
                group_count = u32(subtable + 12)
                group_size = 12
                for group in range(group_count):
                    record = subtable + 16 + group * group_size
                    if record + group_size > end:
                        break
                    start = u32(record)
                    finish = u32(record + 4)
                    if start <= finish:
                        ranges.append((start, finish))

    # Compress format-4 points so the lookup remains cheap for every title
    # character while retaining exact glyph-zero behavior.
    for codepoint in sorted(points):
        if ranges and ranges[-1][1] + 1 == codepoint:
            ranges[-1] = (ranges[-1][0], codepoint)
        else:
            ranges.append((codepoint, codepoint))
    return tuple(sorted(set(ranges)))


def _font_supports_text(path: Path, text: str) -> bool:
    required = {ord(char) for char in str(text or "") if not char.isspace() and unicodedata.category(char) not in {"Cf"}}
    if not required:
        return True
    try:
        stat = path.stat()
        ranges = _font_codepoint_ranges(str(path), int(stat.st_size), int(stat.st_mtime_ns))
    except (OSError, struct.error, ValueError):
        return False
    return all(any(start <= codepoint <= finish for start, finish in ranges) for codepoint in required)


def _resolve_font(fallback_chain: Any = None, weight: int = 500, *, text: str = "") -> dict[str, Any]:
    """Resolve installed fonts and verify every non-whitespace glyph."""
    windows_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    family_names = [str(value).strip() for value in (fallback_chain if isinstance(fallback_chain, (list, tuple)) else [fallback_chain]) if str(value or "").strip()]
    if not family_names:
        family_names = ["system-sans"]
    requires_cjk = _font_requires_cjk(text)
    cjk_files = [("Microsoft JhengHei", windows_fonts / "msjh.ttc"), ("Microsoft YaHei", windows_fonts / "msyh.ttc"), ("Noto Sans CJK TC", Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")), ("Noto Sans CJK TC", Path("/usr/share/fonts/opentype/noto/NotoSansCJKtc-Regular.otf"))]
    if int(weight or 500) >= 600:
        cjk_files = [("Microsoft JhengHei", windows_fonts / "msjhbd.ttc"), ("Microsoft YaHei", windows_fonts / "msyhbd.ttc"), *cjk_files]
    normal_files = {
        "system-sans": [("Segoe UI", windows_fonts / ("segoeuib.ttf" if int(weight or 500) >= 600 else "segoeui.ttf")), ("Arial", windows_fonts / ("arialbd.ttf" if int(weight or 500) >= 600 else "arial.ttf")), ("DejaVu Sans", Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))],
        "noto sans cjk tc": cjk_files,
        "noto sans cjk jp": [("Noto Sans CJK JP", Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")), *cjk_files],
        "segoe ui": [("Segoe UI", windows_fonts / ("segoeuib.ttf" if int(weight or 500) >= 600 else "segoeui.ttf"))],
        "arial": [("Arial", windows_fonts / ("arialbd.ttf" if int(weight or 500) >= 600 else "arial.ttf"))],
        "sans-serif": [("DejaVu Sans", Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))],
    }
    candidates: list[tuple[str, Path]] = []
    for family in family_names:
        normalized = family.casefold()
        if normalized == "system-sans" and requires_cjk:
            candidates.extend(cjk_files)
        candidates.extend(normal_files.get(normalized, []))
    # Traditional Chinese Windows faces are deterministic, installed-only
    # fallbacks.  Coverage checking prevents a Latin-only face from winning.
    candidates.extend(cjk_files)
    seen: set[str] = set()
    for index, (resolved_family, path) in enumerate(candidates):
        if str(path) in seen:
            continue
        seen.add(str(path))
        if path.is_file() and _font_supports_text(path, text):
            return {"path": path, "resolved_family": resolved_family, "resolved_weight": int(weight or 500), "fallback_index": index, "reason": "preferred_installed" if index == 0 else "fallback_installed", "sha256": _file_hash(path), "coverage_checked": True, "coverage_contract": "font-cmap-v1"}
    return {"path": None, "resolved_family": "unresolved", "resolved_weight": int(weight or 500), "fallback_index": -1, "reason": "no_allowed_installed_font_with_required_glyphs", "sha256": "", "coverage_checked": True, "coverage_contract": "font-cmap-v1"}


def _deep_merge(parent: Mapping[str, Any], child: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(parent))
    for key, value in child.items():
        if isinstance(result.get(key), Mapping) and isinstance(value, Mapping):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _semantic_hash(snapshot: Mapping[str, Any]) -> str:
    return _hash({key: value for key, value in snapshot.items() if key not in {"resolved_hash", "semantic_hash", *_APPROVAL_FIELDS}})


def _normalize_visual_overrides(overrides: Mapping[str, Any] | None) -> dict[str, Any]:
    if not overrides:
        return {}
    raw = dict(overrides)
    allowed = {"title_style_id", "title_style_version", "title_role", "font_family", "fallback_chain", "weight", "size_preset", "palette_variant", "accent_token", "composition", "anchor", "readability", "motion"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise VisualStyleError("visual_override_unknown", "unknown visual override: " + ", ".join(unknown))
    result = {key: deepcopy(value) for key, value in raw.items()}
    if "weight" in result and int(result["weight"]) not in TITLE_WEIGHT_VALUES:
        raise VisualStyleError("visual_override_invalid", "unsupported title weight")
    if "size_preset" in result and str(result["size_preset"]) not in TITLE_SIZE_PRESETS:
        raise VisualStyleError("visual_override_invalid", "unsupported title size preset")
    if "anchor" in result and str(result["anchor"]) not in TITLE_ANCHORS:
        raise VisualStyleError("title_anchor_unsupported", f"unsupported title anchor: {result['anchor']}")
    if "font_family" in result and str(result["font_family"]) not in TITLE_FONT_FAMILIES:
        raise VisualStyleError("visual_override_invalid", "unsupported title font family")
    if "composition" in result and str(result["composition"]) not in {"overlay", "standalone"}:
        raise VisualStyleError("visual_override_invalid", "unsupported composition")
    if "motion" in result:
        motion = dict(result["motion"] or {})
        if str(motion.get("preset") or "none") not in TITLE_MOTION_PRESETS:
            raise VisualStyleError("title_motion_unsupported", "unsupported title motion preset")
        if str(motion.get("easing") or "ease-out") not in {"linear", "ease-out"}:
            raise VisualStyleError("title_easing_unsupported", "unsupported title easing")
        result["motion"] = motion
    if "readability" in result:
        readability = dict(result["readability"] or {})
        if str(readability.get("surface") or "translucent") not in {"none", "translucent", "solid"}:
            raise VisualStyleError("visual_override_invalid", "unsupported readability surface")
        result["readability"] = readability
    return result


def _apply_title_overrides(title: Mapping[str, Any], overrides: Mapping[str, Any], aspect: str) -> dict[str, Any]:
    result = deepcopy(dict(title))
    if "font_family" in overrides:
        result["font_family"] = str(overrides["font_family"])
    if "fallback_chain" in overrides:
        chain = overrides["fallback_chain"]
        if not isinstance(chain, list) or not chain or len(chain) > 5 or not all(isinstance(item, str) and item.strip() for item in chain):
            raise VisualStyleError("visual_override_invalid", "fallback_chain must be 1-5 non-empty family names")
        result["fallback_chain"] = list(chain)
    if "weight" in overrides:
        result["weight"] = int(overrides["weight"])
    if "readability" in overrides:
        result["readability"] = _deep_merge(result.get("readability") or {}, overrides["readability"])
    if "motion" in overrides:
        result["motion"] = _deep_merge(result.get("motion") or {}, overrides["motion"])
    if "anchor" in overrides:
        responsive = deepcopy(result.get("responsive") or {})
        current = dict(responsive.get(aspect) or responsive.get("landscape") or responsive)
        current["anchor"] = str(overrides["anchor"])
        if aspect in {"landscape", "portrait"}:
            responsive[aspect] = current
        else:
            responsive = current
        result["responsive"] = responsive
    if "size_preset" in overrides:
        scale = TITLE_SIZE_PRESETS[str(overrides["size_preset"])]
        responsive = deepcopy(result.get("responsive") or {})
        if "anchor" in responsive:
            responsive["size_ratio"] = float(responsive.get("size_ratio") or 0.05) * scale
        else:
            for key, value in responsive.items():
                if isinstance(value, Mapping):
                    value["size_ratio"] = float(value.get("size_ratio") or 0.05) * scale
        result["responsive"] = responsive
    return result


def _apply_palette_override(palette: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(palette))
    variant = str(overrides.get("palette_variant") or "default")
    if variant == "high_contrast":
        result.update({"text_primary": "#FFFFFF", "text_secondary": "#FFFFFF", "shadow": "#000000CC", "surface_overlay": "#000000CC"})
    elif variant not in {"default", "muted"}:
        raise VisualStyleError("visual_override_invalid", "unsupported palette variant")
    if variant == "muted":
        result["accent"] = "#C7C7C7"
    accent_token = str(overrides.get("accent_token") or "")
    if accent_token:
        if accent_token not in result:
            raise VisualStyleError("visual_override_invalid", "unknown accent token")
        result["accent"] = result[accent_token]
    return result


def _escape_filter_path(path: Path | None) -> str:
    if path is None:
        return ""
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "TITLE_ANCHORS", "TITLE_MOTION_PRESETS", "TITLE_STYLES", "TITLE_STYLE_REGISTRY_VERSION", "TITLE_STYLE_SCHEMA_VERSION", "VISUAL_STYLES", "VISUAL_STYLE_REGISTRY_VERSION", "VISUAL_STYLE_SCHEMA_VERSION", "VISUAL_RENDER_CONTRACT_VERSION", "VISUAL_PREVIEW_RENDER_CONTRACT_VERSION", "TITLE_PIXEL_EVIDENCE_VERSION", "VisualStyleError", "build_preview_filter", "ensure_visual_style_state", "load_visual_style_state", "materialize_visual_graph", "materialize_visual_style", "preview_visual_styles", "render_animated_title_preview", "render_true_frame_preview", "resolve_visual_render_plan", "save_visual_style_approval", "validate_materialized_visual_style", "visual_style_api_payload", "visual_style_control_defaults", "visual_style_options", "visual_style_preview_path",
]

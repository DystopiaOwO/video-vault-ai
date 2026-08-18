"""Versioned visual-style and title contracts for preview/render parity.

This module deliberately keeps the contract semantic.  FFmpeg filters are
resolved from those values at the final boundary, so an approved project does
not depend on mutable registry objects or ad-hoc UI values.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import os
import unicodedata
from typing import Any, Mapping

from .creative_brief import approved_creative_brief
from .color_pipeline import build_color_filter, color_mode_contract, validate_lut_resource
from .database import connect, init_db
from .paths import root
from .project import project_dir
from .source_fingerprint import parse_source_fingerprint, persisted_fingerprint_for_stat
from .media_probe import probe_media_metadata


VISUAL_STYLE_SCHEMA_VERSION = "visual-style-v1"
VISUAL_STYLE_REGISTRY_VERSION = "visual-style-registry-v1"
TITLE_STYLE_SCHEMA_VERSION = "title-style-v1"
TITLE_STYLE_REGISTRY_VERSION = "title-style-registry-v1"
VISUAL_STYLE_STATE_SCHEMA_VERSION = 1
VISUAL_RENDER_CONTRACT_VERSION = "visual-render-v1"


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
        _validate_title_definition(item)
        self._entries[style_id] = item

    def resolve(self, style_id: str, version: str | int | None = None, *, role: str = "chapter_title", aspect: str = "landscape") -> dict[str, Any]:
        item = self._entries.get(str(style_id))
        if item is None:
            raise VisualStyleError("title_style_unknown", f"unknown title style: {style_id}")
        expected = str(version or item["version"])
        if str(item["version"]) != expected:
            raise VisualStyleError("title_style_version_unsupported", f"unsupported title style version: {style_id}@{expected}")
        if role not in item["supported_roles"]:
            raise VisualStyleError("title_role_unsupported", f"title style {style_id} does not support role {role}")
        _validate_title_definition(item)
        resolved = deepcopy(item)
        resolved["role"] = role
        responsive = item["responsive"]
        resolved["responsive"] = deepcopy(responsive if "anchor" in responsive else responsive.get(aspect, responsive.get("landscape", {})))
        return resolved

    def list(self) -> list[dict[str, Any]]:
        return [deepcopy(item) for item in self._entries.values()]

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
    title_id = title_style_id or str(style["default_title_style_id"])
    title = resolved_title_registry.resolve(title_id, title_style_version, role=title_role, aspect=aspect)
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
        "composition": style["composition"],
        "palette": deepcopy(style["palette"]),
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
    }
    snapshot["resolved_hash"] = _hash(snapshot)
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
    expected_hash = _hash({key: value for key, value in snapshot.items() if key != "resolved_hash"})
    if snapshot.get("resolved_hash") != expected_hash:
        errors.append("resolved_hash mismatch")
    return {"ok": not errors, "errors": errors}


def visual_style_options() -> dict[str, Any]:
    return {
        "schema_version": VISUAL_STYLE_SCHEMA_VERSION,
        "registry_version": VISUAL_STYLE_REGISTRY_VERSION,
        "registry_hash": VISUAL_STYLES.hash(),
        "styles": VISUAL_STYLES.list(include_internal=False),
        "title_styles": TITLE_STYLES.list(),
        "title_roles": sorted({role for item in TITLE_STYLES.list() for role in item["supported_roles"]}),
    }


def visual_style_api_payload(state: Mapping[str, Any] | None) -> dict[str, Any]:
    current = dict(state or {})
    for field in ("recommendation", "approved", "source_provenance"):
        if isinstance(current.get(field), str):
            try:
                current[field] = json.loads(current[field])
            except ValueError:
                current[field] = {}
    if isinstance(current.get("source_provenance"), list):
        current["source_provenance"] = [_public_source(item) for item in current["source_provenance"]]
    current["options"] = visual_style_options()
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
    snapshot = materialize_visual_style(style_id, brief, style_version=payload.get("visual_style_version"), title_style_id=payload.get("title_style_id"), title_style_version=payload.get("title_style_version"), title_role=str(payload.get("title_role") or "chapter_title"), color_settings=project_color)
    preview_variant_id = str(payload.get("preview_variant_id") or "").strip()
    preview_plan_hash = str(payload.get("preview_plan_hash") or "").strip()
    current = load_visual_style_state(db, project_id)
    if not preview_variant_id or not preview_plan_hash:
        raise VisualStyleError("visual_style_preview_required", "必須選取目前真實 preview variant，不能只提交任意 hash")
    evidence = _load_preview_evidence(db, project_id, preview_variant_id)
    _validate_preview_evidence(cfg, db, project_id, evidence, snapshot, brief, project_color, preview_plan_hash)
    snapshot["approved_preview_variant_id"] = preview_variant_id
    snapshot["approved_preview_plan_hash"] = preview_plan_hash
    snapshot["approved_preview_evidence_identity"] = {
        "preview_variant_id": preview_variant_id,
        "preview_image_sha256": str(evidence["preview_image_sha256"]),
        "source_media_uuid": str(evidence["source_media_uuid"]),
        "generated_at": str(evidence["generated_at"]),
    }
    snapshot["resolved_hash"] = _hash({key: value for key, value in snapshot.items() if key != "resolved_hash"})
    preview_revision = int(current.get("preview_revision") or 0) + 1
    source = _source_provenance(db, project_id)
    with connect(db) as con:
        con.execute(
            "update visual_style_states set status='approved', approved_json=?, source_provenance_json=?, creative_brief_revision=?, creative_brief_hash=?, preview_revision=?, updated_at=? where project_id=?",
            (json.dumps(snapshot, ensure_ascii=False, sort_keys=True), json.dumps(source, ensure_ascii=False, sort_keys=True), int(brief.get("brief_version") or 1), str(brief.get("visual_contract_hash") or ""), preview_revision, _now(), int(project_id)),
        )
    return load_visual_style_state(db, project_id)


def _load_preview_evidence(db: Path, project_id: int, preview_variant_id: str) -> dict[str, Any]:
    init_db(db)
    with connect(db) as con:
        row = con.execute("select * from visual_style_preview_evidence where project_id=? and preview_variant_id=?", (int(project_id), preview_variant_id)).fetchone()
    if not row:
        raise VisualStyleError("visual_style_preview_stale", "preview variant 不存在或不屬於此 project")
    result = dict(row)
    for field in ("technical_transform_json", "source_fingerprint_json", "representative_frame_json"):
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
    if str(evidence.get("visual_style_hash") or "") != str(snapshot.get("resolved_hash") or ""):
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
) -> dict[str, Any]:
    """Resolve one immutable, renderer-ready Visual Style contract.

    Preview and formal segment rendering both consume this structure.  The
    semantic snapshot remains the approval input; this function is the only
    place that turns it into pixel-affecting filters.  In particular, DJI/LUT
    technical transforms are delegated to ``color_pipeline`` and are never
    represented as a generic EQ look.
    """
    missing = [field for field in ("resolved_hash", "output", "framing", "grading", "title_style", "palette") if not snapshot.get(field)]
    expected_snapshot_hash = _hash({key: value for key, value in snapshot.items() if key != "resolved_hash"})
    if missing or str(snapshot.get("resolved_hash") or "") != expected_snapshot_hash:
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
    title_plan = _resolve_title_plan(snapshot, width, height, title_text, title_enable=title_enable, title_role=title_role, duration_seconds=title_duration_seconds)
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
        "visual_style_hash": str(snapshot.get("resolved_hash") or ""),
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
    plan["resolved_hash"] = _hash({key: value for key, value in plan.items() if key != "resolved_hash"})
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
) -> dict[str, Any]:
    title_style = dict(snapshot.get("title_style") or {})
    original_role = str(title_style.get("role") or "chapter_title")
    role = str(title_role or original_role)
    role_tokens = title_style.get("role_tokens") if isinstance(title_style.get("role_tokens"), Mapping) else {}
    role_override = dict(role_tokens.get(role) or {}) if role != original_role else {}
    title_style = {**title_style, **role_override}
    title_style["role"] = role
    responsive = title_style.get("responsive") or {}
    aspect = str((snapshot.get("output") or {}).get("orientation") or "landscape")
    responsive = dict(responsive.get(aspect) or responsive.get("landscape") or responsive)
    safe = dict(title_style.get("safe_zone") or {})
    anchor = str(responsive.get("anchor") or "bottom-left")
    size_ratio = float(responsive.get("size_ratio") or 0.052)
    left = float(safe.get("left") or 0.05)
    right = float(safe.get("right") or 0.05)
    top = float(safe.get("top") or 0.06)
    bottom = float(safe.get("bottom") or 0.08)
    if anchor == "top-left":
        x, y = f"w*{left:.6f}", f"h*{top:.6f}"
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
    font_path = _system_font_path(title_style.get("fallback_chain"), int(title_style.get("weight") or 500))
    font_option = f":fontfile='{_escape_filter_path(font_path)}'" if font_path else ""
    readability = dict(title_style.get("readability") or {})
    max_width_ratio = min(0.95, max(0.2, float(title_style.get("max_width_ratio") or 0.78)))
    max_width_pixels = max(1, int(width * max_width_ratio))
    font_size = max(20, int(height * size_ratio))
    wrapped_text, wrap_lines = _wrap_title_text(str(title_text), max_width_pixels, font_size)
    escaped = wrapped_text.replace("\\", "\\\\").replace("\n", "\\n").replace(":", "\\:").replace("'", "\\'")
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
    enter = max(0.001, float(motion.get("enter_seconds") or 0.28))
    exit_seconds = max(0.001, float(motion.get("exit_seconds") or 0.22))
    duration = max(enter + exit_seconds, float(duration_seconds or 1.0))
    alpha = f"if(lt(t\\,{enter:.6f})\\,t/{enter:.6f}\\,if(lt(t\\,{max(enter, duration-exit_seconds):.6f})\\,1\\,max(0\\,({duration:.6f}-t)/{exit_seconds:.6f})))"
    if preset in {"fade", "fade_rise", "slide_fade"}:
        options.append(f"alpha={alpha}")
    if preset == "fade_rise":
        options[-1:] = [f"alpha={alpha}", f"y=({y})+((1-min(t/{enter:.6f}\\,1))*{max(4, int(height * 0.018))})"]
    elif preset == "slide_fade":
        options[-1:] = [f"alpha={alpha}", f"x=({x})-((1-min(t/{enter:.6f}\\,1))*{max(6, int(width * 0.025))})"]
    if title_enable:
        options.append(f"enable='{title_enable}'")
    drawtext = "drawtext=" + ":".join(option for option in options if option)
    return {"role": role, "text": str(title_text), "wrapped_text": wrapped_text, "wrap_lines": wrap_lines, "anchor": anchor, "safe_zone": safe, "max_width_ratio": max_width_ratio, "max_width_pixels": max_width_pixels, "max_width_enforced": True, "font_path": str(font_path or ""), "weight": int(title_style.get("weight") or 500), "font_size": font_size, "filter": drawtext if title_text else "", "motion": {**motion, "resolved_enter_seconds": enter, "resolved_exit_seconds": exit_seconds, "resolved_duration_seconds": duration}}


def _wrap_title_text(text: str, max_width_pixels: int, font_size: int, *, max_lines: int = 3) -> tuple[str, int]:
    """Wrap and bound titles before FFmpeg drawtext sees them.

    FFmpeg's ``boxw`` bounds the box, but does not make the title text itself
    wrap.  This deterministic display-unit wrapper is shared by Preview and
    Formal Render through the resolved title plan and clamps overlong content
    to the approved safe width instead of allowing silent clipping.
    """

    limit = max(4, int(max_width_pixels / max(1.0, float(font_size) * 0.92)))
    lines: list[str] = []
    current = ""
    units = 0
    for char in str(text):
        if char == "\n":
            lines.append(current.rstrip())
            current, units = "", 0
            continue
        width = 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if current and units + width > limit:
            lines.append(current.rstrip())
            current, units = "", 0
        current += char
        units += width
    if current or not lines:
        lines.append(current.rstrip())
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
    plan = resolve_visual_render_plan(snapshot, width=width, height=height, title_text=title_text, color_settings=color_settings, source_display_ratio=source_display_ratio, source_geometry=source_geometry, title_role=title_role, title_duration_seconds=title_duration_seconds)
    filter_graph = plan["filter_graph"]
    # Place the seek after the input so synthetic/short-GOP fixtures still
    # yield a decoded representative frame instead of an empty image stream.
    command = [str(cfg["ffmpeg_path"]), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source), "-ss", f"{float(timestamp_seconds):.6f}"]
    if str(plan.get("graph_type") or "linear") == "split_background_overlay":
        command.extend(["-filter_complex", materialize_visual_graph(plan, input_label="[0:v]", output_label="[vout]"), "-map", "[vout]"])
    else:
        command.extend(["-vf", filter_graph])
    command.extend(["-frames:v", "1", str(output)])
    result = runner(command) if runner else subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)
    returncode = int(getattr(result, "returncode", 0))
    if returncode != 0 or not output.is_file() or output.stat().st_size <= 0:
        stderr = str(getattr(result, "stderr", ""))[-1200:]
        raise VisualStyleError("preview_render_failed", f"true-frame preview failed: {stderr}")
    return {"file": str(output), "sha256": _file_hash(output), "timestamp_seconds": float(timestamp_seconds), "width": width, "height": height, "filter_contract": filter_graph, "visual_render_plan": plan, "visual_style_hash": snapshot.get("resolved_hash"), "title_text": title_text}


def visual_style_preview_path(cfg: Mapping[str, Any], project_id: int, filename: str) -> Path:
    token = Path(str(filename)).name
    if token != str(filename) or not token or token.startswith("."):
        raise VisualStyleError("preview_path_invalid", "invalid visual style preview filename")
    return project_dir(dict(cfg), int(project_id)) / "output" / "visual_style_previews" / token


def preview_visual_styles(cfg: Mapping[str, Any], db: Path, project_id: int, *, force: bool = False) -> dict[str, Any]:
    """Generate bounded true-frame variants only after Creative Brief approval."""
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
    # Four variants are intentionally generated for each deterministic frame:
    # the three public looks plus the standalone card comparison surface.
    styles = [item for item in VISUAL_STYLES.list(include_internal=True) if item.get("style_id") in {"diary_natural", "clean_minimal", "cinematic", "standalone_card_compare"}]
    for frame in representative_frames:
        source = frame["source"]
        timestamp = float(frame["timestamp_seconds"])
        for style in styles:
            snapshot = materialize_visual_style(str(style["style_id"]), brief, color_settings=color_settings)
            preview_plan = resolve_visual_render_plan(snapshot, width=int((snapshot.get("output") or {}).get("width") or 1920), height=int((snapshot.get("output") or {}).get("height") or 1080), title_text="咖啡日記 / Coffee Diary", color_settings=color_settings, source_geometry=frame.get("display_geometry") if isinstance(frame.get("display_geometry"), Mapping) else None)
            token = _hash({"source": source, "timestamp": timestamp, "style": snapshot["resolved_hash"], "plan": preview_plan["resolved_hash"], "creative_brief_hash": brief.get("visual_contract_hash"), "frame": frame["selection_reason"]})[:20]
            output = visual_style_preview_path(cfg, project_id, f"{style['style_id']}-{token}.png")
            if force:
                output.unlink(missing_ok=True)
            if output.is_file() and output.stat().st_size > 0:
                item = {"file": str(output), "sha256": _file_hash(output), "cache_hit": True, "visual_style": snapshot, "source": _public_source(source), "timestamp_seconds": timestamp}
            else:
                item = render_true_frame_preview(cfg, project_id, Path(str(source["path"])), timestamp, snapshot, output, color_settings=color_settings, source_display_ratio=float((frame.get("display_geometry") or {}).get("display_ratio") or 0.0) or None, source_geometry=frame.get("display_geometry") if isinstance(frame.get("display_geometry"), Mapping) else None)
                item.update({"cache_hit": False, "visual_style": snapshot, "source": _public_source(source)})
            item["preview_plan_hash"] = str(preview_plan.get("resolved_hash") or "")
            item["technical_transform"] = dict(preview_plan.get("technical_transform") or {})
            item["representative_frame"] = {key: value for key, value in frame.items() if key != "source"}
            item["preview_variant_id"] = _hash({"project_id": project_id, "style": snapshot.get("resolved_hash"), "plan": item["preview_plan_hash"], "source": source.get("project_media_uuid"), "timestamp": timestamp, "frame": frame.get("selection_reason")})[:32]
            item["title_style_identity"] = _hash(snapshot.get("title_style") or {})
            item["creative_brief_hash"] = str(brief.get("visual_contract_hash") or "")
            item["source_media_uuid"] = str(source.get("project_media_uuid") or "")
            item["source_fingerprint"] = dict(source.get("fingerprint") or {})
            item["generated_at"] = _now()
            _persist_preview_evidence(db, project_id, state, item, snapshot)
            variants.append(item)
    return {"ok": True, "status": "ready", "preview_revision": int(state.get("preview_revision") or 0), "source": _public_source(representative_frames[0]["source"]), "representative_frames": [{key: value for key, value in frame.items() if key != "source"} for frame in representative_frames], "variants": variants}


def _persist_preview_evidence(db: Path, project_id: int, state: Mapping[str, Any], item: Mapping[str, Any], snapshot: Mapping[str, Any]) -> None:
    with connect(db) as con:
        con.execute(
            "insert or replace into visual_style_preview_evidence(preview_variant_id, project_id, preview_revision, preview_plan_hash, visual_style_id, visual_style_version, visual_style_hash, title_style_identity, creative_brief_hash, technical_transform_json, source_media_uuid, source_fingerprint_json, timestamp_seconds, representative_frame_json, preview_filename, preview_image_sha256, generated_at) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(item["preview_variant_id"]), int(project_id), int(state.get("preview_revision") or 0), str(item["preview_plan_hash"]), str(snapshot.get("visual_style_id") or ""), str(snapshot.get("visual_style_version") or ""), str(snapshot.get("resolved_hash") or ""), str(item["title_style_identity"]), str(item["creative_brief_hash"]), json.dumps(item.get("technical_transform") or {}, ensure_ascii=False, sort_keys=True), str(item["source_media_uuid"]), json.dumps(item.get("source_fingerprint") or {}, ensure_ascii=False, sort_keys=True), float(item.get("timestamp_seconds") or 0), json.dumps(item.get("representative_frame") or {}, ensure_ascii=False, sort_keys=True), Path(str(item.get("file") or "")).name, str(item["sha256"]), str(item["generated_at"]),
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
        "diary_natural": {"version": "1", "label": "Diary Natural", "composition": "overlay", "default_title_style_id": "diary_natural_overlay", "palette": {"text_primary": "#FFF8EE", "text_secondary": "#E8DED0", "accent": "#E1A46A", "surface_overlay": "#241B14CC", "surface_overlay_strong": "#17110DEE", "shadow": "#00000099"}, "grading": {"look_id": "diary-warm-neutral", "look_version": "1", "source_colorspace": "bt709", "brightness": 0.015, "contrast": 1.03, "saturation": 1.04}, "supported_aspects": ["landscape", "portrait"], "enabled_for_round1_ui": True, "required_capabilities": {}},
        "clean_minimal": {"version": "1", "label": "Clean Minimal", "composition": "overlay", "default_title_style_id": "clean_minimal_overlay", "palette": {"text_primary": "#FFFFFF", "text_secondary": "#E7E7E7", "accent": "#9ED8FF", "surface_overlay": "#111111B8", "surface_overlay_strong": "#111111DD", "shadow": "#00000080"}, "grading": {"look_id": "clean-neutral", "look_version": "1", "source_colorspace": "bt709", "brightness": 0.0, "contrast": 1.0, "saturation": 0.96}, "supported_aspects": ["landscape", "portrait"], "enabled_for_round1_ui": True, "required_capabilities": {}},
        "cinematic": {"version": "1", "label": "Cinematic", "composition": "overlay", "default_title_style_id": "cinematic_overlay", "palette": {"text_primary": "#FFF7DD", "text_secondary": "#E0D4B8", "accent": "#D6A85C", "surface_overlay": "#0B1820C7", "surface_overlay_strong": "#071016E6", "shadow": "#000000AA"}, "grading": {"look_id": "cinematic-teal-gold", "look_version": "1", "source_colorspace": "bt709", "brightness": -0.015, "contrast": 1.08, "saturation": 1.08}, "supported_aspects": ["landscape", "portrait"], "enabled_for_round1_ui": True, "required_capabilities": {}},
        "standalone_card_compare": {"version": "1", "label": "Standalone Card Compare", "composition": "standalone", "default_title_style_id": "standalone_card", "palette": {"text_primary": "#FFF8EE", "text_secondary": "#D9D9D9", "accent": "#E1A46A", "surface_overlay": "#20242AFF", "surface_overlay_strong": "#20242AFF", "shadow": "#000000AA"}, "grading": {"look_id": "card-neutral", "look_version": "1", "source_colorspace": "bt709", "brightness": 0.0, "contrast": 1.0, "saturation": 1.0}, "supported_aspects": ["landscape", "portrait"], "enabled_for_round1_ui": False, "required_capabilities": {}},
        "test_soft_panel": {"version": "1", "label": "Test Soft Panel", "composition": "overlay", "default_title_style_id": "test_soft_panel", "palette": {"text_primary": "#FFFFFF", "text_secondary": "#E8F4FF", "accent": "#7BDFF2", "surface_overlay": "#153047CC", "surface_overlay_strong": "#102033E6", "shadow": "#00000088"}, "grading": {"look_id": "test-soft-panel", "look_version": "1", "source_colorspace": "bt709", "brightness": 0.02, "contrast": 1.01, "saturation": 1.02}, "supported_aspects": ["landscape", "portrait"], "enabled_for_round1_ui": False, "required_capabilities": {}}
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
        stat = {"size": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns} if path.is_file() else {}
        persisted = parse_source_fingerprint(str(row["source_fingerprint_json"] or "{}"))
        if path.is_file() and persisted and persisted_fingerprint_for_stat(path, persisted) is None:
            raise VisualStyleError("source_changed", f"source fingerprint changed: {path}")
        result.append({"project_media_uuid": str(row["project_media_uuid"] or ""), "video_id": int(row["video_id"]), "path": str(path), "fingerprint": persisted or stat})
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


def _system_font_path(fallback_chain: Any = None, weight: int = 500) -> Path | None:
    """Resolve only installed system fonts; never download or bundle assets."""
    candidates: list[Path] = []
    windows_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    if int(weight or 500) >= 600:
        candidates.extend([windows_fonts / "msyhbd.ttc", windows_fonts / "segoeuib.ttf", windows_fonts / "arialbd.ttf"])
    candidates.extend([windows_fonts / "msyh.ttc", windows_fonts / "msjh.ttc", windows_fonts / "segoeui.ttf", windows_fonts / "arial.ttf"])
    candidates.extend([Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")])
    for path in candidates:
        if path.is_file():
            return path
    return None


def _escape_filter_path(path: Path | None) -> str:
    if path is None:
        return ""
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "TITLE_STYLES", "TITLE_STYLE_REGISTRY_VERSION", "TITLE_STYLE_SCHEMA_VERSION", "VISUAL_STYLES", "VISUAL_STYLE_REGISTRY_VERSION", "VISUAL_STYLE_SCHEMA_VERSION", "VISUAL_RENDER_CONTRACT_VERSION", "VisualStyleError", "build_preview_filter", "ensure_visual_style_state", "load_visual_style_state", "materialize_visual_graph", "materialize_visual_style", "preview_visual_styles", "render_true_frame_preview", "resolve_visual_render_plan", "save_visual_style_approval", "validate_materialized_visual_style", "visual_style_api_payload", "visual_style_options", "visual_style_preview_path",
]

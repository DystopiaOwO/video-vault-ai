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
from typing import Any, Mapping

from .creative_brief import approved_creative_brief
from .database import connect, init_db
from .paths import root
from .project import project_dir
from .source_fingerprint import parse_source_fingerprint, persisted_fingerprint_for_stat


VISUAL_STYLE_SCHEMA_VERSION = "visual-style-v1"
VISUAL_STYLE_REGISTRY_VERSION = "visual-style-registry-v1"
TITLE_STYLE_SCHEMA_VERSION = "title-style-v1"
TITLE_STYLE_REGISTRY_VERSION = "title-style-registry-v1"
VISUAL_STYLE_STATE_SCHEMA_VERSION = 1


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
            return _state_row(row)
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


def save_visual_style_approval(cfg: Mapping[str, Any], db: Path, project_id: int, payload: Mapping[str, Any], *, base_revision: int | None = None) -> dict[str, Any]:
    brief = _load_brief(db, project_id)
    if not brief or brief.get("status") != "approved":
        raise VisualStyleError("creative_brief_required", "Creative Brief 尚未由 human 核准，不能核准 Visual Style")
    style_id = str(payload.get("visual_style_id") or "")
    snapshot = materialize_visual_style(style_id, brief, style_version=payload.get("visual_style_version"), title_style_id=payload.get("title_style_id"), title_style_version=payload.get("title_style_version"), title_role=str(payload.get("title_role") or "chapter_title"))
    current = load_visual_style_state(db, project_id)
    preview_revision = int(current.get("preview_revision") or 0) + 1
    source = _source_provenance(db, project_id)
    with connect(db) as con:
        con.execute(
            "update visual_style_states set status='approved', approved_json=?, source_provenance_json=?, creative_brief_revision=?, creative_brief_hash=?, preview_revision=?, updated_at=? where project_id=?",
            (json.dumps(snapshot, ensure_ascii=False, sort_keys=True), json.dumps(source, ensure_ascii=False, sort_keys=True), int(brief.get("brief_version") or 1), str(brief.get("visual_contract_hash") or ""), preview_revision, _now(), int(project_id)),
        )
    return load_visual_style_state(db, project_id)


def build_preview_filter(snapshot: Mapping[str, Any], *, width: int, height: int, title_text: str = "") -> str:
    """Resolve display-safe framing and the actual creative grading transform."""
    framing = snapshot.get("framing") or {}
    strategy = str(framing.get("strategy_id") or "preserve_full_frame")
    if strategy == "crop_reframe":
        geometry = f"scale={int(width)}:{int(height)}:force_original_aspect_ratio=increase,crop={int(width)}:{int(height)}"
    elif strategy == "background_treatment":
        geometry = f"scale={int(width)}:{int(height)}:force_original_aspect_ratio=decrease,pad={int(width)}:{int(height)}:(ow-iw)/2:(oh-ih)/2:color=0x20242a"
    elif strategy == "preserve_full_frame":
        geometry = f"scale={int(width)}:{int(height)}:force_original_aspect_ratio=decrease,pad={int(width)}:{int(height)}:(ow-iw)/2:(oh-ih)/2:color=0x20242a"
    else:
        raise VisualStyleError("framing_unknown", f"unknown framing strategy: {strategy}")
    grading = snapshot.get("grading") or {}
    brightness = float(grading.get("brightness") or 0)
    contrast = float(grading.get("contrast") or 1)
    saturation = float(grading.get("saturation") or 1)
    grade = f"eq=brightness={brightness:.5f}:contrast={contrast:.5f}:saturation={saturation:.5f}"
    graph = f"{geometry},setsar=1,{grade}"
    if title_text:
        title = str(title_text).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        title_style = snapshot.get("title_style") or {}
        responsive = title_style.get("responsive") or {}
        size_ratio = float(responsive.get("size_ratio") or 0.052)
        color = str((snapshot.get("palette") or {}).get("text_primary") or "#FFFFFF").lstrip("#")[:6]
        shadow = str((snapshot.get("palette") or {}).get("shadow") or "#00000099").lstrip("#")[:6]
        font_path = _system_font_path(title_style.get("fallback_chain"))
        font_option = f":fontfile='{_escape_filter_path(font_path)}'" if font_path else ""
        graph += f",drawtext=text='{title}'{font_option}:fontcolor=0x{color}:fontsize={max(20, int(height * size_ratio))}:x=w*0.06:y=h*0.82:shadowcolor=0x{shadow}:shadowx=2:shadowy=2"
    return graph


def render_true_frame_preview(
    cfg: Mapping[str, Any],
    project_id: int,
    source: Path,
    timestamp_seconds: float,
    snapshot: Mapping[str, Any],
    output: Path,
    *,
    runner: Any | None = None,
) -> dict[str, Any]:
    """Render one real source frame through the same semantic filter resolver."""
    output.parent.mkdir(parents=True, exist_ok=True)
    width = int((snapshot.get("output") or {}).get("width") or 1920)
    height = int((snapshot.get("output") or {}).get("height") or 1080)
    filter_graph = build_preview_filter(snapshot, width=width, height=height, title_text="咖啡日記 / Coffee Diary")
    command = [str(cfg["ffmpeg_path"]), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-ss", f"{float(timestamp_seconds):.6f}", "-i", str(source), "-frames:v", "1", "-vf", filter_graph, "-frames:v", "1", str(output)]
    result = runner(command) if runner else subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)
    returncode = int(getattr(result, "returncode", 0))
    if returncode != 0 or not output.is_file() or output.stat().st_size <= 0:
        stderr = str(getattr(result, "stderr", ""))[-1200:]
        raise VisualStyleError("preview_render_failed", f"true-frame preview failed: {stderr}")
    return {"file": str(output), "sha256": _file_hash(output), "timestamp_seconds": float(timestamp_seconds), "width": width, "height": height, "filter_contract": filter_graph, "visual_style_hash": snapshot.get("resolved_hash"), "title_text": "咖啡日記 / Coffee Diary"}


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
    source = next((item for item in sources if Path(str(item.get("path") or "")).is_file()), None)
    if not source:
        raise VisualStyleError("preview_source_missing", "找不到可用的 approved source")
    timestamp = 0.5
    variants: list[dict[str, Any]] = []
    for style in VISUAL_STYLES.list(include_internal=False):
        snapshot = materialize_visual_style(str(style["style_id"]), brief)
        token = _hash({"source": source, "timestamp": timestamp, "style": snapshot["resolved_hash"]})[:20]
        output = visual_style_preview_path(cfg, project_id, f"{style['style_id']}-{token}.png")
        if force:
            output.unlink(missing_ok=True)
        if output.is_file() and output.stat().st_size > 0:
            item = {"file": str(output), "sha256": _file_hash(output), "cache_hit": True, "visual_style": snapshot, "source": _public_source(source), "timestamp_seconds": timestamp}
        else:
            item = render_true_frame_preview(cfg, project_id, Path(str(source["path"])), timestamp, snapshot, output)
            item.update({"cache_hit": False, "visual_style": snapshot, "source": _public_source(source)})
        variants.append(item)
    return {"ok": True, "status": "ready", "preview_revision": int(state.get("preview_revision") or 0), "source": _public_source(source), "variants": variants}


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
    direction = dict((brief.get("framing_intent") or {}).get(direction_id) or {})
    strategy = str(direction.get("approved_strategy_id") or direction.get("approved_strategy") or "preserve_full_frame")
    if strategy not in {"auto_recommended", "crop_reframe", "background_treatment", "preserve_full_frame"}:
        raise VisualStyleError("framing_unknown", f"unknown approved framing strategy: {strategy}")
    if strategy == "auto_recommended":
        strategy = "crop_reframe"
    return {"direction_id": direction_id, "source_orientation": direction.get("source_orientation") or "unknown", "target_orientation": target, "strategy_id": strategy, "strategy_version": str(direction.get("approved_strategy_version") or "1"), "resolved_semantic": deepcopy(direction.get("resolved_semantic") or {})}


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


def _system_font_path(fallback_chain: Any = None) -> Path | None:
    """Resolve only installed system fonts; never download or bundle assets."""
    candidates: list[Path] = []
    windows_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
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
    "TITLE_STYLES", "TITLE_STYLE_REGISTRY_VERSION", "TITLE_STYLE_SCHEMA_VERSION", "VISUAL_STYLES", "VISUAL_STYLE_REGISTRY_VERSION", "VISUAL_STYLE_SCHEMA_VERSION", "VisualStyleError", "build_preview_filter", "ensure_visual_style_state", "load_visual_style_state", "materialize_visual_style", "preview_visual_styles", "render_true_frame_preview", "save_visual_style_approval", "validate_materialized_visual_style", "visual_style_api_payload", "visual_style_options", "visual_style_preview_path",
]

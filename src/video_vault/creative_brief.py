"""Versioned project Creative Brief for early output-direction approval."""

from __future__ import annotations

from datetime import datetime, timezone
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .database import connect, init_db, project_videos
from .media_probe import probe_media_metadata
from .project_lifecycle import project_commit
from .source_fingerprint import parse_source_fingerprint


CREATIVE_BRIEF_SCHEMA_VERSION = 1
CREATIVE_BRIEF_CONTRACT_VERSION = "creative-brief-v1"
CREATIVE_BRIEF_RECOMMENDATION_VERSION = "source-geometry-orientation-v1"
CREATIVE_BRIEF_REGISTRY_VERSION = "creative-brief-registry-v1"
CREATIVE_BRIEF_STATUS_NEEDS_CONFIRMATION = "needs_confirmation"
CREATIVE_BRIEF_STATUS_APPROVED = "approved"


class OutputContractRegistry:
    """Small deterministic registry for output contracts.

    Entries are data, not resolver branches.  Tests and future features can
    register an additional contract without editing the Round-1 materializer.
    """

    def __init__(self, entries: list[Mapping[str, Any]] | None = None, *, version: str = CREATIVE_BRIEF_REGISTRY_VERSION):
        self.version = str(version)
        self._entries: dict[str, dict[str, Any]] = {}
        for entry in entries or []:
            self.register(entry)

    def register(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        normalized = {
            "output_contract_id": str(entry.get("output_contract_id") or "").strip(),
            "version": str(entry.get("version") or "").strip(),
            "orientation": str(entry.get("orientation") or "").strip().lower(),
            "aspect_ratio": str(entry.get("aspect_ratio") or "").strip(),
            "width": int(entry.get("width") or 0),
            "height": int(entry.get("height") or 0),
            "render_profile_id": str(entry.get("render_profile_id") or "").strip(),
            "enabled_for_round1_ui": bool(entry.get("enabled_for_round1_ui", False)),
            "capability": deepcopy(entry.get("capability") if isinstance(entry.get("capability"), Mapping) else {}),
            "label": str(entry.get("label") or entry.get("output_contract_id") or "").strip(),
        }
        if not normalized["output_contract_id"] or not normalized["version"]:
            raise ValueError("output contract requires stable id and version")
        if not normalized["orientation"] or not normalized["aspect_ratio"] or normalized["width"] <= 0 or normalized["height"] <= 0:
            raise ValueError(f"invalid output contract: {normalized['output_contract_id']}")
        if not normalized["render_profile_id"]:
            raise ValueError(f"output contract missing render_profile_id: {normalized['output_contract_id']}")
        if normalized["output_contract_id"] in self._entries:
            raise ValueError(f"duplicate output contract: {normalized['output_contract_id']}")
        self._entries[normalized["output_contract_id"]] = normalized
        return deepcopy(normalized)

    def resolve(self, contract_id: str, version: str | None = None) -> dict[str, Any]:
        key = str(contract_id or "").strip()
        entry = self._entries.get(key)
        if entry is None:
            raise ValueError(f"unknown output_contract_id: {key or 'empty'}")
        if version not in (None, "") and str(version) != entry["version"]:
            raise ValueError(f"unsupported output contract version: {key}@{version}")
        return deepcopy(entry)

    def entries(self, *, enabled_for_round1_ui: bool | None = None) -> list[dict[str, Any]]:
        entries = list(self._entries.values())
        if enabled_for_round1_ui is not None:
            entries = [entry for entry in entries if entry["enabled_for_round1_ui"] is enabled_for_round1_ui]
        return deepcopy(entries)

    def for_orientation(self, orientation: str, *, enabled_for_round1_ui: bool | None = None) -> dict[str, Any]:
        normalized = str(orientation or "").strip().lower()
        matches = [entry for entry in self.entries(enabled_for_round1_ui=enabled_for_round1_ui) if entry["orientation"] == normalized]
        if not matches:
            raise ValueError(f"no output contract for orientation: {normalized or 'empty'}")
        return matches[0]

    def hash(self) -> str:
        return _hash({"version": self.version, "entries": self.entries()})


class MismatchDirectionRegistry:
    def __init__(self, entries: list[Mapping[str, Any]] | None = None, *, version: str = CREATIVE_BRIEF_REGISTRY_VERSION):
        self.version = str(version)
        self._entries: dict[str, dict[str, Any]] = {}
        for entry in entries or []:
            self.register(entry)

    def register(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        normalized = {
            "direction_id": str(entry.get("direction_id") or "").strip(),
            "version": str(entry.get("version") or "").strip(),
            "source_orientation": str(entry.get("source_orientation") or "").strip().lower(),
            "target_orientation": str(entry.get("target_orientation") or "").strip().lower(),
            "label": str(entry.get("label") or entry.get("direction_id") or "").strip(),
            "description": str(entry.get("description") or "").strip(),
        }
        if not normalized["direction_id"] or not normalized["version"] or not normalized["source_orientation"] or not normalized["target_orientation"]:
            raise ValueError("mismatch direction requires stable id/version/source/target")
        if normalized["direction_id"] in self._entries:
            raise ValueError(f"duplicate mismatch direction: {normalized['direction_id']}")
        self._entries[normalized["direction_id"]] = normalized
        return deepcopy(normalized)

    def resolve(self, direction_id: str, version: str | None = None) -> dict[str, Any]:
        key = str(direction_id or "").strip()
        entry = self._entries.get(key)
        if entry is None:
            raise ValueError(f"unknown mismatch direction: {key or 'empty'}")
        if version not in (None, "") and str(version) != entry["version"]:
            raise ValueError(f"unsupported mismatch direction version: {key}@{version}")
        return deepcopy(entry)

    def entries(self) -> list[dict[str, Any]]:
        return deepcopy(list(self._entries.values()))

    def hash(self) -> str:
        return _hash({"version": self.version, "entries": self.entries()})


class FramingStrategyRegistry:
    def __init__(self, directions: MismatchDirectionRegistry, entries: list[Mapping[str, Any]] | None = None, *, version: str = CREATIVE_BRIEF_REGISTRY_VERSION):
        self.version = str(version)
        self.directions = directions
        self._entries: dict[str, dict[str, Any]] = {}
        for entry in entries or []:
            self.register(entry)

    def register(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        supported = tuple(str(item).strip() for item in (entry.get("supported_direction_ids") or ()))
        for direction_id in supported:
            self.directions.resolve(direction_id)
        normalized = {
            "strategy_id": str(entry.get("strategy_id") or "").strip(),
            "version": str(entry.get("version") or "").strip(),
            "supported_direction_ids": list(supported),
            "label": str(entry.get("label") or entry.get("strategy_id") or "").strip(),
            "description": str(entry.get("description") or "").strip(),
            "semantic": deepcopy(entry.get("semantic") if isinstance(entry.get("semantic"), Mapping) else {}),
            "capability": deepcopy(entry.get("capability") if isinstance(entry.get("capability"), Mapping) else {}),
        }
        if not normalized["strategy_id"] or not normalized["version"] or not normalized["supported_direction_ids"]:
            raise ValueError("framing strategy requires stable id/version/directions")
        if normalized["strategy_id"] in self._entries:
            raise ValueError(f"duplicate framing strategy: {normalized['strategy_id']}")
        self._entries[normalized["strategy_id"]] = normalized
        return deepcopy(normalized)

    def resolve(self, strategy_id: str, version: str | None = None, *, direction_id: str | None = None) -> dict[str, Any]:
        key = str(strategy_id or "").strip()
        entry = self._entries.get(key)
        if entry is None:
            raise ValueError(f"unknown framing strategy: {key or 'empty'}")
        if version not in (None, "") and str(version) != entry["version"]:
            raise ValueError(f"unsupported framing strategy version: {key}@{version}")
        if direction_id is not None and direction_id not in entry["supported_direction_ids"]:
            raise ValueError(f"framing strategy {key} is not applicable to {direction_id}")
        return deepcopy(entry)

    def entries(self, *, direction_id: str | None = None) -> list[dict[str, Any]]:
        entries = list(self._entries.values())
        if direction_id is not None:
            entries = [entry for entry in entries if direction_id in entry["supported_direction_ids"]]
        return deepcopy(entries)

    def hash(self) -> str:
        return _hash({"version": self.version, "entries": self.entries()})


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


MISMATCH_DIRECTION_REGISTRY = MismatchDirectionRegistry([
    {
        "direction_id": "portrait_source_in_landscape",
        "version": "1",
        "source_orientation": "portrait",
        "target_orientation": "landscape",
        "label": "橫向輸出 + 直向素材",
        "description": "portrait source framed for a landscape target",
    },
    {
        "direction_id": "landscape_source_in_portrait",
        "version": "1",
        "source_orientation": "landscape",
        "target_orientation": "portrait",
        "label": "直向輸出 + 橫向素材",
        "description": "landscape source framed for a portrait target",
    },
])

OUTPUT_CONTRACT_REGISTRY = OutputContractRegistry([
    {
        "output_contract_id": "landscape_16_9",
        "version": "1",
        "orientation": "landscape",
        "aspect_ratio": "16:9",
        "width": 1920,
        "height": 1080,
        "render_profile_id": "final_1080p",
        "enabled_for_round1_ui": True,
        "label": "橫向 16:9",
        "capability": {"round1": True},
    },
    {
        "output_contract_id": "portrait_9_16",
        "version": "1",
        "orientation": "portrait",
        "aspect_ratio": "9:16",
        "width": 1080,
        "height": 1920,
        "render_profile_id": "final_1080p_portrait",
        "enabled_for_round1_ui": True,
        "label": "直向 9:16",
        "capability": {"round1": True},
    },
])

FRAMING_STRATEGY_REGISTRY = FramingStrategyRegistry(MISMATCH_DIRECTION_REGISTRY, [
    {
        "strategy_id": "auto_recommended",
        "version": "1",
        "supported_direction_ids": ["portrait_source_in_landscape", "landscape_source_in_portrait"],
        "label": "依 AI 建議",
        "description": "use the recommendation selected by the project brief",
        "semantic": {"kind": "deferred_policy"},
        "capability": {"implemented_in_vid26": False},
    },
    {
        "strategy_id": "crop_reframe",
        "version": "1",
        "supported_direction_ids": ["portrait_source_in_landscape", "landscape_source_in_portrait"],
        "label": "裁切／重新構圖",
        "description": "crop or reframe while preserving the subject",
        "semantic": {"kind": "crop_reframe"},
        "capability": {"implemented_in_vid26": False},
    },
    {
        "strategy_id": "background_treatment",
        "version": "1",
        "supported_direction_ids": ["portrait_source_in_landscape", "landscape_source_in_portrait"],
        "label": "背景處理（VID-27）",
        "description": "use a background treatment without stretching the foreground",
        "semantic": {"kind": "background_treatment"},
        "capability": {"implemented_in_vid26": False, "owner": "VID-27"},
    },
    {
        "strategy_id": "preserve_full_frame",
        "version": "1",
        "supported_direction_ids": ["portrait_source_in_landscape", "landscape_source_in_portrait"],
        "label": "保留完整畫面",
        "description": "preserve the complete source frame",
        "semantic": {"kind": "preserve_full_frame"},
        "capability": {"implemented_in_vid26": False},
    },
])


def _combined_registry_hash(
    output_registry: OutputContractRegistry,
    direction_registry: MismatchDirectionRegistry,
    strategy_registry: FramingStrategyRegistry,
) -> str:
    return _hash({
        "registry_version": CREATIVE_BRIEF_REGISTRY_VERSION,
        "output_contracts": output_registry.entries(),
        "mismatch_directions": direction_registry.entries(),
        "framing_strategies": strategy_registry.entries(),
    })


def _registry_hash() -> str:
    return _combined_registry_hash(OUTPUT_CONTRACT_REGISTRY, MISMATCH_DIRECTION_REGISTRY, FRAMING_STRATEGY_REGISTRY)


def creative_brief_options() -> dict[str, Any]:
    """Materialized API options; persisted approvals never depend on this list."""
    return {
        "registry_version": CREATIVE_BRIEF_REGISTRY_VERSION,
        "registry_hash": _registry_hash(),
        "output_contracts": OUTPUT_CONTRACT_REGISTRY.entries(),
        "mismatch_directions": MISMATCH_DIRECTION_REGISTRY.entries(),
        "framing_strategies": FRAMING_STRATEGY_REGISTRY.entries(),
    }


def creative_brief_api_payload(brief: Mapping[str, Any]) -> dict[str, Any]:
    payload = deepcopy(dict(brief))
    payload["options"] = creative_brief_options()
    return payload


def _orientation(width: int, height: int) -> str:
    if width > height:
        return "landscape"
    if height > width:
        return "portrait"
    return "square"


def _output_for_orientation(orientation: str, registry: OutputContractRegistry = OUTPUT_CONTRACT_REGISTRY) -> dict[str, Any]:
    return registry.for_orientation(orientation, enabled_for_round1_ui=True)


def _materialized_output(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "output_contract_id": str(entry["output_contract_id"]),
        "output_contract_version": str(entry["version"]),
        "orientation": str(entry["orientation"]),
        "aspect_ratio": str(entry["aspect_ratio"]),
        "width": int(entry["width"]),
        "height": int(entry["height"]),
        "render_profile_id": str(entry["render_profile_id"]),
        "capability": deepcopy(entry.get("capability") or {}),
    }


def _materialized_strategy(entry: Mapping[str, Any], direction: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "strategy_id": str(entry["strategy_id"]),
        "strategy_version": str(entry["version"]),
        "resolved_semantic": deepcopy(entry.get("semantic") or {}),
        "direction_id": str(direction["direction_id"]),
        "direction_version": str(direction["version"]),
    }


def _resolve_strategy(value: Any, field: str, *, direction_id: str, registry: FramingStrategyRegistry = FRAMING_STRATEGY_REGISTRY) -> dict[str, Any]:
    if isinstance(value, Mapping):
        strategy_id = value.get("strategy_id") or value.get("approved_strategy_id") or value.get("approved_strategy") or value.get("recommended_strategy_id") or value.get("recommended_strategy")
        version = value.get("strategy_version") or value.get("approved_strategy_version") or value.get("recommended_strategy_version")
    else:
        strategy_id, version = value, None
    try:
        return registry.resolve(str(strategy_id or ""), str(version) if version not in (None, "") else None, direction_id=direction_id)
    except ValueError as exc:
        raise ValueError(f"Creative Brief {field} strategy 不支援: {strategy_id or 'empty'}") from exc


def _source_geometry(cfg: Mapping[str, Any], db: Path, project_id: int) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    counts = {"portrait": 0, "landscape": 0, "square": 0, "unknown": 0}
    ffprobe_path = str((cfg.get("ffprobe_path") if isinstance(cfg, Mapping) else "") or "ffprobe")
    for order, row in enumerate(project_videos(db, int(project_id)), 1):
        row = dict(row)
        source = Path(str(row.get("current_path") or "")).expanduser()
        probe = None
        probe_error = ""
        if source.is_file():
            try:
                probe = probe_media_metadata(ffprobe_path, source)
            except Exception as exc:  # metadata is recommendation evidence; no source mutation
                probe_error = str(exc)
        display_width = int(getattr(probe, "display_width", 0) or row.get("width") or 0)
        display_height = int(getattr(probe, "display_height", 0) or row.get("height") or 0)
        source_orientation = _orientation(display_width, display_height) if display_width and display_height else "unknown"
        counts[source_orientation] = counts.get(source_orientation, 0) + 1
        fingerprint = parse_source_fingerprint(row.get("source_fingerprint_json"))
        sources.append({
            "order": order,
            "project_media_uuid": str(row.get("project_media_uuid") or ""),
            "orientation": source_orientation,
            "display_width": display_width,
            "display_height": display_height,
            "display_ratio": round(float(getattr(probe, "display_ratio", 0.0) or (display_width / display_height if display_height else 0.0)), 12),
            "display_aspect_ratio": str(getattr(probe, "display_aspect_ratio", "") or ""),
            "sample_aspect_ratio": str(getattr(probe, "sample_aspect_ratio", "") or "1:1"),
            "rotation_degrees": int(getattr(probe, "rotation_degrees", 0) or 0),
            "display_matrix": str(getattr(probe, "display_matrix", "") or ""),
            "metadata_source": "vid-39-media-probe" if probe is not None else "project-media-metadata",
            "metadata_error": probe_error,
            "source_fingerprint": {
                key: fingerprint.get(key)
                for key in ("sha256", "size", "mtime_ns", "identity")
                if fingerprint.get(key) not in (None, "")
            },
        })
    return {
        "contract_version": "display-geometry-v2",
        "source_count": len(sources),
        "orientation_counts": counts,
        "sources": sources,
    }


def _recommendation(source_geometry: Mapping[str, Any], project_context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    counts = dict(source_geometry.get("orientation_counts") or {})
    portrait = int(counts.get("portrait") or 0)
    landscape = int(counts.get("landscape") or 0)
    target = "portrait" if portrait > landscape else "landscape"
    output_entry = _output_for_orientation(target)
    output = _materialized_output(output_entry)
    total = int(source_geometry.get("source_count") or 0)
    reason = (
        f"素材幾何摘要：{portrait} 支直向、{landscape} 支橫向"
        f"（共 {total} 支）；依主要素材方向建議使用 {output['aspect_ratio']}。"
    )
    if portrait == landscape and total:
        reason += " 方向比例相同，Round-1 預設採用橫向交付，仍需人工確認。"
    if not total or (portrait == 0 and landscape == 0):
        reason = "尚無足夠的 display geometry evidence，採用橫向 16:9 作為待確認預設。"
    return {
        "recommendation_version": CREATIVE_BRIEF_RECOMMENDATION_VERSION,
        "source": "deterministic_source_geometry",
        "output": output,
        "reason": reason,
        "source_orientation_summary": {
            "portrait": portrait,
            "landscape": landscape,
            "square": int(counts.get("square") or 0),
            "unknown": int(counts.get("unknown") or 0),
        },
        "registry_version": CREATIVE_BRIEF_REGISTRY_VERSION,
        "registry_hash": _registry_hash(),
        "framing_intent": {
            direction["direction_id"]: {
                **_materialized_strategy(
                    FRAMING_STRATEGY_REGISTRY.resolve("crop_reframe", direction_id=direction["direction_id"]),
                    direction,
                ),
                "recommended_strategy": "crop_reframe",
                "reason": (
                    "先嘗試保留主體的輕度 crop/reframe；若不安全，再由後續視覺流程使用 background treatment。"
                    if direction["source_orientation"] == "portrait"
                    else "優先犧牲左右邊緣做 crop/reframe；若不安全，再由後續視覺流程使用 background treatment。"
                ),
            }
            for direction in MISMATCH_DIRECTION_REGISTRY.entries()
        },
        "project_context": {
            "content_type": str((project_context or {}).get("content_type") or ""),
            "platform": str((project_context or {}).get("platform") or ""),
        },
    }


def _row_to_brief(row: Mapping[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {
            "schema_version": CREATIVE_BRIEF_SCHEMA_VERSION,
            "contract_version": CREATIVE_BRIEF_CONTRACT_VERSION,
            "brief_version": 0,
            "status": CREATIVE_BRIEF_STATUS_NEEDS_CONFIRMATION,
            "recommendation": {},
            "approved": {},
            "source_geometry": {},
            "visual_contract_hash": "",
            "story_relevant_hash": "",
        }
    def load_json(key: str) -> dict[str, Any]:
        try:
            value = json.loads(str(row.get(key) or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            value = {}
        return value if isinstance(value, dict) else {}
    return {
        "schema_version": int(row.get("schema_version") or CREATIVE_BRIEF_SCHEMA_VERSION),
        "contract_version": CREATIVE_BRIEF_CONTRACT_VERSION,
        "brief_version": int(row.get("brief_version") or 0),
        "status": str(row.get("status") or CREATIVE_BRIEF_STATUS_NEEDS_CONFIRMATION),
        "recommendation": load_json("recommendation_json"),
        "approved": load_json("approved_json"),
        "source_geometry": load_json("source_geometry_json"),
        "visual_contract_hash": str(row.get("visual_contract_hash") or ""),
        "story_relevant_hash": str(row.get("story_relevant_hash") or ""),
        "approved_at": str(row.get("approved_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
    }


def load_creative_brief(db: Path, project_id: int) -> dict[str, Any]:
    init_db(db)
    with connect(db) as con:
        row = con.execute("select * from creative_briefs where project_id=?", (int(project_id),)).fetchone()
    return _row_to_brief(dict(row) if row else None)


def ensure_creative_brief(cfg: Mapping[str, Any], db: Path, project_id: int) -> dict[str, Any]:
    current = load_creative_brief(db, project_id)
    if current.get("brief_version", 0) > 0 and current.get("recommendation"):
        return current
    from .database import project
    project_row = project(db, int(project_id))
    if not project_row:
        raise ValueError(f"project not found: {project_id}")
    geometry = _source_geometry(cfg, db, project_id)
    recommendation = _recommendation(geometry, dict(project_row))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect(db) as con:
        con.execute(
            """insert into creative_briefs(
                project_id, schema_version, brief_version, status,
                recommendation_json, approved_json, source_geometry_json,
                story_relevant_hash, visual_contract_hash, updated_at
            ) values(?, ?, 1, ?, ?, '{}', ?, '', '', ?)
            on conflict(project_id) do update set
                recommendation_json=excluded.recommendation_json,
                source_geometry_json=excluded.source_geometry_json,
                updated_at=excluded.updated_at""",
            (
                int(project_id), CREATIVE_BRIEF_SCHEMA_VERSION,
                CREATIVE_BRIEF_STATUS_NEEDS_CONFIRMATION,
                _canonical(recommendation), _canonical(geometry), now,
            ),
        )
    return load_creative_brief(db, project_id)


def recommend_creative_brief(cfg: Mapping[str, Any], db: Path, project_id: int, *, base_revision: int | None = None) -> dict[str, Any]:
    from .database import project
    project_row = project(db, int(project_id))
    if not project_row:
        raise ValueError(f"project not found: {project_id}")
    geometry = _source_geometry(cfg, db, project_id)
    recommendation = _recommendation(geometry, dict(project_row))
    current = load_creative_brief(db, project_id)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with project_commit(db, project_id, base_revision) as commit:
        with connect(db) as con:
            con.execute(
                """insert into creative_briefs(project_id, schema_version, brief_version, status, recommendation_json, approved_json, source_geometry_json, updated_at)
                values(?, ?, 1, ?, ?, ?, ?, ?)
                on conflict(project_id) do update set
                    brief_version=creative_briefs.brief_version+1,
                    recommendation_json=excluded.recommendation_json,
                    source_geometry_json=excluded.source_geometry_json,
                    updated_at=excluded.updated_at""",
                (
                    int(project_id), CREATIVE_BRIEF_SCHEMA_VERSION,
                    current.get("status") or CREATIVE_BRIEF_STATUS_NEEDS_CONFIRMATION,
                    _canonical(recommendation), _canonical(current.get("approved") or {}), _canonical(geometry), now,
                ),
            )
        # Recommendation refresh is metadata only; it must not stale Story.
        commit.record_changed(False)
    return load_creative_brief(db, project_id)


def _normalize_approved(
    brief: Mapping[str, Any],
    recommendation: Mapping[str, Any],
    *,
    output_registry: OutputContractRegistry = OUTPUT_CONTRACT_REGISTRY,
    direction_registry: MismatchDirectionRegistry = MISMATCH_DIRECTION_REGISTRY,
    strategy_registry: FramingStrategyRegistry = FRAMING_STRATEGY_REGISTRY,
    allow_disabled: bool = False,
) -> dict[str, Any]:
    output_input = brief.get("output") if isinstance(brief.get("output"), Mapping) else {}
    output_id = str(output_input.get("output_contract_id") or "").strip()
    output_version = str(output_input.get("output_contract_version") or "").strip() or None
    if output_id:
        output_entry = output_registry.resolve(output_id, output_version)
    else:
        # Compatibility for the first VID-26 payload shape.  The resolved
        # contract is immediately materialized with a stable ID/version.
        output_entry = _output_for_orientation(str(output_input.get("orientation") or ""), output_registry)
    if not output_entry.get("enabled_for_round1_ui") and not allow_disabled:
        raise ValueError(f"output contract disabled for Round-1 approval: {output_entry['output_contract_id']}")
    output = _materialized_output(output_entry)
    if str(output_input.get("aspect_ratio") or output["aspect_ratio"]) != output["aspect_ratio"]:
        raise ValueError("Creative Brief aspect_ratio 與 orientation 不一致")
    for key in ("width", "height"):
        if output_input.get(key, output[key]) != output[key]:
            raise ValueError("Creative Brief resolution 必須使用該 orientation 的 Round-1 預設值")
    framing_input = brief.get("framing_intent") if isinstance(brief.get("framing_intent"), Mapping) else {}
    framing: dict[str, Any] = {}
    for direction in direction_registry.entries():
        direction_id = direction["direction_id"]
        item = framing_input.get(direction_id) if isinstance(framing_input.get(direction_id), Mapping) else {}
        recommended = (recommendation.get("framing_intent") or {}).get(direction_id) or {}
        selected = item or recommended
        strategy_entry = _resolve_strategy(selected, direction_id, direction_id=direction_id, registry=strategy_registry)
        recommended_entry = _resolve_strategy(recommended or "auto_recommended", f"{direction_id} recommendation", direction_id=direction_id, registry=strategy_registry)
        framing[direction_id] = {
            "direction_id": direction_id,
            "direction_version": direction["version"],
            "source_orientation": direction["source_orientation"],
            "target_orientation": direction["target_orientation"],
            "recommended_strategy": str(recommended_entry["strategy_id"]),
            "recommended_strategy_id": str(recommended_entry["strategy_id"]),
            "recommended_strategy_version": str(recommended_entry["version"]),
            "approved_strategy": str(strategy_entry["strategy_id"]),
            "approved_strategy_id": str(strategy_entry["strategy_id"]),
            "approved_strategy_version": str(strategy_entry["version"]),
            "resolved_semantic": deepcopy(strategy_entry.get("semantic") or {}),
        }
    return {
        "contract_version": CREATIVE_BRIEF_CONTRACT_VERSION,
        "registry_version": CREATIVE_BRIEF_REGISTRY_VERSION,
        "registry_hash": _combined_registry_hash(output_registry, direction_registry, strategy_registry),
        "output": output,
        "framing_intent": framing,
        "approval_source": str(brief.get("approval_source") or "human_override"),
    }


def save_approved_creative_brief(cfg: Mapping[str, Any], db: Path, project_id: int, brief: Mapping[str, Any], *, base_revision: int | None = None, approval_source: str = "human_override") -> dict[str, Any]:
    current = ensure_creative_brief(cfg, db, project_id)
    approved = _normalize_approved({**dict(brief), "approval_source": approval_source}, current.get("recommendation") or {})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    visual_hash = _hash({
        "contract_version": CREATIVE_BRIEF_CONTRACT_VERSION,
        "registry_version": approved["registry_version"],
        "registry_hash": approved["registry_hash"],
        **{key: approved[key] for key in ("output", "framing_intent")},
    })
    with project_commit(db, project_id, base_revision) as commit:
        with connect(db) as con:
            con.execute(
                """update creative_briefs set brief_version=brief_version+1, status=?, approved_json=?, visual_contract_hash=?, approved_at=?, updated_at=? where project_id=?""",
                (CREATIVE_BRIEF_STATUS_APPROVED, _canonical(approved), visual_hash, now, now, int(project_id)),
            )
        # Visual/render-only contract; do not advance project revision or stale Story.
        commit.record_changed(False)
    return load_creative_brief(db, project_id)


def validate_materialized_approved(
    approved: Mapping[str, Any],
    *,
    output_registry: OutputContractRegistry = OUTPUT_CONTRACT_REGISTRY,
    direction_registry: MismatchDirectionRegistry = MISMATCH_DIRECTION_REGISTRY,
    strategy_registry: FramingStrategyRegistry = FRAMING_STRATEGY_REGISTRY,
) -> dict[str, Any]:
    errors: list[str] = []
    output = approved.get("output") if isinstance(approved.get("output"), Mapping) else {}
    try:
        output_entry = output_registry.resolve(str(output.get("output_contract_id") or ""), str(output.get("output_contract_version") or "") or None)
        expected_output = _materialized_output(output_entry)
        for key in ("orientation", "aspect_ratio", "width", "height", "render_profile_id"):
            if output.get(key) != expected_output[key]:
                errors.append(f"output {key} does not match resolved contract")
    except ValueError as exc:
        errors.append(str(exc))
    framing = approved.get("framing_intent") if isinstance(approved.get("framing_intent"), Mapping) else {}
    expected_directions = {item["direction_id"] for item in direction_registry.entries()}
    if set(framing) != expected_directions:
        errors.append("framing direction coverage does not match registry")
    for direction in direction_registry.entries():
        direction_id = direction["direction_id"]
        item = framing.get(direction_id) if isinstance(framing.get(direction_id), Mapping) else {}
        try:
            strategy = strategy_registry.resolve(
                str(item.get("approved_strategy_id") or item.get("approved_strategy") or ""),
                str(item.get("approved_strategy_version") or "") or None,
                direction_id=direction_id,
            )
            if item.get("resolved_semantic") != strategy.get("semantic", {}):
                errors.append(f"framing {direction_id} semantic materialization mismatch")
        except ValueError as exc:
            errors.append(str(exc))
    return {"valid": not errors, "errors": errors}


def approved_creative_brief(db: Path, project_id: int) -> dict[str, Any] | None:
    brief = load_creative_brief(db, project_id)
    if brief.get("status") != CREATIVE_BRIEF_STATUS_APPROVED or not brief.get("approved"):
        return None
    validation = validate_materialized_approved(brief["approved"])
    if not validation["valid"]:
        raise ValueError("approved Creative Brief contract invalid: " + "; ".join(validation["errors"]))
    return deepcopy(brief["approved"])


__all__ = [
    "CREATIVE_BRIEF_CONTRACT_VERSION",
    "CREATIVE_BRIEF_REGISTRY_VERSION",
    "CREATIVE_BRIEF_SCHEMA_VERSION",
    "CREATIVE_BRIEF_STATUS_APPROVED",
    "CREATIVE_BRIEF_STATUS_NEEDS_CONFIRMATION",
    "FRAMING_STRATEGY_REGISTRY",
    "MISMATCH_DIRECTION_REGISTRY",
    "OUTPUT_CONTRACT_REGISTRY",
    "FramingStrategyRegistry",
    "MismatchDirectionRegistry",
    "OutputContractRegistry",
    "approved_creative_brief",
    "creative_brief_api_payload",
    "creative_brief_options",
    "ensure_creative_brief",
    "load_creative_brief",
    "recommend_creative_brief",
    "save_approved_creative_brief",
    "validate_materialized_approved",
]

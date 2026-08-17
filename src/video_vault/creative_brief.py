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
CREATIVE_BRIEF_STATUS_NEEDS_CONFIRMATION = "needs_confirmation"
CREATIVE_BRIEF_STATUS_APPROVED = "approved"
ALLOWED_ORIENTATIONS = {"landscape", "portrait"}
ALLOWED_STRATEGIES = {
    "auto_recommended",
    "crop_reframe",
    "background_treatment",
    "preserve_full_frame",
}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _orientation(width: int, height: int) -> str:
    if width > height:
        return "landscape"
    if height > width:
        return "portrait"
    return "square"


def _output_for_orientation(orientation: str) -> dict[str, Any]:
    key = str(orientation or "").strip().lower()
    if key == "landscape":
        return {
            "orientation": "landscape",
            "aspect_ratio": "16:9",
            "width": 1920,
            "height": 1080,
            "render_profile_id": "final_1080p",
        }
    if key == "portrait":
        return {
            "orientation": "portrait",
            "aspect_ratio": "9:16",
            "width": 1080,
            "height": 1920,
            "render_profile_id": "final_1080p_portrait",
        }
    raise ValueError("Creative Brief output orientation 必須是 landscape 或 portrait")


def _strategy(value: Any, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in ALLOWED_STRATEGIES:
        raise ValueError(f"Creative Brief {field} strategy 不支援: {normalized or 'empty'}")
    return normalized


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
    output = _output_for_orientation(target)
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
        "framing_intent": {
            "portrait_source_in_landscape": {
                "recommended_strategy": "crop_reframe",
                "reason": "先嘗試保留主體的輕度 crop/reframe；若不安全，再由後續視覺流程使用 background treatment。",
            },
            "landscape_source_in_portrait": {
                "recommended_strategy": "crop_reframe",
                "reason": "優先犧牲左右邊緣做 crop/reframe；若不安全，再由後續視覺流程使用 background treatment。",
            },
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


def _normalize_approved(brief: Mapping[str, Any], recommendation: Mapping[str, Any]) -> dict[str, Any]:
    output_input = brief.get("output") if isinstance(brief.get("output"), Mapping) else {}
    orientation = str(output_input.get("orientation") or "").strip().lower()
    output = _output_for_orientation(orientation)
    if str(output_input.get("aspect_ratio") or output["aspect_ratio"]) != output["aspect_ratio"]:
        raise ValueError("Creative Brief aspect_ratio 與 orientation 不一致")
    for key in ("width", "height"):
        if output_input.get(key, output[key]) != output[key]:
            raise ValueError("Creative Brief resolution 必須使用該 orientation 的 Round-1 預設值")
    framing_input = brief.get("framing_intent") if isinstance(brief.get("framing_intent"), Mapping) else {}
    framing: dict[str, Any] = {}
    for direction in ("portrait_source_in_landscape", "landscape_source_in_portrait"):
        item = framing_input.get(direction) if isinstance(framing_input.get(direction), Mapping) else {}
        strategy = _strategy(item.get("approved_strategy") or item.get("strategy") or item.get("recommended_strategy") or ((recommendation.get("framing_intent") or {}).get(direction) or {}).get("recommended_strategy"), f"{direction}")
        framing[direction] = {
            "recommended_strategy": str(((recommendation.get("framing_intent") or {}).get(direction) or {}).get("recommended_strategy") or "auto_recommended"),
            "approved_strategy": strategy,
        }
    return {
        "contract_version": CREATIVE_BRIEF_CONTRACT_VERSION,
        "output": output,
        "framing_intent": framing,
        "approval_source": str(brief.get("approval_source") or "human_override"),
    }


def save_approved_creative_brief(cfg: Mapping[str, Any], db: Path, project_id: int, brief: Mapping[str, Any], *, base_revision: int | None = None, approval_source: str = "human_override") -> dict[str, Any]:
    current = ensure_creative_brief(cfg, db, project_id)
    approved = _normalize_approved({**dict(brief), "approval_source": approval_source}, current.get("recommendation") or {})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    visual_hash = _hash({"contract_version": CREATIVE_BRIEF_CONTRACT_VERSION, **{key: approved[key] for key in ("output", "framing_intent")}})
    with project_commit(db, project_id, base_revision) as commit:
        with connect(db) as con:
            con.execute(
                """update creative_briefs set brief_version=brief_version+1, status=?, approved_json=?, visual_contract_hash=?, approved_at=?, updated_at=? where project_id=?""",
                (CREATIVE_BRIEF_STATUS_APPROVED, _canonical(approved), visual_hash, now, now, int(project_id)),
            )
        # Visual/render-only contract; do not advance project revision or stale Story.
        commit.record_changed(False)
    return load_creative_brief(db, project_id)


def approved_creative_brief(db: Path, project_id: int) -> dict[str, Any] | None:
    brief = load_creative_brief(db, project_id)
    if brief.get("status") != CREATIVE_BRIEF_STATUS_APPROVED or not brief.get("approved"):
        return None
    return deepcopy(brief["approved"])


__all__ = [
    "ALLOWED_ORIENTATIONS",
    "ALLOWED_STRATEGIES",
    "CREATIVE_BRIEF_CONTRACT_VERSION",
    "CREATIVE_BRIEF_SCHEMA_VERSION",
    "CREATIVE_BRIEF_STATUS_APPROVED",
    "CREATIVE_BRIEF_STATUS_NEEDS_CONFIRMATION",
    "approved_creative_brief",
    "ensure_creative_brief",
    "load_creative_brief",
    "recommend_creative_brief",
    "save_approved_creative_brief",
]

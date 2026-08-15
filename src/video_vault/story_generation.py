"""Text-only project story generation with validation, cache and safe publish."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.request
from typing import Any, Mapping, Protocol
from uuid import uuid4

from .config import parse_bool
from .database import connect, init_db, project
from .project import project_dir
from .project_lifecycle import CancellationRequested, ProjectRevisionConflict, check_base_revision, current_revision, project_commit
from .story_input import STORY_INPUT_PROMPT_VERSION, build_story_input_snapshot
from .story_profiles import load_creator_profile, load_project_story_settings, story_profile_definition
from .story_calibration import calibration_for_profile
from .storyboard import load_storyboard, update_storyboard
from .story_context_budget import (
    DEFAULT_RESERVED_OUTPUT_TOKENS,
    context_budget_error_message,
    preflight_story_context,
)
from .lmstudio_runtime import LMStudioRuntimeProvisioner, resolve_lmstudio_runtime_context


STORY_OUTPUT_SCHEMA_VERSION = 1
STORY_PROMPT_VERSION = "project-story-v1"
STORY_CACHE_CONTRACT_VERSION = "story-cache-v2"
DEFAULT_REASONING_EFFORT = "none"
SUPPORTED_REASONING_EFFORTS = {"none", "low", "medium", "high"}
STORY_GENERATION_STATUSES = {"queued", "running", "validating", "publishing", "succeeded", "failed", "cancelled", "interrupted"}
STORY_SYSTEM_PROMPT = (
    "你是 video-vault-ai 的 project story planner。只能輸出嚴格 JSON。"
    "只能引用輸入提供的 segment_uuid，不得發明事件、地點、參數或不存在的內容。"
    "所有 human_override.include=true 的 segment 必須 exactly once 出現在 chapters[].segment_uuids，"
    "或依輸入既有 duplicate_group 契約出現在 suppressed_segments；不得無聲省略。"
    "suppressed_segments 的 representative 必須 exactly 出現在 chapter，且與被 suppress segment 屬於同一 duplicate_group。"
    "不要要求圖片，不要輸出 frame bytes、image_url 或 base64。"
    "不得決定 approval 或 render。咖啡、抹茶與烘豆不得自行寫成教學、業配或虛構專業參數。"
)

_STORY_OUTPUT_JSON_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "project_story_output",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "schema_version": {"type": "integer"},
                "project_summary": {"type": "string"},
                "story_profile": {
                    "type": "string",
                    "enum": ["travel_diary", "coffee_matcha_diary", "roasting_diary", "general_diary"],
                },
                "chapters": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "title": {"type": "string"},
                            "purpose": {"type": "string"},
                            "segment_uuids": {"type": "array", "items": {"type": "string"}},
                            "pacing_intent": {"type": "string"},
                            "transition_intent": {"type": "string"},
                            "natural_audio_intent": {"type": "string"},
                            "title_card_suggestion": {"type": "string"},
                            "notes": {"type": "string"},
                            "confidence": {"type": "number"},
                            "needs_review_reasons": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": [
                            "title", "purpose", "segment_uuids", "pacing_intent", "transition_intent",
                            "natural_audio_intent", "title_card_suggestion", "notes", "confidence", "needs_review_reasons",
                        ],
                    },
                },
                "overall_confidence": {"type": "number"},
                "needs_review_reasons": {"type": "array", "items": {"type": "string"}},
                "suppressed_segments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "segment_uuid": {"type": "string"},
                            "representative_segment_uuid": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": ["segment_uuid", "representative_segment_uuid", "reason"],
                    },
                },
            },
            "required": [
                "schema_version", "project_summary", "story_profile", "chapters", "overall_confidence",
                "needs_review_reasons", "suppressed_segments",
            ],
        },
    },
}


class StoryGenerationError(RuntimeError):
    pass


class StoryValidationError(StoryGenerationError):
    pass


class StorySemanticValidationError(StoryValidationError):
    """A structurally valid Story output violates the project semantic contract."""

    def __init__(self, message: str, findings: Mapping[str, Any]):
        super().__init__(message)
        self.findings = dict(findings)


class StoryContextBudgetError(StoryGenerationError):
    """A StoryInput request was blocked before network generation."""

    def __init__(self, message: str, budget: Mapping[str, Any]):
        super().__init__(message)
        self.budget = dict(budget)
        self.audit = {
            "calls": 0,
            "retries": 0,
            "generation_post_calls": 0,
            "request_budgets": [{"attempt": 1, "budget": dict(budget)}],
            "context_budget": dict(budget),
            "runtime_context": dict(budget.get("context_metadata") or {}),
            "error": message,
        }


class StoryProviderHTTPError(StoryGenerationError):
    """An HTTP failure with a safe, structured provider audit."""

    def __init__(self, message: str, audit: Mapping[str, Any]):
        super().__init__(message)
        self.audit = dict(audit)


class StoryRuntimeCleanupError(StoryGenerationError):
    """An app-owned local runtime could not be proven cleaned up safely."""

    def __init__(self, message: str, audit: Mapping[str, Any]):
        super().__init__(message)
        self.audit = dict(audit)


class StoryProvider(Protocol):
    provider: str
    model: str

    def context_metadata(self) -> Mapping[str, Any]:
        """Return model context capacity and its auditable source."""

    def cache_contract_metadata(self) -> Mapping[str, Any]:
        """Return deterministic metadata for the provider request contract."""

    def generate_story(self, snapshot: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return parsed model output and the provider response for audit."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _context_length_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _reasoning_effort(value: Any) -> str:
    normalized = str(value or DEFAULT_REASONING_EFFORT).strip().lower()
    if normalized not in SUPPORTED_REASONING_EFFORTS:
        raise StoryGenerationError(
            f"不支援的 Story reasoning_effort：{normalized}；可用值為 "
            + ", ".join(sorted(SUPPORTED_REASONING_EFFORTS))
        )
    return normalized


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _provider_http_error(exc: urllib.error.HTTPError, *, provider: str, model: str) -> StoryProviderHTTPError:
    try:
        raw_body = exc.read().decode("utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        raw_body = ""
    payload: Mapping[str, Any] = {}
    try:
        parsed = json.loads(raw_body) if raw_body else {}
        if isinstance(parsed, Mapping):
            payload = parsed
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    raw_error = payload.get("error")
    error = raw_error if isinstance(raw_error, Mapping) else payload
    provider_type = str(error.get("type") or error.get("code") or "")
    provider_message = str(raw_error if isinstance(raw_error, str) else error.get("message") or "").strip()
    classification_text = " ".join((provider_type, provider_message, raw_body[:1000])).lower()
    overflow_markers = (
        "exceed_context_size",
        "context_length_exceeded",
        "context window",
        "context length",
        "available context",
        "n_ctx",
        "too many tokens",
    )
    error_code = "provider_context_overflow" if any(marker in classification_text for marker in overflow_markers) else "provider_http_error"
    status = int(exc.code)
    audit = {
        "error_code": error_code,
        "http_status": status,
        "provider": str(provider),
        "model": str(model),
        "provider_error_type": provider_type,
        "provider_error_message": provider_message[:1000],
        "provider_response_classified": True,
    }
    if error_code == "provider_context_overflow":
        message = f"本地文字模型 context overflow（HTTP {status}）：{provider_message or provider_type or 'context capacity exceeded'}"
    else:
        message = f"本地文字模型 HTTP {status}：{provider_message or provider_type or exc.reason}"
    return StoryProviderHTTPError(message, audit)


def story_cache_key(
    snapshot: Mapping[str, Any],
    *,
    provider: str,
    model: str,
    prompt_version: str = STORY_PROMPT_VERSION,
    schema_version: int = STORY_OUTPUT_SCHEMA_VERSION,
    provider_contract_version: str = STORY_CACHE_CONTRACT_VERSION,
    provider_contract_metadata: Mapping[str, Any] | None = None,
) -> str:
    provider_contract = dict(provider_contract_metadata or {})
    provider_contract.setdefault("cache_contract_version", str(provider_contract_version))
    payload = {
        "input_hash": str(snapshot.get("input_hash") or ""),
        "provider": str(provider),
        "model": str(model),
        "prompt_version": str(prompt_version),
        "schema_version": int(schema_version),
        "provider_request_contract": provider_contract,
    }
    return "story_" + hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _provider_cache_contract(provider: StoryProvider) -> dict[str, Any]:
    method = getattr(provider, "cache_contract_metadata", None)
    if not callable(method):
        raise StoryGenerationError("Story provider 缺少 deterministic cache contract metadata，generation fail closed")
    metadata = method()
    if not isinstance(metadata, Mapping):
        raise StoryGenerationError("Story provider cache contract metadata 格式錯誤，generation fail closed")
    return dict(metadata)


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def _story_root(cfg: Mapping[str, Any], project_id: int) -> Path:
    path = project_dir(dict(cfg), int(project_id)) / "story"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _generation_dir(cfg: Mapping[str, Any], project_id: int, generation_uuid: str) -> Path:
    path = _story_root(cfg, project_id) / "generations" / generation_uuid
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_dir(cfg: Mapping[str, Any], project_id: int) -> Path:
    path = _story_root(cfg, project_id) / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _parse_json_field(row: Mapping[str, Any], key: str, default: Any) -> Any:
    raw = row.get(key)
    if not raw:
        return default
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _story_audit(
    row: Mapping[str, Any],
    raw: Mapping[str, Any],
    normalized: Mapping[str, Any],
    review: Mapping[str, Any],
) -> dict[str, Any]:
    """Expose safe raw/normalized/effective audit metadata without raw model text."""
    raw_audit = raw.get("provider_audit") if isinstance(raw.get("provider_audit"), Mapping) else {}
    normalized_chapters = [item for item in normalized.get("chapters") or [] if isinstance(item, Mapping)]
    effective_chapters = [item for item in (review.get("chapters") or normalized_chapters) if isinstance(item, Mapping)]
    normalized_segments = [
        str(segment_id)
        for chapter in normalized_chapters
        for segment_id in chapter.get("segment_uuids") or []
    ]
    effective_segments = [
        str(segment_id)
        for chapter in effective_chapters
        for segment_id in chapter.get("segment_uuids") or []
    ]
    return {
        "raw": {
            "provider": str(row.get("provider") or ""),
            "model": str(row.get("model") or ""),
            "input_hash": str(row.get("input_hash") or ""),
            "schema_version": int(row.get("schema_version") or 0),
            "provider_audit": dict(raw_audit),
        },
        "normalized": {
            "schema_version": int(normalized.get("schema_version") or row.get("schema_version") or 0),
            "project_summary_present": bool(str(normalized.get("project_summary") or "").strip()),
            "chapter_count": len(normalized_chapters),
            "segment_count": len(normalized_segments),
            "segment_uuids": normalized_segments,
            "suppressed_count": len(normalized.get("suppressed_segments") or []),
            "validation_status": str(((row.get("validation") or {}).get("status") or "unknown")),
        },
        "effective": {
            "source": "human" if str(review.get("source") or "") == "human" else "normalized",
            "locked": bool(review.get("locked", False)),
            "chapter_count": len(effective_chapters),
            "segment_count": len(effective_segments),
            "segment_uuids": effective_segments,
            "suppressed_count": len(review.get("suppressed_segments") or normalized.get("suppressed_segments") or []),
        },
    }


def _public_generation(row: Mapping[str, Any], *, include_internal: bool = False) -> dict[str, Any]:
    result = dict(row)
    for key in ("input_snapshot_json", "raw_response_json", "normalized_response_json", "review_state_json", "validation_json"):
        result[key.removesuffix("_json")] = _parse_json_field(row, key, {})
        result.pop(key, None)
    result["story_audit"] = _story_audit(
        result,
        result.get("raw_response") or {},
        result.get("normalized_response") or {},
        result.get("review_state") or {},
    )
    if not include_internal:
        result["input_snapshot"] = {"input_hash": result.get("input_snapshot", {}).get("input_hash", ""), "schema_version": result.get("input_snapshot", {}).get("schema_version", 0)}
        raw = result.get("raw_response") or {}
        if isinstance(raw, Mapping) and isinstance(raw.get("provider_audit"), Mapping):
            result["provider_audit"] = dict(raw["provider_audit"])
        result.pop("raw_response", None)
    return result


def list_story_generations(db: Path, project_id: int, *, include_internal: bool = False) -> list[dict[str, Any]]:
    init_db(db)
    with connect(db) as con:
        rows = con.execute("select * from story_generations where project_id=? order by generation desc, id desc", (int(project_id),)).fetchall()
    return [_public_generation(dict(row), include_internal=include_internal) for row in rows]


def project_story_detail(cfg: Mapping[str, Any], db: Path, project_id: int) -> dict[str, Any]:
    row = project(db, int(project_id))
    if not row:
        return {}
    settings = load_project_story_settings(cfg, db, int(project_id))
    creator = load_creator_profile(cfg)
    profile = story_profile_definition(str(settings.get("profile_id") or "general_diary"))
    generations = list_story_generations(db, int(project_id), include_internal=False)
    current_uuid = str(row["current_story_generation_uuid"] or "")
    current = next((item for item in generations if str(item.get("story_generation_uuid")) == current_uuid), None)
    try:
        current_input_hash = str(build_story_input_snapshot(cfg, db, project_id).get("input_hash") or "")
    except (OSError, TypeError, ValueError):
        current_input_hash = ""
    current_is_stale = bool(current and current_input_hash and current.get("input_hash") != current_input_hash)
    generation_archived_after_apply = bool(current_is_stale and current and current.get("applied_at"))
    current_generation_state = (
        "generation_archived_after_apply"
        if generation_archived_after_apply
        else "stale_before_apply"
        if current_is_stale
        else "current"
    )
    return {
        "settings": settings,
        "creator_profile": creator,
        "story_profile": profile,
        "generations": generations,
        "current_generation": current,
        "current_story_generation_uuid": current_uuid,
        "last_successful_story_generation_uuid": str(row["last_successful_story_generation_uuid"] or ""),
        "current_input_hash": current_input_hash,
        "current_generation_is_stale": current_is_stale,
        "current_generation_state": current_generation_state,
        "generation_archived_after_apply": generation_archived_after_apply,
        "calibration": calibration_for_profile(cfg, db, str(settings.get("profile_id") or "general_diary")),
    }


def recover_interrupted_story_generations(db: Path) -> int:
    """Close generations left in an in-flight state after a process restart."""
    init_db(db)
    now = _now()
    with connect(db) as con:
        rows = con.execute(
            """select story_generation_uuid, project_id
               from story_generations
               where status in ('queued', 'running', 'validating', 'publishing')"""
        ).fetchall()
        for row in rows:
            con.execute(
                """update story_generations
                   set status='interrupted', finished_at=?,
                       error=case when coalesce(error, '')='' then '服務重新啟動，中斷未完成的故事生成' else error end
                   where story_generation_uuid=?""",
                (now, str(row["story_generation_uuid"])),
            )
            con.execute(
                """update projects
                   set current_story_generation_uuid=coalesce(nullif(last_successful_story_generation_uuid, ''), '')
                   where id=? and current_story_generation_uuid=?""",
                (int(row["project_id"]), str(row["story_generation_uuid"])),
            )
    return len(rows)


def get_story_generation(db: Path, generation_uuid: str, *, include_internal: bool = False) -> dict[str, Any]:
    init_db(db)
    with connect(db) as con:
        row = con.execute("select * from story_generations where story_generation_uuid=?", (str(generation_uuid),)).fetchone()
    if not row:
        raise ValueError(f"story generation not found: {generation_uuid}")
    return _public_generation(dict(row), include_internal=include_internal)


def _insert_generation(db: Path, values: Mapping[str, Any]) -> None:
    init_db(db)
    with connect(db) as con:
        con.execute(
            """insert into story_generations(
                story_generation_uuid, project_id, generation, status,
                base_project_revision, input_hash, input_snapshot_json,
                provider, model, prompt_version, schema_version,
                creator_profile_version, project_story_profile_version,
                raw_response_json, normalized_response_json, review_state_json,
                validation_json, created_at, finished_at, published_revision,
                previous_successful_generation_uuid, error
            ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                values["story_generation_uuid"], values["project_id"], values["generation"], values["status"],
                values["base_project_revision"], values["input_hash"], _canonical(values["input_snapshot"]),
                values["provider"], values["model"], values["prompt_version"], values["schema_version"],
                values["creator_profile_version"], values["project_story_profile_version"],
                _canonical(values.get("raw_response") or {}), _canonical(values.get("normalized_response") or {}),
                _canonical(values.get("review_state") or {}), _canonical(values.get("validation") or {}),
                values.get("created_at") or _now(), values.get("finished_at") or "", values.get("published_revision"),
                values.get("previous_successful_generation_uuid") or "", values.get("error") or "",
            ),
        )


def _update_generation(db: Path, generation_uuid: str, **fields: Any) -> None:
    if not fields:
        return
    encoded = {key: (_canonical(value) if key.endswith("_json") else value) for key, value in fields.items()}
    with connect(db) as con:
        con.execute(
            f"update story_generations set {', '.join(f'{key}=?' for key in encoded)} where story_generation_uuid=?",
            (*encoded.values(), str(generation_uuid)),
        )


class MockStoryProvider:
    provider = "mock"
    model = "deterministic-story-v1"

    def context_metadata(self) -> dict[str, Any]:
        return {
            "context_capacity_tokens": 1_000_000,
            "source": "built_in_mock_contract",
            "verified": True,
        }

    def cache_contract_metadata(self) -> dict[str, Any]:
        return {"provider_contract_version": "mock-story-v1"}

    def generate_story(self, snapshot: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        profile_id = str(snapshot.get("story_profile_id") or "general_diary")
        segments = list(snapshot.get("segments") or [])
        chapters: list[dict[str, Any]] = []
        buckets: dict[str, list[Mapping[str, Any]]] = {}
        for segment in segments:
            if not bool((segment.get("human_override") or {}).get("include", True)):
                continue
            if profile_id == "travel_diary":
                key = str(segment.get("time_of_day") or segment.get("activity") or "旅程")
            elif profile_id == "coffee_matcha_diary":
                key = str(segment.get("activity") or segment.get("shot_role") or "日記片段")
            elif profile_id == "roasting_diary":
                key = str(segment.get("activity") or segment.get("shot_role") or "烘焙紀錄")
            else:
                key = str(segment.get("activity") or segment.get("time_of_day") or "日記片段")
            buckets.setdefault(key, []).append(segment)
        for title, bucket in buckets.items():
            ids = [str(item["segment_uuid"]) for item in bucket]
            chapters.append({
                "title": title or "日記片段",
                "purpose": "依照素材順序保留場景與動作的日記段落",
                "segment_uuids": ids,
                "pacing_intent": str(snapshot.get("desired_pacing") or "自然、保留呼吸感"),
                "transition_intent": "以場景切換作為段落轉場",
                "natural_audio_intent": "保留可辨識的環境音與關鍵動作聲",
                "title_card_suggestion": title or "",
                "confidence": 0.75,
                "needs_review_reasons": ["mock provider 結果需要人工確認"],
            })
        output = {
            "schema_version": STORY_OUTPUT_SCHEMA_VERSION,
            "project_summary": str(snapshot.get("project_intent") or "依照素材內容整理的專案故事骨架"),
            "story_profile": profile_id,
            "chapters": chapters,
            "overall_confidence": 0.75 if chapters else 0.0,
            "needs_review_reasons": ["故事排序與章節仍需人工審核"],
        }
        return output, {"provider": self.provider, "model": self.model, "output": output}


class LocalTextStoryProvider:
    provider = "local_text"

    _ROOT_KEYS = {
        "schema_version", "project_summary", "story_profile", "chapters",
        "overall_confidence", "needs_review_reasons", "suppressed_segments",
    }
    _CHAPTER_KEYS = {
        "title", "purpose", "segment_uuids", "pacing_intent", "transition_intent",
        "natural_audio_intent", "title_card_suggestion", "notes", "confidence", "needs_review_reasons",
    }

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_seconds: float = 180.0,
        context_length: int | None = None,
        context_source: str = "unknown",
        reserved_output_tokens: int = DEFAULT_RESERVED_OUTPUT_TOKENS,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
        runtime_context_timeout_seconds: float = 5.0,
        runtime_context_resolver: Any | None = None,
        runtime_provisioning_enabled: bool = False,
        runtime_target_context_length: int | None = None,
        runtime_load_timeout_seconds: float = 180.0,
        runtime_cleanup_timeout_seconds: float = 30.0,
        runtime_provisioner: Any | None = None,
    ):
        self.base_url = str(base_url).rstrip("/")
        self.model = str(model)
        self.timeout_seconds = float(timeout_seconds)
        self.context_length = _context_length_or_none(context_length)
        self.context_source = str(context_source or "unknown")
        self.reserved_output_tokens = int(reserved_output_tokens or DEFAULT_RESERVED_OUTPUT_TOKENS)
        self.reasoning_effort = _reasoning_effort(reasoning_effort)
        self.runtime_context_timeout_seconds = float(runtime_context_timeout_seconds)
        self.runtime_context_resolver = runtime_context_resolver or resolve_lmstudio_runtime_context
        self.request_model = self.model
        self.generation_post_calls = 0
        self.last_runtime_context: dict[str, Any] = {}
        self.runtime_provisioner = runtime_provisioner or LMStudioRuntimeProvisioner(
            self.base_url,
            self.model,
            enabled=runtime_provisioning_enabled,
            target_context_length=runtime_target_context_length or self.context_length,
            load_timeout_seconds=runtime_load_timeout_seconds,
            cleanup_timeout_seconds=runtime_cleanup_timeout_seconds,
            runtime_context_timeout_seconds=self.runtime_context_timeout_seconds,
            runtime_context_resolver=self.runtime_context_resolver,
        )
        self.last_runtime_cleanup: dict[str, Any] = {}

    def context_metadata(self) -> dict[str, Any]:
        metadata = self.runtime_context_resolver(
            self.base_url,
            self.request_model,
            configured_context_length=self.context_length,
            configured_context_source=self.context_source,
            timeout_seconds=self.runtime_context_timeout_seconds,
            urlopen_fn=urllib.request.urlopen,
        )
        self.last_runtime_context = dict(metadata) if isinstance(metadata, Mapping) else {}
        return dict(self.last_runtime_context)

    def cache_contract_metadata(self) -> dict[str, Any]:
        return {
            "provider_contract_version": "local-text-story-v2",
            "reasoning_effort": self.reasoning_effort,
            "reserved_output_tokens": self.reserved_output_tokens,
            "strict_schema": True,
        }

    def ensure_runtime_context(
        self,
        *,
        required_context_tokens: int,
        current_metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        metadata = self.runtime_provisioner.ensure(
            required_context_tokens,
            current_metadata,
            generation_post_calls=self.generation_post_calls,
        )
        self.request_model = str(getattr(self.runtime_provisioner, "request_model", self.model) or self.model)
        self.last_runtime_context = dict(metadata) if isinstance(metadata, Mapping) else {}
        return dict(self.last_runtime_context)

    def cleanup_runtime_context(self) -> dict[str, Any]:
        cleanup = self.runtime_provisioner.cleanup()
        self.last_runtime_cleanup = dict(cleanup) if isinstance(cleanup, Mapping) else {
            "status": "blocked",
            "reason_code": "runtime_cleanup_response_malformed",
        }
        return dict(self.last_runtime_cleanup)

    def runtime_lifecycle_audit(self) -> dict[str, Any]:
        return {
            "configured_model": self.model,
            "request_model": self.request_model,
            "reasoning_effort": self.reasoning_effort,
            "runtime_provisioning": dict(getattr(self.runtime_provisioner, "last_evidence", {}) or {}),
            "runtime_cleanup": dict(self.last_runtime_cleanup),
        }

    def _strict_parse(self, content: Any) -> dict[str, Any]:
        try:
            output = json.loads(str(content))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StoryValidationError("文字模型沒有回傳合法 JSON") from exc
        if not isinstance(output, dict):
            raise StoryValidationError("文字模型輸出必須是 JSON object")
        unknown = sorted(set(output) - self._ROOT_KEYS)
        missing = sorted({"schema_version", "project_summary", "story_profile", "chapters", "overall_confidence"} - set(output))
        if unknown or missing:
            bits = []
            if unknown:
                bits.append("未知欄位：" + ", ".join(unknown))
            if missing:
                bits.append("缺少欄位：" + ", ".join(missing))
            raise StoryValidationError("文字模型 schema 不符合契約（" + "；".join(bits) + "）")
        if not isinstance(output.get("chapters"), list):
            raise StoryValidationError("文字模型 chapters 必須是陣列")
        for index, chapter in enumerate(output["chapters"], 1):
            if not isinstance(chapter, Mapping):
                raise StoryValidationError(f"文字模型 chapter {index} 必須是物件")
            unknown_chapter = sorted(set(chapter) - self._CHAPTER_KEYS)
            if unknown_chapter:
                raise StoryValidationError(f"文字模型 chapter {index} 含未知欄位：{', '.join(unknown_chapter)}")
        if "suppressed_segments" in output and not isinstance(output["suppressed_segments"], list):
            raise StoryValidationError("文字模型 suppressed_segments 必須是陣列")
        return output

    def _request(self, request_payload: Mapping[str, Any]) -> tuple[dict[str, Any], Any, float]:
        started = time.perf_counter()
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(request_payload, ensure_ascii=False).encode("utf-8"),
            headers={"content-type": "application/json", "accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise _provider_http_error(exc, provider=self.provider, model=self.request_model) from exc
        except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
            raise StoryGenerationError(f"本地文字模型不可用：{exc}") from exc
        content = (((raw.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        if isinstance(content, list):
            content = "".join(str(item.get("text") or item) if isinstance(item, Mapping) else str(item) for item in content)
        try:
            parsed = self._strict_parse(content)
        except StoryValidationError as exc:
            exc.raw_content = str(content)
            raise
        return parsed, raw, (time.perf_counter() - started) * 1000

    def _generate_story(self, snapshot: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        system = STORY_SYSTEM_PROMPT
        base_messages = [{"role": "system", "content": system}, {"role": "user", "content": _canonical(snapshot)}]
        attempts: list[dict[str, Any]] = []
        latencies: list[float] = []
        request_budgets: list[dict[str, Any]] = []
        semantic_attempts: list[dict[str, Any]] = []
        last_error: StoryValidationError | None = None
        for attempt in range(2):
            messages = list(base_messages)
            if attempt:
                if isinstance(last_error, StorySemanticValidationError):
                    corrective_retry_reason = {
                        "contract": "project_story_semantic_coverage",
                        "instruction": "修正 validation findings，重新輸出完整 JSON，不要解釋或省略 segment。",
                        "findings": dict(last_error.findings),
                    }
                    retry_message = (
                        "上一個輸出通過 strict JSON，但不符合 project semantic contract。"
                        "所有 expected included segment 必須 exactly once 出現在 chapter，"
                        "或依既有 duplicate_group 合法放入 suppressed_segments。"
                        "請只依照以下 machine-readable findings 修正，重新輸出完整 JSON，不要解釋：\n"
                        + _canonical(corrective_retry_reason)
                    )
                else:
                    retry_message = "上一個輸出不符合 strict schema。只修正 schema，重新輸出完整 JSON，不要解釋。"
                messages.extend([
                    {"role": "assistant", "content": attempts[-1].get("content", "")},
                    {"role": "user", "content": retry_message},
                ])
            budget = preflight_story_context(
                snapshot,
                self,
                system_prompt=STORY_SYSTEM_PROMPT,
                request_messages=messages,
                reserved_output_tokens=self.reserved_output_tokens,
            )
            request_budgets.append({"attempt": attempt + 1, "budget": budget})
            if not budget["request_allowed"]:
                error = StoryContextBudgetError(context_budget_error_message(budget), budget)
                error.audit = {
                    "calls": attempt,
                    "generation_post_calls": attempt,
                    "retries": attempt,
                    "request_budgets": request_budgets,
                    "context_budget": budget,
                    "blocked_retry_budget": budget if attempt > 0 else None,
                    "runtime_context": dict(budget.get("context_metadata") or {}),
                    **_semantic_audit_fields(semantic_attempts),
                    "error": str(error),
                }
                raise error
            payload = {
                "model": self.request_model,
                "temperature": 0,
                "max_tokens": self.reserved_output_tokens,
                "reasoning_effort": self.reasoning_effort,
                "response_format": _STORY_OUTPUT_JSON_SCHEMA,
                "messages": messages,
            }
            started = time.perf_counter()
            try:
                self.generation_post_calls += 1
                output, raw, latency = self._request(payload)
                latencies.append(round(latency, 3))
                try:
                    validate_story_output(output, snapshot)
                except StorySemanticValidationError as exc:
                    findings = dict(exc.findings)
                    semantic_attempts.append({"attempt": attempt + 1, "status": "failed", **findings})
                    last_error = exc
                    attempts.append({
                        "content": json.dumps(output, ensure_ascii=False, sort_keys=True),
                        "error": str(exc),
                        "semantic_findings": findings,
                    })
                    if attempt == 1:
                        break
                    continue
                semantic_attempts.append({
                    "attempt": attempt + 1,
                    "status": "passed",
                    **_story_semantic_findings(output, snapshot, []),
                })
                attempts.append({"content": json.dumps(output, ensure_ascii=False, sort_keys=True), "error": ""})
                audit = {
                    "calls": attempt + 1,
                    "generation_post_calls": attempt + 1,
                    "retries": attempt,
                    "call_latencies_ms": latencies,
                    "total_latency_ms": round(sum(latencies), 3),
                    "strict_schema": True,
                    "reasoning_effort": self.reasoning_effort,
                    "context_budget": budget,
                    "request_budgets": request_budgets,
                    **_semantic_audit_fields(semantic_attempts, _story_coverage_summary(output, snapshot)),
                }
                return output, {**dict(raw), "provider_audit": audit}
            except StorySemanticValidationError:
                raise
            except StoryValidationError as exc:
                last_error = exc
                latencies.append(round((time.perf_counter() - started) * 1000, 3))
                semantic_attempts.append({
                    "attempt": attempt + 1,
                    "status": "not_run",
                    "reason_code": "structural_validation_failed",
                })
                attempts.append({"content": str(getattr(exc, "raw_content", "")), "error": str(exc)})
                if attempt == 1:
                    break
            except StoryProviderHTTPError as exc:
                latencies.append(round((time.perf_counter() - started) * 1000, 3))
                exc.audit = {
                    **dict(exc.audit),
                    "calls": attempt + 1,
                    "generation_post_calls": attempt + 1,
                    "retries": attempt,
                    "call_latencies_ms": latencies,
                    "total_latency_ms": round(sum(latencies), 3),
                    "strict_schema": True,
                    "reasoning_effort": self.reasoning_effort,
                    "context_budget": budget,
                    "request_budgets": request_budgets,
                    "runtime_context": dict(budget.get("context_metadata") or {}),
                    **_semantic_audit_fields(semantic_attempts),
                }
                raise
            except StoryGenerationError as exc:
                latencies.append(round((time.perf_counter() - started) * 1000, 3))
                exc.audit = {
                    **dict(getattr(exc, "audit", {}) or {}),
                    "calls": attempt + 1,
                    "generation_post_calls": attempt + 1,
                    "retries": attempt,
                    "call_latencies_ms": latencies,
                    "total_latency_ms": round(sum(latencies), 3),
                    "strict_schema": True,
                    "context_budget": budget,
                    "request_budgets": request_budgets,
                    "runtime_context": dict(budget.get("context_metadata") or {}),
                    "reasoning_effort": self.reasoning_effort,
                    **_semantic_audit_fields(semantic_attempts),
                    "error": str(exc),
                }
                raise
        error = last_error or StoryValidationError("本地文字模型 strict schema 驗證失敗")
        error.audit = {
            "calls": 2,
            "generation_post_calls": 2,
            "retries": 1,
            "call_latencies_ms": latencies,
            "total_latency_ms": round(sum(latencies), 3),
            "strict_schema": True,
            "reasoning_effort": self.reasoning_effort,
            "request_budgets": request_budgets,
            **_semantic_audit_fields(semantic_attempts),
            "error": str(error),
        }
        raise error

    def generate_story(self, snapshot: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            output, raw = self._generate_story(snapshot)
        except Exception as exc:
            cleanup = self.cleanup_runtime_context()
            audit = dict(getattr(exc, "audit", {}) or {})
            audit.update(self.runtime_lifecycle_audit())
            audit["runtime_cleanup"] = cleanup
            exc.audit = audit
            raise

        cleanup = self.cleanup_runtime_context()
        provider_audit = dict(raw.get("provider_audit") or {})
        provider_audit.update(self.runtime_lifecycle_audit())
        provider_audit["runtime_cleanup"] = cleanup
        raw = {**dict(raw), "provider_audit": provider_audit}
        if cleanup.get("status") not in {"pass", "not_needed"}:
            raise StoryRuntimeCleanupError(
                "app-owned Story runtime cleanup 未能安全完成，generation fail closed",
                provider_audit,
            )
        return output, raw


def provider_from_config(cfg: Mapping[str, Any], provider_override: str | None = None) -> StoryProvider:
    story_cfg = dict(cfg.get("story") or {})
    provider = str(provider_override or story_cfg.get("provider") or "mock")
    if provider == "mock":
        return MockStoryProvider()
    if provider in {"local", "local_text", "lmstudio", "lm_studio"}:
        local = dict((cfg.get("ai") or {}).get("local") or {})
        base_url = str(story_cfg.get("base_url") or local.get("base_url") or "http://127.0.0.1:1234/v1")
        model = str(story_cfg.get("model") or local.get("model") or "")
        if not model:
            raise StoryGenerationError("未設定本地文字模型名稱")
        context_length = (
            story_cfg.get("context_length")
            or story_cfg.get("max_context_length")
            or story_cfg.get("context_capacity_tokens")
        )
        context_source = story_cfg.get("context_source")
        for metadata_key, source_name in (("model_metadata", "config.story.model_metadata"), ("model_context", "config.story.model_context")):
            metadata = story_cfg.get(metadata_key)
            if isinstance(metadata, Mapping):
                context_length = context_length or metadata.get("max_context_length") or metadata.get("context_length") or metadata.get("context_capacity_tokens")
                context_source = context_source or metadata.get("source") or source_name
        if context_length in (None, ""):
            context_length = local.get("context_length") or local.get("max_context_length") or local.get("context_capacity_tokens")
            context_source = context_source or ("config.ai.local.context_length" if context_length not in (None, "") else None)
        if context_length not in (None, "") and not context_source:
            context_source = "config.story.context_length"
        runtime_raw = story_cfg.get("runtime_provisioning")
        runtime_cfg = dict(runtime_raw) if isinstance(runtime_raw, Mapping) else {}
        runtime_target = (
            runtime_cfg.get("target_context_length")
            or runtime_cfg.get("context_length")
            or context_length
        )
        return LocalTextStoryProvider(
            base_url,
            model,
            float(story_cfg.get("timeout_seconds") or 180),
            context_length=_context_length_or_none(context_length),
            context_source=str(context_source or "unknown"),
            reserved_output_tokens=int(story_cfg.get("max_output_tokens") or DEFAULT_RESERVED_OUTPUT_TOKENS),
            reasoning_effort=_reasoning_effort(story_cfg.get("reasoning_effort")),
            runtime_context_timeout_seconds=float(story_cfg.get("runtime_context_timeout_seconds") or 5),
            runtime_provisioning_enabled=parse_bool(runtime_cfg.get("enabled"), default=False),
            runtime_target_context_length=_context_length_or_none(runtime_target),
            runtime_load_timeout_seconds=float(runtime_cfg.get("load_timeout_seconds") or 180),
            runtime_cleanup_timeout_seconds=float(runtime_cfg.get("cleanup_timeout_seconds") or 30),
        )
    raise StoryGenerationError(f"不支援的文字故事 provider：{provider}")


def _validate_confidence(value: Any, label: str, errors: list[str]) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        errors.append(f"{label} confidence 無效")
        return 0.0
    if not 0 <= confidence <= 1:
        errors.append(f"{label} confidence 必須介於 0 與 1")
    return confidence


def _story_coverage_summary(output: Mapping[str, Any], snapshot: Mapping[str, Any]) -> dict[str, Any]:
    expected_ids = {
        str(item.get("segment_uuid") or "")
        for item in snapshot.get("segments") or []
        if bool((item.get("human_override") or {}).get("include", True))
    }
    allowed_ids = {str(item.get("segment_uuid") or "") for item in snapshot.get("segments") or []}
    chapters = output.get("chapters") if isinstance(output.get("chapters"), list) else []
    used_sequence: list[str] = []
    for chapter in chapters:
        if not isinstance(chapter, Mapping) or not isinstance(chapter.get("segment_uuids"), list):
            continue
        used_sequence.extend(str(value).strip() for value in chapter.get("segment_uuids") or [] if str(value).strip())
    raw_suppressed = output.get("suppressed_segments") if isinstance(output.get("suppressed_segments"), list) else []
    suppressed_ids: set[str] = set()
    representative_ids: set[str] = set()
    for item in raw_suppressed:
        if not isinstance(item, Mapping):
            continue
        segment_id = str(item.get("segment_uuid") or "").strip()
        representative_id = str(item.get("representative_segment_uuid") or "").strip()
        if segment_id:
            suppressed_ids.add(segment_id)
        if representative_id:
            representative_ids.add(representative_id)
    used_ids = set(used_sequence)
    duplicate_ids = sorted({segment_id for segment_id in used_sequence if used_sequence.count(segment_id) > 1})
    invalid_ids = sorted((used_ids | suppressed_ids | representative_ids) - allowed_ids)
    conflict_ids = sorted(used_ids & suppressed_ids)
    missing_ids = sorted(expected_ids - used_ids - suppressed_ids)
    missing_representative_ids = sorted(representative_ids - used_ids)
    return {
        "expected_ids": sorted(expected_ids),
        "used_ids": sorted(used_ids),
        "suppressed_ids": sorted(suppressed_ids),
        "missing_ids": missing_ids,
        "duplicate_ids": duplicate_ids,
        "invalid_ids": invalid_ids,
        "conflict_ids": conflict_ids,
        "missing_representative_ids": missing_representative_ids,
    }


def _story_semantic_findings(
    output: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    errors: list[str],
) -> dict[str, Any]:
    summary = _story_coverage_summary(output, snapshot)
    reason_codes: list[str] = []
    if summary["missing_ids"]:
        reason_codes.append("missing_expected_segments")
    if summary["duplicate_ids"]:
        reason_codes.append("duplicate_segment_ids")
    if summary["invalid_ids"]:
        reason_codes.append("invalid_segment_ids")
    if summary["conflict_ids"]:
        reason_codes.append("chapter_suppressed_conflict")
    if summary["missing_representative_ids"]:
        reason_codes.append("suppressed_representative_missing")
    if errors and not reason_codes:
        reason_codes.append("semantic_validation_error")
    return {
        **summary,
        "reason_codes": reason_codes,
        "validation_errors": list(errors),
    }


def _semantic_audit_fields(
    semantic_attempts: list[Mapping[str, Any]],
    final_coverage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    first_failure = next(
        (item for item in semantic_attempts if str(item.get("status") or "") == "failed"),
        {},
    )
    if final_coverage is None:
        last_attempt = next(reversed(semantic_attempts), {}) if semantic_attempts else {}
        final_coverage = {
            key: list(last_attempt.get(key) or [])
            for key in (
                "expected_ids", "used_ids", "suppressed_ids", "missing_ids",
                "duplicate_ids", "invalid_ids", "conflict_ids", "missing_representative_ids",
            )
            if key in last_attempt
        }
    return {
        "semantic_validation_attempts": [dict(item) for item in semantic_attempts],
        "first_attempt_missing_ids": list(first_failure.get("missing_ids") or []),
        "first_attempt_duplicate_ids": list(first_failure.get("duplicate_ids") or []),
        "first_attempt_invalid_ids": list(first_failure.get("invalid_ids") or []),
        "corrective_retry_reason": "semantic_validation" if first_failure else "",
        "final_coverage": dict(final_coverage or {}),
    }


def validate_story_output(output: Mapping[str, Any], snapshot: Mapping[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    profile_id = str(snapshot.get("story_profile_id") or "")
    if int(output.get("schema_version") or 0) != STORY_OUTPUT_SCHEMA_VERSION:
        errors.append("story output schema version 不支援")
    if str(output.get("story_profile") or "") != profile_id:
        errors.append("story profile 與 input 不一致")
    if not str(output.get("project_summary") or "").strip():
        errors.append("缺少 project_summary")
    allowed_ids = {str(item.get("segment_uuid") or "") for item in snapshot.get("segments") or []}
    chapters = output.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        errors.append("故事至少需要一個 chapter")
        chapters = []
    try:
        overall = float(output.get("overall_confidence"))
        if not 0 <= overall <= 1:
            errors.append("overall_confidence 必須介於 0 與 1")
    except (TypeError, ValueError):
        errors.append("overall_confidence 無效")
    used: set[str] = set()
    suppressed: list[dict[str, str]] = []
    suppressed_ids: set[str] = set()
    raw_suppressed = output.get("suppressed_segments") or []
    if not isinstance(raw_suppressed, list):
        errors.append("suppressed_segments 必須是陣列")
        raw_suppressed = []
    segment_by_id = {str(item.get("segment_uuid") or ""): item for item in snapshot.get("segments") or []}
    for index, item in enumerate(raw_suppressed, 1):
        if not isinstance(item, Mapping):
            errors.append(f"suppressed_segments {index} 必須是 object")
            continue
        segment_id = str(item.get("segment_uuid") or "").strip()
        representative_id = str(item.get("representative_segment_uuid") or "").strip()
        if not segment_id or not representative_id or segment_id == representative_id:
            errors.append(f"suppressed_segments {index} 必須指定不同的 segment_uuid 與 representative_segment_uuid")
            continue
        if segment_id not in allowed_ids or representative_id not in allowed_ids:
            errors.append(f"suppressed_segments {index} 引用了不存在或跨專案 segment")
            continue
        source_group = str((segment_by_id.get(segment_id) or {}).get("duplicate_group") or "")
        representative_group = str((segment_by_id.get(representative_id) or {}).get("duplicate_group") or "")
        if not source_group or source_group != representative_group:
            errors.append(f"suppressed segment {segment_id} 必須與 representative 屬於同一 duplicate_group")
            continue
        if segment_id in suppressed_ids:
            errors.append(f"suppressed segment 重複：{segment_id}")
            continue
        suppressed_ids.add(segment_id)
        suppressed.append({
            "segment_uuid": segment_id,
            "representative_segment_uuid": representative_id,
            "reason": str(item.get("reason") or "duplicate"),
        })
    normalized_chapters: list[dict[str, Any]] = []
    for index, chapter in enumerate(chapters, 1):
        if not isinstance(chapter, Mapping):
            errors.append(f"chapter {index} 必須是 object")
            continue
        title = str(chapter.get("title") or "").strip()
        purpose = str(chapter.get("purpose") or "").strip()
        ids = chapter.get("segment_uuids")
        if not title or not purpose or not isinstance(ids, list) or not ids:
            errors.append(f"chapter {index} 缺少必要欄位")
            continue
        cleaned_ids = [str(item).strip() for item in ids]
        for segment_id in cleaned_ids:
            if segment_id not in allowed_ids:
                errors.append(f"chapter {index} 引用了不存在或跨專案 segment：{segment_id}")
            if segment_id in used:
                errors.append(f"segment UUID 重複出現在多個 chapter：{segment_id}")
            if segment_id in suppressed_ids:
                errors.append(f"segment UUID 不可同時出現在 chapter 與 suppressed_segments：{segment_id}")
            used.add(segment_id)
        confidence = _validate_confidence(chapter.get("confidence"), f"chapter {index}", errors)
        if "roast_parameters" in chapter or "development_ratio" in chapter or "roast_curve" in chapter:
            errors.append(f"chapter {index} 含未由輸入提供的烘焙參數")
        normalized_chapters.append({
            "title": title,
            "purpose": purpose,
            "segment_uuids": cleaned_ids,
            "pacing_intent": str(chapter.get("pacing_intent") or "").strip(),
            "transition_intent": str(chapter.get("transition_intent") or "").strip(),
            "natural_audio_intent": str(chapter.get("natural_audio_intent") or "").strip(),
            "title_card_suggestion": str(chapter.get("title_card_suggestion") or "").strip(),
            "notes": str(chapter.get("notes") or "").strip(),
            "confidence": confidence,
            "needs_review_reasons": [str(item) for item in chapter.get("needs_review_reasons") or []],
            "locked": bool(chapter.get("locked", False)),
        })
    if not used:
        errors.append("故事沒有可用 segment")
    expected_ids = {
        str(item.get("segment_uuid") or "")
        for item in snapshot.get("segments") or []
        if bool((item.get("human_override") or {}).get("include", True))
    }
    missing_ids = sorted(expected_ids - used - suppressed_ids)
    if missing_ids:
        errors.append(f"故事輸出遺漏可納入片段：{', '.join(missing_ids)}")
    used_ids = set(used)
    for item in suppressed:
        if item["representative_segment_uuid"] not in used_ids:
            errors.append(
                f"suppressed segment {item['segment_uuid']} 的 representative 必須出現在 chapter：{item['representative_segment_uuid']}"
            )
    if errors:
        raise StorySemanticValidationError(
            "；".join(errors),
            _story_semantic_findings(output, snapshot, errors),
        )
    return {
        "schema_version": STORY_OUTPUT_SCHEMA_VERSION,
        "project_summary": str(output["project_summary"]).strip(),
        "story_profile": profile_id,
        "chapters": normalized_chapters,
        "overall_confidence": float(output.get("overall_confidence") or 0),
        "needs_review_reasons": [str(item) for item in output.get("needs_review_reasons") or []],
        "suppressed_segments": suppressed,
    }


def _chapter_id(chapter: Mapping[str, Any], index: int) -> str:
    digest = hashlib.sha256(_canonical(list(chapter.get("segment_uuids") or [])).encode("utf-8")).hexdigest()
    return f"chapter_{digest[:16]}"


def normalize_story_output(output: Mapping[str, Any], snapshot: Mapping[str, Any], previous: Mapping[str, Any] | None = None) -> dict[str, Any]:
    normalized = validate_story_output(output, snapshot)
    previous_chapters = [item for item in (previous or {}).get("chapters") or [] if item.get("chapter_id")]
    old_exact: dict[tuple[str, ...], list[str]] = {}
    for item in previous_chapters:
        old_exact.setdefault(tuple(item.get("segment_uuids") or []), []).append(str(item.get("chapter_id") or ""))
    old_sets: dict[frozenset[str], list[Mapping[str, Any]]] = {}
    for item in previous_chapters:
        old_sets.setdefault(frozenset(item.get("segment_uuids") or []), []).append(item)
    chapters = []
    for index, chapter in enumerate(normalized["chapters"], 1):
        membership = frozenset(chapter["segment_uuids"])
        ordered = tuple(chapter["segment_uuids"])
        exact_candidates = old_exact.get(ordered) or []
        chapter_id = exact_candidates[0] if len(exact_candidates) == 1 else None
        needs_review = list(chapter.get("needs_review_reasons") or [])
        if len(exact_candidates) > 1:
            needs_review.append("章節 identity 對應不明確，需人工確認")
        elif not chapter_id:
            candidates = old_sets.get(membership) or []
            if len(candidates) == 1:
                chapter_id = str(candidates[0].get("chapter_id") or "")
            elif len(candidates) > 1:
                needs_review.append("章節 identity 對應不明確，需人工確認")
        chapter_id = chapter_id or _chapter_id(chapter, index)
        chapters.append({"chapter_id": chapter_id, "order": index, **chapter, "locked": bool(chapter.get("locked", False)), "needs_review_reasons": needs_review})
    return {**normalized, "chapters": chapters, "normalization_version": 1}


def _merge_locked_chapters(output: Mapping[str, Any], previous: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep exact locked human chapters while allowing new unlocked chapters to regenerate."""
    old_chapters = [dict(item) for item in (previous or {}).get("chapters") or [] if bool(item.get("locked"))]
    if not old_chapters:
        return dict(output)
    chapters = [dict(item) for item in output.get("chapters") or []]
    for locked in old_chapters:
        old_ids = set(str(item) for item in locked.get("segment_uuids") or [])
        match_index = next((index for index, item in enumerate(chapters) if set(str(value) for value in item.get("segment_uuids") or []) == old_ids), None)
        if match_index is not None:
            chapters[match_index] = {**chapters[match_index], **locked, "locked": True}
            continue
        for item in chapters:
            item["segment_uuids"] = [value for value in item.get("segment_uuids") or [] if str(value) not in old_ids]
        chapters = [item for item in chapters if item.get("segment_uuids")]
        locked["needs_review_reasons"] = [*(locked.get("needs_review_reasons") or []), "鎖定章節無法與新結果精確對應，已保留並需人工確認"]
        chapters.append(locked)
    return {**dict(output), "chapters": chapters}


def _cache_load(cache_dir: Path, key: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    metadata = cache_dir / f"{key}.json"
    raw_path = cache_dir / f"{key}.raw.json"
    normalized_path = cache_dir / f"{key}.normalized.json"
    if not (metadata.is_file() and raw_path.is_file() and normalized_path.is_file()):
        return None
    try:
        meta = json.loads(metadata.read_text(encoding="utf-8"))
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        normalized = json.loads(normalized_path.read_text(encoding="utf-8"))
        if meta.get("cache_key") != key or not isinstance(normalized, dict) or not isinstance(raw, dict):
            return None
        return raw, normalized
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _cache_store(cache_dir: Path, key: str, raw: Mapping[str, Any], normalized: Mapping[str, Any]) -> None:
    _atomic_json(cache_dir / f"{key}.raw.json", raw)
    _atomic_json(cache_dir / f"{key}.normalized.json", normalized)
    _atomic_json(cache_dir / f"{key}.json", {"cache_key": key, "schema_version": STORY_OUTPUT_SCHEMA_VERSION, "created_at": _now()})


def _provider_runtime_cleanup(provider: Any) -> dict[str, Any]:
    method = getattr(provider, "cleanup_runtime_context", None)
    if not callable(method):
        return {"status": "not_needed", "reason_code": "provider_has_no_owned_runtime"}
    try:
        result = method()
    except (OSError, TypeError, ValueError) as exc:
        return {
            "status": "blocked",
            "reason_code": "runtime_cleanup_failed",
            "error": f"{type(exc).__name__}: {exc}"[:500],
        }
    return dict(result) if isinstance(result, Mapping) else {
        "status": "blocked",
        "reason_code": "runtime_cleanup_response_malformed",
    }


def _provider_runtime_lifecycle(provider: Any) -> dict[str, Any]:
    method = getattr(provider, "runtime_lifecycle_audit", None)
    result = method() if callable(method) else {}
    return dict(result) if isinstance(result, Mapping) else {}


def generate_project_story(
    cfg: Mapping[str, Any],
    db: Path,
    project_id: int,
    *,
    base_revision: int | None = None,
    provider_override: str | None = None,
    force: bool = False,
    should_cancel: Any | None = None,
) -> dict[str, Any]:
    snapshot = build_story_input_snapshot(cfg, db, project_id)
    base = int(snapshot["project_revision"])
    check_base_revision(db, project_id, base_revision)
    provider = provider_from_config(cfg, provider_override)
    provider_cache_contract = _provider_cache_contract(provider)
    key = story_cache_key(
        snapshot,
        provider=provider.provider,
        model=provider.model,
        provider_contract_metadata=provider_cache_contract,
    )
    cache_identity = {
        "cache_key": key,
        "provider_request_contract": provider_cache_contract,
    }
    with connect(db) as con:
        generation = int(con.execute("select coalesce(max(generation), 0) + 1 from story_generations where project_id=?", (int(project_id),)).fetchone()[0])
        previous = con.execute("select last_successful_story_generation_uuid from projects where id=?", (int(project_id),)).fetchone()
    generation_uuid = str(uuid4())
    previous_uuid = str(previous[0] or "") if previous else ""
    previous_story: dict[str, Any] = {}
    if previous_uuid:
        with connect(db) as con:
            previous_row = con.execute("select normalized_response_json, review_state_json from story_generations where story_generation_uuid=?", (previous_uuid,)).fetchone()
        if previous_row:
            previous_story = _parse_json_field({"normalized_response_json": previous_row["normalized_response_json"]}, "normalized_response_json", {})
            previous_review = _parse_json_field({"review_state_json": previous_row["review_state_json"]}, "review_state_json", {})
            if previous_review.get("chapters"):
                previous_story = {**previous_story, "chapters": previous_review["chapters"]}
    values = {
        "story_generation_uuid": generation_uuid,
        "project_id": int(project_id),
        "generation": generation,
        "status": "running",
        "base_project_revision": base,
        "input_hash": snapshot["input_hash"],
        "input_snapshot": snapshot,
        "provider": provider.provider,
        "model": provider.model,
        "prompt_version": STORY_PROMPT_VERSION,
        "schema_version": STORY_OUTPUT_SCHEMA_VERSION,
        "creator_profile_version": int(snapshot.get("creator_profile_version") or 1),
        "project_story_profile_version": int(snapshot.get("story_profile_version") or 1),
        "previous_successful_generation_uuid": previous_uuid,
    }
    _insert_generation(db, values)
    try:
        story_cfg = dict(cfg.get("story") or {})
        context_budget = preflight_story_context(
            snapshot,
            provider,
            system_prompt=STORY_SYSTEM_PROMPT,
            reserved_output_tokens=int(story_cfg.get("max_output_tokens") or DEFAULT_RESERVED_OUTPUT_TOKENS),
        )
        _update_generation(
            db,
            generation_uuid,
            validation_json={
                "status": context_budget["status"],
                "context_budget": context_budget,
                "cache_identity": cache_identity,
            },
        )
        if not context_budget["request_allowed"]:
            raise StoryContextBudgetError(context_budget_error_message(context_budget), context_budget)
        if should_cancel and should_cancel():
            raise CancellationRequested("故事生成已取消")
        cached = None if force else _cache_load(_cache_dir(cfg, project_id), key)
        if cached:
            raw, normalized = cached
            normalized = normalize_story_output(_merge_locked_chapters(normalized, previous_story), snapshot, previous=previous_story)
            cache_hit = True
        else:
            output, raw = provider.generate_story(snapshot)
            _update_generation(db, generation_uuid, status="validating", raw_response_json=raw)
            if should_cancel and should_cancel():
                raise CancellationRequested("故事生成已取消")
            normalized = normalize_story_output(_merge_locked_chapters(output, previous_story), snapshot, previous=previous_story)
            _cache_store(_cache_dir(cfg, project_id), key, raw, normalized)
            cache_hit = False
        provider_audit = dict(raw.get("provider_audit") or {})
        provider_audit["cache_identity"] = cache_identity
        provider_audit["cache_hit"] = cache_hit
        raw = {**dict(raw), "provider_audit": provider_audit}
        runtime_cleanup = _provider_runtime_cleanup(provider)
        lifecycle = _provider_runtime_lifecycle(provider)
        if lifecycle:
            provider_audit = dict(raw.get("provider_audit") or {})
            provider_audit.update(lifecycle)
            provider_audit["runtime_cleanup"] = runtime_cleanup
            raw = {**dict(raw), "provider_audit": provider_audit}
        if runtime_cleanup.get("status") not in {"pass", "not_needed"}:
            raise StoryRuntimeCleanupError(
                "app-owned Story runtime cleanup 未能安全完成，publish fail closed",
                dict(raw.get("provider_audit") or {"runtime_cleanup": runtime_cleanup}),
            )
        generation_path = _generation_dir(cfg, project_id, generation_uuid)
        _atomic_json(generation_path / "input_snapshot.json", snapshot)
        _atomic_json(generation_path / "raw_response.json", raw)
        _atomic_json(generation_path / "normalized_response.json", normalized)
        validation = {
            "status": "passed",
            "cache_hit": cache_hit,
            "cache_key": key,
            "cache_identity": cache_identity,
            "context_budget": context_budget,
        }
        _atomic_json(generation_path / "validation.json", validation)
        _update_generation(db, generation_uuid, status="publishing", raw_response_json=raw, normalized_response_json=normalized, validation_json=validation)
        if should_cancel and should_cancel():
            raise CancellationRequested("故事生成已取消")
        with project_commit(db, project_id, base_revision=base) as commit:
            current = current_revision(db, project_id)
            if current != base:
                raise ProjectRevisionConflict(project_id, base, current)
            with connect(db) as con:
                pointer_update = con.execute(
                    "update projects set current_story_generation_uuid=?, last_successful_story_generation_uuid=? where id=?",
                    (generation_uuid, generation_uuid, int(project_id)),
                )
                if pointer_update.rowcount != 1:
                    raise StoryGenerationError("故事 generation publish 找不到專案，已 rollback")
                status_update = con.execute(
                    "update story_generations set status='succeeded', finished_at=?, published_revision=? where story_generation_uuid=?",
                    (_now(), base, generation_uuid),
                )
                if status_update.rowcount != 1:
                    raise StoryGenerationError("故事 generation publish 找不到 generation，已 rollback")
            commit.record_changed(False)
        result = get_story_generation(db, generation_uuid, include_internal=True)
        result["cache_key"] = key
        result["cache_hit"] = cache_hit
        return result
    except CancellationRequested as exc:
        cleanup = _provider_runtime_cleanup(provider)
        lifecycle = _provider_runtime_lifecycle(provider)
        fields: dict[str, Any] = {"status": "cancelled", "finished_at": _now(), "error": str(exc)}
        if lifecycle:
            fields["raw_response_json"] = {"provider_audit": {**lifecycle, "runtime_cleanup": cleanup}}
        _update_generation(db, generation_uuid, **fields)
        raise
    except Exception as exc:
        cleanup = _provider_runtime_cleanup(provider)
        audit = dict(getattr(exc, "audit", {}) or {})
        lifecycle = _provider_runtime_lifecycle(provider)
        if lifecycle:
            audit.update(lifecycle)
            audit["runtime_cleanup"] = cleanup
        fields = {"status": "failed", "finished_at": _now(), "error": str(exc)}
        if audit:
            fields["raw_response_json"] = {"provider_audit": audit}
            blocked_retry_budget = audit.get("blocked_retry_budget")
            if isinstance(blocked_retry_budget, Mapping):
                fields["validation_json"] = {
                    "status": "blocked",
                    "context_budget": dict(blocked_retry_budget),
                    "request_budgets": list(audit.get("request_budgets") or []),
                    "cache_identity": cache_identity,
                }
            elif audit.get("semantic_validation_attempts"):
                fields["validation_json"] = {
                    "status": "failed",
                    "context_budget": dict(audit.get("context_budget") or {}),
                    "request_budgets": list(audit.get("request_budgets") or []),
                    "cache_identity": cache_identity,
                    "semantic_validation_attempts": list(audit.get("semantic_validation_attempts") or []),
                    "first_attempt_missing_ids": list(audit.get("first_attempt_missing_ids") or []),
                    "first_attempt_duplicate_ids": list(audit.get("first_attempt_duplicate_ids") or []),
                    "first_attempt_invalid_ids": list(audit.get("first_attempt_invalid_ids") or []),
                    "corrective_retry_reason": str(audit.get("corrective_retry_reason") or ""),
                    "final_coverage": dict(audit.get("final_coverage") or {}),
                }
            elif audit.get("error_code"):
                fields["validation_json"] = {
                    "status": "failed",
                    "context_budget": dict(audit.get("context_budget") or {}),
                    "request_budgets": list(audit.get("request_budgets") or []),
                    "cache_identity": cache_identity,
                    "provider_error": {
                        "error_code": str(audit.get("error_code") or "provider_error"),
                        "http_status": audit.get("http_status"),
                        "provider_error_type": str(audit.get("provider_error_type") or ""),
                        "provider_error_message": str(audit.get("provider_error_message") or ""),
                    },
                }
        _update_generation(db, generation_uuid, **fields)
        raise


def _assert_generation_project(generation: Mapping[str, Any], project_id: int | None) -> None:
    if project_id is not None and int(generation.get("project_id") or 0) != int(project_id):
        raise StoryGenerationError("故事 generation 不屬於指定專案")


def _review_chapter_identity(
    submitted: Any,
    original: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Require application-owned chapter IDs and preserve them across edits/reorder."""
    if not isinstance(submitted, list):
        raise StoryValidationError("人工審核 chapters 必須是陣列")
    known = {str(item.get("chapter_id") or ""): dict(item) for item in original if str(item.get("chapter_id") or "")}
    if not known:
        raise StoryValidationError("故事 generation 缺少 app-owned chapter_id")
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for index, item in enumerate(submitted, 1):
        if not isinstance(item, Mapping):
            raise StoryValidationError(f"人工審核 chapter {index} 必須是 object")
        chapter_id = str(item.get("chapter_id") or "").strip()
        if not chapter_id or chapter_id not in known:
            raise StoryValidationError(f"人工審核 chapter {index} 的 chapter_id 不是 app-owned identity")
        if chapter_id in seen:
            raise StoryValidationError(f"人工審核 chapter_id 重複：{chapter_id}")
        seen.add(chapter_id)
        result.append({**dict(item), "chapter_id": chapter_id})
    if seen != set(known):
        missing = sorted(set(known) - seen)
        raise StoryValidationError("人工審核不可刪除既有章節 identity：" + ", ".join(missing))
    return result


def update_story_generation_review(
    db: Path,
    generation_uuid: str,
    review_state: Mapping[str, Any],
    *,
    project_id: int | None = None,
    base_revision: int | None = None,
) -> dict[str, Any]:
    generation = get_story_generation(db, generation_uuid, include_internal=True)
    _assert_generation_project(generation, project_id)
    if project_id is not None:
        check_base_revision(db, int(project_id), base_revision)
    snapshot = generation.get("input_snapshot") or {}
    original = generation.get("normalized_response") or {}
    submitted_for_boundary = review_state.get("chapters")
    allowed_ids = {str(item.get("segment_uuid") or "") for item in snapshot.get("segments") or []}
    if isinstance(submitted_for_boundary, list):
        for item in submitted_for_boundary:
            if not isinstance(item, Mapping):
                continue
            for segment_id in item.get("segment_uuids") or []:
                if str(segment_id) not in allowed_ids:
                    raise StoryValidationError(f"chapter 引用了不存在或跨專案 segment：{segment_id}")
    submitted_chapters = _review_chapter_identity(review_state.get("chapters", original.get("chapters") or []), original.get("chapters") or [])
    candidate = {**original, **dict(review_state), "chapters": submitted_chapters}
    reviewed = validate_story_output(candidate, snapshot)
    reviewed_by_id = {str(item.get("chapter_id")): item for item in submitted_chapters}
    original_by_id = {str(item.get("chapter_id")): item for item in original.get("chapters") or []}
    reviewed["chapters"] = [
        {**chapter, "chapter_id": str(submitted_chapters[index].get("chapter_id"))}
        for index, chapter in enumerate(reviewed["chapters"])
    ]
    for chapter_id, chapter in reviewed_by_id.items():
        if chapter_id not in original_by_id:
            raise StoryValidationError(f"人工審核 chapter_id 不是 app-owned identity：{chapter_id}")
    state = {
        "project_summary": reviewed["project_summary"],
        "chapters": reviewed["chapters"],
        "overall_confidence": reviewed["overall_confidence"],
        "needs_review_reasons": reviewed["needs_review_reasons"],
        "suppressed_segments": reviewed.get("suppressed_segments") or [],
        "locked": bool(review_state.get("locked", (generation.get("review_state") or {}).get("locked", False))),
    }
    state["source"] = "human"
    state["story_generation_uuid"] = generation_uuid
    state["base_project_revision"] = generation.get("base_project_revision")
    state["edited_at"] = _now()
    _update_generation(db, generation_uuid, review_state_json=state)
    return get_story_generation(db, generation_uuid, include_internal=True)


def _storyboard_state_from_generation(generation: Mapping[str, Any], existing: Mapping[str, Any]) -> dict[str, Any]:
    model = generation.get("normalized_response") or {}
    review = generation.get("review_state") or {}
    reviewed = {**model, **review}
    chapters = reviewed.get("chapters") or []
    old_groups = {str(item.get("title") or ""): dict(item) for item in existing.get("groups") or []}
    old_segments = {str(key): dict(value) for key, value in (existing.get("segments") or {}).items()}
    groups: list[dict[str, Any]] = []
    next_segments = dict(old_segments)
    for chapter_index, chapter in enumerate(chapters, 1):
        chapter_id = str(chapter.get("chapter_id") or _chapter_id(chapter, chapter_index))
        title = str(chapter.get("title") or f"章節 {chapter_index}")
        old_group = old_groups.get(title) or {}
        group_id = str(old_group.get("group_id") or f"story_{chapter_id}")
        groups.append({"group_id": group_id, "title": title, "category": "story", "order": chapter_index})
        for order, segment_id in enumerate(chapter.get("segment_uuids") or [], 1):
            segment_id = str(segment_id)
            old = dict(old_segments.get(segment_id) or {})
            if old.get("locked") or old.get("manual_group") or old.get("manual_order"):
                preserved = {**old}
                if not old.get("locked") and not old.get("manual_group"):
                    preserved["group_id"] = group_id
                if not old.get("locked") and not old.get("manual_order"):
                    preserved["order"] = order
                next_segments[segment_id] = preserved
                continue
            next_segments[segment_id] = {
                **old,
                "group_id": group_id,
                "order": order,
                "included": bool(old.get("included", True)),
                "locked": bool(old.get("locked", False)),
                "manual_group": bool(old.get("manual_group", False)),
                "manual_order": bool(old.get("manual_order", False)),
                "auto_group_id": group_id,
                "auto_order": order,
                "notes": str(old.get("notes") or ""),
            }
    existing_group_ids = {str(item.get("group_id") or "") for item in groups}
    preserved_group_ids = {
        str(item.get("group_id") or "")
        for item in next_segments.values()
        if (item.get("manual_group") or item.get("locked")) and str(item.get("group_id") or "")
    }
    for group in existing.get("groups") or []:
        group_id = str(group.get("group_id") or "")
        if group_id in preserved_group_ids and group_id not in existing_group_ids:
            groups.append({**dict(group), "order": len(groups) + 1})
            existing_group_ids.add(group_id)
    return {"schema_version": int(existing.get("schema_version") or 1), "groups": groups, "segments": next_segments}


def _restore_story_apply_state(
    db: Path,
    project_id: int,
    generation_uuid: str,
    generation_before: Mapping[str, Any],
    project_before: Mapping[str, Any] | None,
    rollback_files: Mapping[Path, bytes | None],
) -> None:
    """Compensate every Apply side effect when the success boundary fails."""
    for path, content in rollback_files.items():
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    with connect(db) as con:
        if project_before:
            project_fields = {key: value for key, value in dict(project_before).items() if key != "id"}
            con.execute(
                f"update projects set {', '.join(f'{key}=?' for key in project_fields)} where id=?",
                (*project_fields.values(), int(project_id)),
            )
        generation_fields = {key: value for key, value in dict(generation_before).items() if key != "id"}
        con.execute(
            f"update story_generations set {', '.join(f'{key}=?' for key in generation_fields)} where story_generation_uuid=?",
            (*generation_fields.values(), str(generation_uuid)),
        )


def apply_story_generation_to_storyboard(
    cfg: Mapping[str, Any],
    db: Path,
    project_id: int,
    generation_uuid: str,
    *,
    base_revision: int | None = None,
) -> dict[str, Any]:
    rollback_files: dict[Path, bytes | None] | None = None
    generation_before: Mapping[str, Any] | None = None
    project_before: Mapping[str, Any] | None = None
    rollback_ready = False
    result_payload: dict[str, Any] | None = None
    commit = None
    try:
        # Use the project lock for the complete mutation and marker boundary.
        # Duplicate idempotence intentionally checks base_revision only after
        # seeing whether this generation was already applied.
        with project_commit(db, int(project_id), None) as active_commit:
            commit = active_commit
            generation = get_story_generation(db, generation_uuid, include_internal=True)
            _assert_generation_project(generation, project_id)
            if generation.get("applied_at"):
                existing = load_storyboard(dict(cfg), int(project_id))
                if existing is None:
                    raise ValueError("尚未建立 storyboard，請先到分鏡審核建立分鏡後再套用故事")
                result_payload = {
                    "generation": get_story_generation(db, generation_uuid, include_internal=False),
                    "storyboard": existing,
                    "render_changed": False,
                    "approval_invalidated": False,
                    "apply_succeeded": True,
                    "apply_state": "apply_succeeded",
                    "generation_state": "generation_archived_after_apply",
                    "idempotent": True,
                    "project_revision": current_revision(db, int(project_id)),
                }
            else:
                check_base_revision(db, int(project_id), base_revision)
                if str(generation.get("status")) != "succeeded":
                    raise StoryGenerationError("只有成功且已驗證的故事 generation 可以套用")
                current_snapshot = build_story_input_snapshot(cfg, db, int(project_id))
                if str(generation.get("input_hash") or "") != str(current_snapshot.get("input_hash") or ""):
                    raise StoryGenerationError("故事 generation 的 input_hash 已過期，請重新計算目前 StoryInputSnapshot 後再套用")
                existing = load_storyboard(dict(cfg), int(project_id))
                if existing is None:
                    raise ValueError("尚未建立 storyboard，請先到分鏡審核建立分鏡後再套用故事")
                state = _storyboard_state_from_generation(generation, existing)
                folder = project_dir(dict(cfg), int(project_id))
                rollback_files = {
                    folder / name: (folder / name).read_bytes() if (folder / name).is_file() else None
                    for name in ("storyboard.json", "review_status.json", "project_plan.json")
                }
                with connect(db) as con:
                    generation_before = dict(con.execute(
                        "select * from story_generations where story_generation_uuid=?",
                        (str(generation_uuid),),
                    ).fetchone() or {})
                project_before = dict(project(db, int(project_id)) or {})
                rollback_ready = True
                try:
                    result = update_storyboard(dict(cfg), db, int(project_id), state, return_result=True, base_revision=base_revision)
                    applied_revision = active_commit.base_revision + (1 if result["render_changed"] else 0)
                    _update_generation(
                        db,
                        generation_uuid,
                        applied_to_storyboard_revision=applied_revision,
                        applied_at=_now(),
                    )
                except Exception:
                    _restore_story_apply_state(db, int(project_id), generation_uuid, generation_before, project_before, rollback_files)
                    rollback_ready = False
                    raise
                result_payload = {
                    "storyboard": result["state"],
                    "render_changed": result["render_changed"],
                    "approval_invalidated": result["approval_invalidated"],
                    "apply_succeeded": True,
                    "apply_state": "apply_succeeded",
                    "generation_state": "generation_archived_after_apply",
                    "idempotent": False,
                }
        rollback_ready = False
    except Exception:
        if rollback_ready and rollback_files is not None and generation_before is not None:
            _restore_story_apply_state(db, int(project_id), generation_uuid, generation_before, project_before, rollback_files)
        raise

    if result_payload is None or commit is None:
        raise StoryGenerationError("故事 Apply 未建立成功結果")
    if not result_payload.get("idempotent"):
        result_payload["generation"] = get_story_generation(db, generation_uuid, include_internal=False)
        result_payload["project_revision"] = int(commit.new_revision if commit.new_revision is not None else current_revision(db, int(project_id)))
    return result_payload


__all__ = [
    "LocalTextStoryProvider",
    "MockStoryProvider",
    "STORY_GENERATION_STATUSES",
    "STORY_OUTPUT_SCHEMA_VERSION",
    "STORY_PROMPT_VERSION",
    "STORY_CACHE_CONTRACT_VERSION",
    "StoryGenerationError",
    "StoryContextBudgetError",
    "StoryProviderHTTPError",
    "StoryRuntimeCleanupError",
    "StorySemanticValidationError",
    "StoryValidationError",
    "apply_story_generation_to_storyboard",
    "generate_project_story",
    "get_story_generation",
    "list_story_generations",
    "normalize_story_output",
    "provider_from_config",
    "project_story_detail",
    "story_cache_key",
    "update_story_generation_review",
    "validate_story_output",
]

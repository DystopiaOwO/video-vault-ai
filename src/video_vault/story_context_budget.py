"""Fail-closed Story Provider context budget preflight."""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence


STORY_CONTEXT_BUDGET_CONTRACT_VERSION = "story-context-budget-v1"
DEFAULT_RESERVED_OUTPUT_TOKENS = 2048
TOKEN_ESTIMATOR_VERSION = "utf8-bytes-upper-bound-v2"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _default_messages(snapshot: Mapping[str, Any], system_prompt: str) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": str(system_prompt)},
        {"role": "user", "content": _canonical(snapshot)},
    ]


def _audited_provider_token_count(provider: Any, messages: Sequence[Mapping[str, Any]], metadata: Mapping[str, Any]) -> tuple[int, str] | None:
    source = str(metadata.get("tokenizer_source") or "")
    if not source or metadata.get("tokenizer_verified") is not True:
        return None
    for method_name in ("estimate_input_tokens", "count_tokens", "tokenize"):
        method = getattr(provider, method_name, None)
        if not callable(method):
            continue
        try:
            value = method(messages)
            if isinstance(value, Mapping):
                value = value.get("tokens") or value.get("token_count")
            if isinstance(value, (list, tuple)):
                value = len(value)
            parsed = _positive_int(value)
        except (TypeError, ValueError, OSError):
            parsed = None
        if parsed is not None:
            return parsed, source
    return None


def estimate_story_input_tokens(
    snapshot: Mapping[str, Any],
    *,
    system_prompt: str = "",
    request_messages: Sequence[Mapping[str, Any]] | None = None,
    provider: Any | None = None,
) -> dict[str, int | str]:
    """Estimate the exact request shape, preferring audited tokenizer metadata."""

    messages = list(request_messages or _default_messages(snapshot, system_prompt))
    messages_json = _canonical(messages)
    total_bytes = len(messages_json.encode("utf-8"))
    metadata = provider_context_metadata(provider) if provider is not None else {}
    audited_count = _audited_provider_token_count(provider, messages, metadata) if provider is not None else None
    if audited_count is not None:
        estimated, tokenizer_source = audited_count
        estimator = "provider-model-tokenizer-v1"
        guarantee = "provider/model tokenizer declared verified by provider metadata"
    else:
        # A tokenizer can always split a UTF-8 byte stream into at most one token
        # per byte; this is intentionally an upper bound and may over-block.
        estimated = max(1, total_bytes)
        tokenizer_source = "fail_closed_utf8_byte_upper_bound"
        estimator = TOKEN_ESTIMATOR_VERSION
        guarantee = "upper bound: one token cannot represent fewer than one encoded byte"
    return {
        "estimator": estimator,
        "estimator_guarantee": guarantee,
        "tokenizer_source": tokenizer_source,
        "estimated_input_tokens": int(estimated),
        "input_bytes": int(total_bytes),
        "message_count": int(len(messages)),
        "request_messages_bytes": int(total_bytes),
    }


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def provider_context_metadata(provider: Any) -> dict[str, Any]:
    method = getattr(provider, "context_metadata", None)
    metadata = method() if callable(method) else None
    if not isinstance(metadata, Mapping):
        metadata = {}
    result = dict(metadata)
    result.setdefault("provider", str(getattr(provider, "provider", "") or ""))
    result.setdefault("model", str(getattr(provider, "model", "") or ""))
    result["context_capacity_tokens"] = _positive_int(
        result.get("context_capacity_tokens")
        or result.get("max_context_length")
        or result.get("context_length")
    )
    result["source"] = str(result.get("source") or "unknown")
    return result


def preflight_story_context(
    snapshot: Mapping[str, Any],
    provider: Any,
    *,
    system_prompt: str = "",
    request_messages: Sequence[Mapping[str, Any]] | None = None,
    reserved_output_tokens: int = DEFAULT_RESERVED_OUTPUT_TOKENS,
) -> dict[str, Any]:
    """Return an auditable allow/block contract before any provider request."""

    metadata = provider_context_metadata(provider)
    estimate = estimate_story_input_tokens(
        snapshot,
        system_prompt=system_prompt,
        request_messages=request_messages,
        provider=provider,
    )
    capacity = metadata.get("context_capacity_tokens")
    reserved = _positive_int(reserved_output_tokens) or DEFAULT_RESERVED_OUTPUT_TOKENS
    estimated_input = int(estimate["estimated_input_tokens"])
    budget = {
        "contract_version": STORY_CONTEXT_BUDGET_CONTRACT_VERSION,
        "provider": str(metadata.get("provider") or ""),
        "model": str(metadata.get("model") or ""),
        "context_capacity_tokens": capacity,
        "available_context_tokens": capacity,
        "reserved_output_tokens": reserved,
        "available_input_tokens": max(0, int(capacity or 0) - reserved),
        "estimated_input_tokens": estimated_input,
        "estimated_request_tokens": estimated_input + reserved,
        "context_metadata_source": str(metadata.get("source") or "unknown"),
        "context_metadata": metadata,
        "token_estimator": {key: value for key, value in estimate.items()},
        "request_allowed": False,
        "status": "blocked",
        "reason_code": "context_capacity_unknown",
        "recommendation": "請提供可稽核的 model context metadata；否則請縮減 input 或換用已知 context 的模型。",
    }
    if capacity is None:
        return budget
    if estimated_input + reserved > int(capacity):
        budget.update(
            {
                "reason_code": "estimated_input_exceeds_context",
                "recommendation": "請縮減 input 或換用更大 context 的模型；不會自動切換 cloud 或付費 provider。",
            }
        )
        return budget
    budget.update({"request_allowed": True, "status": "pass", "reason_code": "within_context_budget", "recommendation": ""})
    return budget


def context_budget_error_message(budget: Mapping[str, Any]) -> str:
    estimated = int(budget.get("estimated_input_tokens") or 0)
    capacity = budget.get("available_context_tokens")
    reason = str(budget.get("reason_code") or "context_budget_blocked")
    if reason == "context_capacity_unknown":
        return (
            f"Story Provider context capacity unknown：estimated input {estimated} tokens，"
            "available context unknown；已在 request 前 blocked，"
            "不得假設無限 context。請提供可稽核的 provider/model context metadata，"
            "或縮減 input / 換用已知 context 的模型。"
        )
    return (
        f"Story input context budget blocked：estimated input {estimated} tokens，"
        f"available context {capacity} tokens；請縮減 input 或換用更大 context 的模型。"
    )


__all__ = [
    "DEFAULT_RESERVED_OUTPUT_TOKENS",
    "STORY_CONTEXT_BUDGET_CONTRACT_VERSION",
    "TOKEN_ESTIMATOR_VERSION",
    "context_budget_error_message",
    "estimate_story_input_tokens",
    "preflight_story_context",
    "provider_context_metadata",
]

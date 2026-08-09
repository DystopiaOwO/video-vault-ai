"""Fail-closed Story Provider context budget preflight."""

from __future__ import annotations

import json
import math
from typing import Any, Mapping


STORY_CONTEXT_BUDGET_CONTRACT_VERSION = "story-context-budget-v1"
DEFAULT_RESERVED_OUTPUT_TOKENS = 2048
TOKEN_ESTIMATOR_VERSION = "utf8-bytes-div3-conservative-v1"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def estimate_story_input_tokens(snapshot: Mapping[str, Any], *, system_prompt: str = "") -> dict[str, int | str]:
    """Estimate request input conservatively without requiring a provider tokenizer."""

    snapshot_json = _canonical(snapshot)
    system_bytes = len(str(system_prompt).encode("utf-8"))
    snapshot_bytes = len(snapshot_json.encode("utf-8"))
    message_overhead_bytes = len('{"role":"system","content":""}{"role":"user","content":""}'.encode("utf-8"))
    total_bytes = system_bytes + snapshot_bytes + message_overhead_bytes
    estimated = max(1, math.ceil(total_bytes / 3))
    return {
        "estimator": TOKEN_ESTIMATOR_VERSION,
        "estimated_input_tokens": int(estimated),
        "input_bytes": int(total_bytes),
        "system_prompt_bytes": int(system_bytes),
        "snapshot_bytes": int(snapshot_bytes),
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
    reserved_output_tokens: int = DEFAULT_RESERVED_OUTPUT_TOKENS,
) -> dict[str, Any]:
    """Return an auditable allow/block contract before any provider request."""

    estimate = estimate_story_input_tokens(snapshot, system_prompt=system_prompt)
    metadata = provider_context_metadata(provider)
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

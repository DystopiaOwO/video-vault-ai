"""Fresh, fail-closed LM Studio loaded-instance context evidence.

The OpenAI-compatible ``/v1/models`` endpoint only proves that a model name is
addressable.  LM Studio's native v1 API exposes the context length of each
currently loaded instance, which is the value that constrains an imminent
OpenAI-compatible generation request.  This module deliberately performs no
load, download, unload, or configuration mutation.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Callable, Mapping
import urllib.error
import urllib.request
from urllib.parse import urlparse, urlunparse

from .capability_registry import validate_local_endpoint_scope


LMSTUDIO_RUNTIME_CONTEXT_CONTRACT = "lmstudio-runtime-context-v1"
LMSTUDIO_RUNTIME_CONTEXT_SOURCE = "lmstudio.api_v1.loaded_instances.config.context_length"


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _native_models_url(validated_endpoint: str) -> str | None:
    parsed = urlparse(str(validated_endpoint).rstrip("/"))
    path = parsed.path.rstrip("/")
    if path not in {"", "/v1"} or parsed.params or parsed.query or parsed.fragment:
        return None
    return urlunparse((parsed.scheme, parsed.netloc, "/api/v1/models", "", "", ""))


def _blocked(
    evidence: Mapping[str, Any],
    reason_code: str,
    *,
    error: str = "",
    **fields: Any,
) -> dict[str, Any]:
    return {
        **dict(evidence),
        **fields,
        "context_capacity_tokens": None,
        "source": "lmstudio.runtime.unverified",
        "verified": False,
        "metadata_status": "blocked",
        "runtime_context_status": "blocked",
        "runtime_authoritative": True,
        "reason_code": str(reason_code),
        "error": str(error)[:500],
    }


def resolve_lmstudio_runtime_context(
    base_url: str,
    model: str,
    *,
    configured_context_length: Any = None,
    configured_context_source: str = "unknown",
    timeout_seconds: float = 5.0,
    urlopen_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Query the exact loaded model instance and return authoritative context.

    A downloaded-but-unloaded model is intentionally not sufficient evidence:
    an OpenAI-compatible request may JIT-load it with a different context.  The
    caller must therefore block generation unless a loaded instance whose ID
    exactly matches the requested model is visible in the fresh native API
    response.
    """

    configured_capacity = _positive_int(configured_context_length)
    configured_endpoint = str(base_url).rstrip("/")
    requested_model = str(model).strip()
    observed_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    evidence: dict[str, Any] = {
        "contract_version": LMSTUDIO_RUNTIME_CONTEXT_CONTRACT,
        "provider": "local_text",
        "model": requested_model,
        "configured_endpoint": configured_endpoint,
        "configured_context_capacity_tokens": configured_capacity,
        "configured_context_source": str(configured_context_source or "unknown"),
        "runtime_authority_required": True,
        "runtime_context_freshness": "live_query_per_preflight",
        "runtime_observed_at": observed_at,
        "runtime_query_method": "GET",
        "runtime_query_generation_post": False,
        "model_load_attempted": False,
        "model_download": False,
        "config_mutated": False,
        "cloud_fallback": False,
        "paid_call": False,
    }
    if not configured_endpoint or not requested_model:
        return _blocked(evidence, "runtime_binding_incomplete")

    scope = validate_local_endpoint_scope(configured_endpoint)
    evidence.update(
        {
            "network_scope_policy": scope.get("network_scope_policy"),
            "validated_network_scope": scope.get("validated_network_scope"),
            "resolved_addresses": list(scope.get("resolved_addresses") or []),
            "validated_endpoint": str(scope.get("validated_endpoint") or ""),
        }
    )
    if scope.get("status") != "pass":
        return _blocked(
            evidence,
            "runtime_endpoint_scope_blocked",
            scope_error_code=str(scope.get("error_code") or "local_endpoint_scope_blocked"),
        )

    native_url = _native_models_url(str(scope["validated_endpoint"]))
    if native_url is None:
        return _blocked(evidence, "runtime_api_endpoint_unsupported")
    evidence["runtime_models_endpoint"] = native_url

    request = urllib.request.Request(
        native_url,
        headers={"accept": "application/json"},
        method="GET",
    )
    opener = urlopen_fn or urllib.request.urlopen
    try:
        with opener(request, timeout=max(0.1, float(timeout_seconds))) as response:
            status = int(getattr(response, "status", 200))
            raw = response.read().decode("utf-8")
        evidence["runtime_query_http_status"] = status
        if status < 200 or status >= 300:
            return _blocked(evidence, "runtime_context_query_failed", http_status=status)
        payload = json.loads(raw)
    except urllib.error.HTTPError as exc:
        return _blocked(
            evidence,
            "runtime_context_query_failed",
            error=f"HTTP {int(exc.code)}",
            http_status=int(exc.code),
        )
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        return _blocked(
            evidence,
            "runtime_context_query_failed",
            error=f"{type(exc).__name__}: {exc}",
        )

    models = payload.get("models") if isinstance(payload, Mapping) else None
    if not isinstance(models, list):
        return _blocked(evidence, "runtime_context_response_malformed")
    records = [item for item in models if isinstance(item, Mapping)]
    evidence["runtime_model_count"] = len(records)

    exact_instances: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    exact_records: list[Mapping[str, Any]] = []
    for record in records:
        if str(record.get("key") or "") == requested_model:
            exact_records.append(record)
        instances = record.get("loaded_instances")
        if not isinstance(instances, list):
            continue
        for instance in instances:
            if isinstance(instance, Mapping) and str(instance.get("id") or "") == requested_model:
                exact_instances.append((record, instance))

    if len(exact_instances) > 1:
        return _blocked(evidence, "runtime_instance_binding_ambiguous", exact_instance_matches=len(exact_instances))
    if not exact_instances:
        if not exact_records:
            return _blocked(evidence, "runtime_model_not_found", model_exists=False)
        loaded_ids = sorted(
            {
                str(instance.get("id") or "")
                for record in exact_records
                for instance in (record.get("loaded_instances") or [])
                if isinstance(instance, Mapping) and str(instance.get("id") or "")
            }
        )
        if not loaded_ids:
            return _blocked(
                evidence,
                "runtime_model_not_loaded",
                model_exists=True,
                loaded_instance_count=0,
                model_max_context_length=_positive_int(exact_records[0].get("max_context_length")),
            )
        return _blocked(
            evidence,
            "runtime_instance_binding_mismatch",
            model_exists=True,
            loaded_instance_count=len(loaded_ids),
            loaded_instance_ids=loaded_ids,
        )

    record, instance = exact_instances[0]
    if str(record.get("type") or "llm") != "llm":
        return _blocked(evidence, "runtime_model_not_llm", model_exists=True)
    config = instance.get("config") if isinstance(instance.get("config"), Mapping) else {}
    capacity = _positive_int(config.get("context_length"))
    max_capacity = _positive_int(record.get("max_context_length"))
    if capacity is None:
        return _blocked(evidence, "runtime_context_missing", model_exists=True)
    if max_capacity is not None and capacity > max_capacity:
        return _blocked(
            evidence,
            "runtime_context_invalid",
            model_exists=True,
            runtime_context_capacity_tokens=capacity,
            model_max_context_length=max_capacity,
        )

    binding = {
        "validated_endpoint": str(scope["validated_endpoint"]).rstrip("/"),
        "requested_model": requested_model,
        "model_key": str(record.get("key") or ""),
        "instance_id": str(instance.get("id") or ""),
        "instance_config": dict(config),
    }
    fingerprint = hashlib.sha256(_canonical(binding).encode("utf-8")).hexdigest()
    return {
        **evidence,
        "context_capacity_tokens": capacity,
        "source": LMSTUDIO_RUNTIME_CONTEXT_SOURCE,
        "verified": True,
        "metadata_status": "verified",
        "runtime_context_status": "verified",
        "runtime_authoritative": True,
        "reason_code": "runtime_context_verified",
        "model_exists": True,
        "model_key": binding["model_key"],
        "loaded_instance_id": binding["instance_id"],
        "loaded_instance_count": 1,
        "runtime_instance_fingerprint": fingerprint,
        "runtime_instance_config": dict(config),
        "model_max_context_length": max_capacity,
        "configured_context_disagrees": configured_capacity is not None and configured_capacity != capacity,
    }


__all__ = [
    "LMSTUDIO_RUNTIME_CONTEXT_CONTRACT",
    "LMSTUDIO_RUNTIME_CONTEXT_SOURCE",
    "resolve_lmstudio_runtime_context",
]

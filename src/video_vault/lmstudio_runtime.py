"""Fresh LM Studio runtime context evidence and scoped Story provisioning.

The OpenAI-compatible ``/v1/models`` endpoint only proves that a model name is
addressable.  LM Studio's native v1 API exposes the context length of each
currently loaded instance, which is the value that constrains an imminent
OpenAI-compatible generation request. Optional provisioning owns only the
exact instance ID returned by its own native load request.
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
LMSTUDIO_RUNTIME_PROVISIONING_CONTRACT = "lmstudio-story-runtime-provisioning-v1"


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


def _native_action_url(validated_endpoint: str, action: str) -> str | None:
    models_url = _native_models_url(validated_endpoint)
    if models_url is None or action not in {"load", "unload"}:
        return None
    parsed = urlparse(models_url)
    return urlunparse((parsed.scheme, parsed.netloc, f"/api/v1/models/{action}", "", "", ""))


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
            model_max_context_length=_positive_int(exact_records[0].get("max_context_length")),
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


class LMStudioRuntimeProvisioner:
    """Create and clean up one app-owned LM Studio Story instance.

    The provisioner never downloads a model, changes persistent LM Studio
    configuration, or unloads/resizes an instance that existed before its own
    load request. Every successful load is followed by the same live resolver
    used by Story preflight.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        enabled: bool = False,
        target_context_length: Any = None,
        load_timeout_seconds: float = 180.0,
        cleanup_timeout_seconds: float = 30.0,
        runtime_context_timeout_seconds: float = 5.0,
        runtime_context_resolver: Callable[..., Mapping[str, Any]] | None = None,
        urlopen_fn: Callable[..., Any] | None = None,
    ):
        self.base_url = str(base_url).rstrip("/")
        self.model = str(model).strip()
        self.enabled = bool(enabled)
        self.target_context_length = _positive_int(target_context_length)
        self.load_timeout_seconds = max(0.1, float(load_timeout_seconds))
        self.cleanup_timeout_seconds = max(0.1, float(cleanup_timeout_seconds))
        self.runtime_context_timeout_seconds = max(0.1, float(runtime_context_timeout_seconds))
        self.runtime_context_resolver = runtime_context_resolver or resolve_lmstudio_runtime_context
        self.urlopen_fn = urlopen_fn
        self.request_model = self.model
        self.load_attempted = False
        self.owned_instance_id = ""
        self._owned_instance_config: dict[str, Any] = {}
        self._preexisting_instances: dict[str, dict[str, Any]] = {}
        self._cleanup_complete = False
        self.last_evidence: dict[str, Any] = self._base_evidence()
        self.last_cleanup: dict[str, Any] = {}

    def _base_evidence(self) -> dict[str, Any]:
        return {
            "contract_version": LMSTUDIO_RUNTIME_PROVISIONING_CONTRACT,
            "configured_endpoint": self.base_url,
            "configured_model": self.model,
            "enabled": self.enabled,
            "requested_target_context_tokens": self.target_context_length,
            "load_attempted": self.load_attempted,
            "load_generation_post": False,
            "app_owned_instance": bool(self.owned_instance_id),
            "owned_instance_id": self.owned_instance_id,
            "model_download": False,
            "persistent_config_mutated": False,
            "cloud_fallback": False,
            "paid_call": False,
            "external_instance_unload_attempted": False,
        }

    def _opener(self) -> Callable[..., Any]:
        return self.urlopen_fn or urllib.request.urlopen

    def _scope(self) -> tuple[dict[str, Any], str | None, str | None, str | None]:
        scope = validate_local_endpoint_scope(self.base_url)
        if scope.get("status") != "pass":
            return scope, None, None, None
        endpoint = str(scope.get("validated_endpoint") or "")
        return (
            scope,
            _native_models_url(endpoint),
            _native_action_url(endpoint, "load"),
            _native_action_url(endpoint, "unload"),
        )

    def _http_json(
        self,
        method: str,
        url: str,
        *,
        payload: Mapping[str, Any] | None = None,
        timeout_seconds: float,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        data = None if payload is None else json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
        headers = {"accept": "application/json"}
        if data is not None:
            headers["content-type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        evidence: dict[str, Any] = {"method": method, "url": url}
        try:
            with self._opener()(request, timeout=max(0.1, float(timeout_seconds))) as response:
                status = int(getattr(response, "status", 200))
                raw = response.read().decode("utf-8")
            evidence["http_status"] = status
            if status < 200 or status >= 300:
                evidence.update({"status": "blocked", "error_code": "runtime_api_http_error"})
                return None, evidence
            parsed = json.loads(raw)
            if not isinstance(parsed, Mapping):
                evidence.update({"status": "blocked", "error_code": "runtime_api_response_malformed"})
                return None, evidence
            evidence["status"] = "pass"
            return dict(parsed), evidence
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except (AttributeError, OSError, ValueError):
                body = ""
            evidence.update(
                {
                    "status": "blocked",
                    "http_status": int(exc.code),
                    "error_code": "runtime_api_http_error",
                    "error": body[:500] or f"HTTP {int(exc.code)}",
                }
            )
            return None, evidence
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            evidence.update(
                {
                    "status": "blocked",
                    "error_code": "runtime_api_request_failed",
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                }
            )
            return None, evidence

    @staticmethod
    def _model_record(payload: Mapping[str, Any], model: str) -> tuple[Mapping[str, Any] | None, str | None]:
        models = payload.get("models")
        if not isinstance(models, list):
            return None, "runtime_models_response_malformed"
        exact = [item for item in models if isinstance(item, Mapping) and str(item.get("key") or "") == model]
        if len(exact) != 1:
            return None, "runtime_model_not_found" if not exact else "runtime_model_binding_ambiguous"
        if str(exact[0].get("type") or "llm") != "llm":
            return None, "runtime_model_not_llm"
        return exact[0], None

    @staticmethod
    def _instances(record: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        raw_instances = record.get("loaded_instances")
        if not isinstance(raw_instances, list):
            return result
        for item in raw_instances:
            if not isinstance(item, Mapping):
                continue
            instance_id = str(item.get("id") or "").strip()
            if not instance_id:
                continue
            config = item.get("config") if isinstance(item.get("config"), Mapping) else {}
            result[instance_id] = dict(config)
        return result

    def _blocked_metadata(
        self,
        current_metadata: Mapping[str, Any],
        reason_code: str,
        evidence: Mapping[str, Any],
    ) -> dict[str, Any]:
        provisioning = {**self._base_evidence(), **dict(evidence), "status": "blocked", "reason_code": reason_code}
        self.last_evidence = dict(provisioning)
        return {
            **dict(current_metadata),
            "context_capacity_tokens": None,
            "source": "lmstudio.runtime.unverified",
            "verified": False,
            "metadata_status": "blocked",
            "runtime_context_status": "blocked",
            "runtime_authoritative": True,
            "reason_code": reason_code,
            "runtime_provisioning": provisioning,
        }

    def ensure(
        self,
        required_context_tokens: Any,
        current_metadata: Mapping[str, Any],
        *,
        generation_post_calls: int = 0,
    ) -> dict[str, Any]:
        required = _positive_int(required_context_tokens)
        current = dict(current_metadata)
        capacity = _positive_int(current.get("context_capacity_tokens"))
        current_verified = current.get("runtime_context_status") == "verified"
        if required is None:
            return self._blocked_metadata(current, "runtime_required_context_invalid", {})
        if current_verified and capacity is not None and capacity >= required:
            status = "ready_app_owned" if self.owned_instance_id else "ready_as_is"
            provisioning = {
                **self._base_evidence(),
                "status": status,
                "reason_code": "runtime_context_sufficient",
                "required_context_tokens": required,
                "applied_context_tokens": capacity,
            }
            self.last_evidence = dict(provisioning)
            return {**current, "runtime_provisioning": provisioning}
        if not self.enabled:
            provisioning = {
                **self._base_evidence(),
                "status": "disabled",
                "reason_code": "runtime_provisioning_disabled",
                "required_context_tokens": required,
            }
            self.last_evidence = dict(provisioning)
            return {**current, "runtime_provisioning": provisioning}
        if generation_post_calls > 0 or self.load_attempted:
            reason = (
                "runtime_binding_changed_after_generation_post"
                if generation_post_calls > 0
                else "runtime_provisioning_attempt_already_consumed"
            )
            return self._blocked_metadata(
                current,
                reason,
                {"required_context_tokens": required, "generation_post_calls": int(generation_post_calls)},
            )

        target = max(required, self.target_context_length or required)
        scope, models_url, load_url, _unload_url = self._scope()
        scope_evidence = {
            "network_scope_policy": scope.get("network_scope_policy"),
            "validated_network_scope": scope.get("validated_network_scope"),
            "resolved_addresses": list(scope.get("resolved_addresses") or []),
            "validated_endpoint": str(scope.get("validated_endpoint") or ""),
            "required_context_tokens": required,
            "requested_context_tokens": target,
        }
        if scope.get("status") != "pass" or not models_url or not load_url:
            return self._blocked_metadata(current, "runtime_provisioning_endpoint_blocked", scope_evidence)

        before_payload, before_http = self._http_json(
            "GET", models_url, timeout_seconds=self.runtime_context_timeout_seconds
        )
        if before_payload is None:
            return self._blocked_metadata(
                current,
                "runtime_provisioning_inventory_failed",
                {**scope_evidence, "inventory_before": before_http},
            )
        record, record_error = self._model_record(before_payload, self.model)
        if record is None:
            return self._blocked_metadata(
                current,
                str(record_error or "runtime_model_not_found"),
                {**scope_evidence, "inventory_before": before_http},
            )
        max_context = _positive_int(record.get("max_context_length"))
        if max_context is None or max_context < target:
            return self._blocked_metadata(
                current,
                "runtime_model_max_context_insufficient",
                {
                    **scope_evidence,
                    "inventory_before": before_http,
                    "model_max_context_length": max_context,
                },
            )
        self._preexisting_instances = self._instances(record)
        self.load_attempted = True
        self._cleanup_complete = False
        self.last_cleanup = {}
        load_request = {
            "model": self.model,
            "context_length": target,
            "echo_load_config": True,
        }
        load_payload, load_http = self._http_json(
            "POST",
            load_url,
            payload=load_request,
            timeout_seconds=self.load_timeout_seconds,
        )
        common = {
            **scope_evidence,
            "load_attempted": True,
            "model_max_context_length": max_context,
            "preexisting_instance_ids": sorted(self._preexisting_instances),
            "load_request": load_request,
            "load_http": load_http,
        }
        if load_payload is None:
            return self._blocked_metadata(current, "runtime_model_load_failed", common)
        instance_id = str(load_payload.get("instance_id") or "").strip()
        load_status = str(load_payload.get("status") or "").strip().lower()
        load_config = load_payload.get("load_config") if isinstance(load_payload.get("load_config"), Mapping) else {}
        echoed_context = _positive_int(load_config.get("context_length"))
        if not instance_id:
            return self._blocked_metadata(
                current,
                "runtime_model_load_response_malformed",
                {**common, "load_response_status": load_status, "load_response_instance_id": instance_id},
            )
        if instance_id in self._preexisting_instances:
            return self._blocked_metadata(
                current,
                "runtime_load_returned_preexisting_instance",
                {**common, "load_response_instance_id": instance_id, "load_response_context_tokens": echoed_context},
            )

        self.owned_instance_id = instance_id
        self.request_model = instance_id
        self._owned_instance_config = dict(load_config)
        post = self.runtime_context_resolver(
            self.base_url,
            instance_id,
            configured_context_length=target,
            configured_context_source="runtime_provisioning.requested_context_length",
            timeout_seconds=self.runtime_context_timeout_seconds,
            urlopen_fn=self._opener(),
        )
        post = dict(post) if isinstance(post, Mapping) else {}
        applied = _positive_int(post.get("context_capacity_tokens"))
        verification = {
            **common,
            "app_owned_instance": True,
            "owned_instance_id": instance_id,
            "load_response_status": load_status,
            "load_response_context_tokens": echoed_context,
            "post_load_runtime": post,
        }
        if (
            str(post.get("loaded_instance_id") or "") == instance_id
            and str(post.get("model_key") or "") == self.model
            and isinstance(post.get("runtime_instance_config"), Mapping)
        ):
            self._owned_instance_config = dict(post["runtime_instance_config"])
        if (
            load_status != "loaded"
            or post.get("runtime_context_status") != "verified"
            or str(post.get("loaded_instance_id") or "") != instance_id
            or str(post.get("model_key") or "") != self.model
            or applied is None
            or applied < target
        ):
            reason = (
                "runtime_model_load_response_malformed"
                if load_status != "loaded"
                else "runtime_post_load_verification_failed"
            )
            return self._blocked_metadata(current, reason, verification)
        provisioning = {
            **self._base_evidence(),
            **verification,
            "status": "provisioned",
            "reason_code": "runtime_app_owned_context_verified",
            "applied_context_tokens": applied,
            "runtime_instance_fingerprint": str(post.get("runtime_instance_fingerprint") or ""),
        }
        self.last_evidence = dict(provisioning)
        return {
            **post,
            "configured_model": self.model,
            "request_model": instance_id,
            "runtime_provisioning": provisioning,
        }

    def cleanup(self) -> dict[str, Any]:
        if self._cleanup_complete:
            return dict(self.last_cleanup)
        base = {
            "contract_version": LMSTUDIO_RUNTIME_PROVISIONING_CONTRACT,
            "owned_instance_id": self.owned_instance_id,
            "preexisting_instance_ids": sorted(self._preexisting_instances),
            "unload_attempted": False,
            "external_instance_unload_attempted": False,
            "model_download": False,
            "persistent_config_mutated": False,
            "cloud_fallback": False,
        }
        if not self.owned_instance_id:
            self._cleanup_complete = True
            self.last_cleanup = {**base, "status": "not_needed", "reason_code": "no_app_owned_instance"}
            return dict(self.last_cleanup)
        if self.owned_instance_id in self._preexisting_instances:
            self.last_cleanup = {
                **base,
                "status": "blocked",
                "reason_code": "cleanup_ownership_not_proven",
            }
            return dict(self.last_cleanup)

        scope, models_url, _load_url, unload_url = self._scope()
        if scope.get("status") != "pass" or not models_url or not unload_url:
            self.last_cleanup = {
                **base,
                "status": "blocked",
                "reason_code": "cleanup_endpoint_blocked",
                "validated_network_scope": scope.get("validated_network_scope"),
            }
            return dict(self.last_cleanup)
        before_payload, before_http = self._http_json(
            "GET", models_url, timeout_seconds=self.cleanup_timeout_seconds
        )
        if before_payload is None:
            self.last_cleanup = {
                **base,
                "status": "blocked",
                "reason_code": "cleanup_inventory_failed",
                "inventory_before_unload": before_http,
            }
            return dict(self.last_cleanup)
        record, record_error = self._model_record(before_payload, self.model)
        if record is None:
            self.last_cleanup = {
                **base,
                "status": "blocked",
                "reason_code": str(record_error or "cleanup_model_not_found"),
                "inventory_before_unload": before_http,
            }
            return dict(self.last_cleanup)
        instances = self._instances(record)
        if self.owned_instance_id not in instances:
            preserved = all(instances.get(key) == value for key, value in self._preexisting_instances.items())
            self._cleanup_complete = preserved
            self.last_cleanup = {
                **base,
                "status": "pass" if preserved else "blocked",
                "reason_code": "owned_instance_already_absent" if preserved else "external_instance_changed",
                "external_instances_preserved": preserved,
                "inventory_before_unload": before_http,
            }
            return dict(self.last_cleanup)
        if instances[self.owned_instance_id] != self._owned_instance_config:
            self.last_cleanup = {
                **base,
                "status": "blocked",
                "reason_code": "cleanup_owned_instance_config_changed",
                "inventory_before_unload": before_http,
            }
            return dict(self.last_cleanup)

        unload_payload, unload_http = self._http_json(
            "POST",
            unload_url,
            payload={"instance_id": self.owned_instance_id},
            timeout_seconds=self.cleanup_timeout_seconds,
        )
        attempted = {**base, "unload_attempted": True, "unload_http": unload_http}
        if unload_payload is None:
            self.last_cleanup = {
                **attempted,
                "status": "blocked",
                "reason_code": "runtime_owned_instance_unload_failed",
            }
            return dict(self.last_cleanup)
        after_payload, after_http = self._http_json(
            "GET", models_url, timeout_seconds=self.cleanup_timeout_seconds
        )
        if after_payload is None:
            self.last_cleanup = {
                **attempted,
                "status": "blocked",
                "reason_code": "cleanup_post_unload_inventory_failed",
                "inventory_after_unload": after_http,
            }
            return dict(self.last_cleanup)
        after_record, after_error = self._model_record(after_payload, self.model)
        if after_record is None:
            self.last_cleanup = {
                **attempted,
                "status": "blocked",
                "reason_code": str(after_error or "cleanup_model_not_found_after_unload"),
                "inventory_after_unload": after_http,
            }
            return dict(self.last_cleanup)
        after_instances = self._instances(after_record)
        owned_absent = self.owned_instance_id not in after_instances
        preserved = all(after_instances.get(key) == value for key, value in self._preexisting_instances.items())
        passed = owned_absent and preserved
        self._cleanup_complete = passed
        self.last_cleanup = {
            **attempted,
            "status": "pass" if passed else "blocked",
            "reason_code": "app_owned_instance_unloaded" if passed else "cleanup_verification_failed",
            "owned_instance_absent": owned_absent,
            "external_instances_preserved": preserved,
            "inventory_after_unload": after_http,
        }
        return dict(self.last_cleanup)


__all__ = [
    "LMSTUDIO_RUNTIME_CONTEXT_CONTRACT",
    "LMSTUDIO_RUNTIME_CONTEXT_SOURCE",
    "LMSTUDIO_RUNTIME_PROVISIONING_CONTRACT",
    "LMStudioRuntimeProvisioner",
    "resolve_lmstudio_runtime_context",
]

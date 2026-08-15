from __future__ import annotations

from io import BytesIO
import json
import urllib.error
import urllib.request

import pytest

from video_vault.lmstudio_runtime import (
    LMSTUDIO_RUNTIME_CONTEXT_SOURCE,
    resolve_lmstudio_runtime_context,
)
from video_vault.story_generation import (
    LocalTextStoryProvider,
    StoryContextBudgetError,
    StoryGenerationError,
    StoryProviderHTTPError,
    StoryRuntimeCleanupError,
    provider_from_config,
)


class _JsonResponse:
    def __init__(self, payload, status: int = 200):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


def _runtime_payload(
    model: str,
    capacity: int | None,
    *,
    instance_id: str | None = None,
    loaded: bool = True,
):
    instances = []
    if loaded:
        config = {} if capacity is None else {"context_length": capacity, "parallel": 1}
        instances.append({"id": instance_id or model, "config": config})
    return {
        "models": [
            {
                "type": "llm",
                "key": model,
                "loaded_instances": instances,
                "max_context_length": 262144,
            }
        ]
    }


def _valid_story():
    return {
        "schema_version": 1,
        "project_summary": "摘要",
        "story_profile": "general_diary",
        "chapters": [{"title": "章", "purpose": "整理", "segment_uuids": ["segment-a"], "confidence": 0.8}],
        "overall_confidence": 0.8,
    }


def _story_snapshot(**values):
    return {
        "story_profile_id": "general_diary",
        "segments": [{"segment_uuid": "segment-a", "human_override": {"include": True}}],
        **values,
    }


class _LMStudioRouter:
    def __init__(self, *, max_context: int = 262144, applied_context: int = 32768):
        self.max_context = max_context
        self.applied_context = applied_context
        self.instances = {"story-model": {"context_length": 8192, "parallel": 1}}
        self.calls = []
        self.load_requests = []
        self.unload_requests = []
        self.generation_requests = []
        self.load_failure = False
        self.unload_failure = False
        self.mutate_after_first_generation = False
        self.load_instance_id = "story-model:2"

    def inventory(self):
        return {
            "models": [
                {
                    "type": "llm",
                    "key": "story-model",
                    "loaded_instances": [
                        {"id": instance_id, "config": dict(config)}
                        for instance_id, config in sorted(self.instances.items())
                    ],
                    "max_context_length": self.max_context,
                }
            ]
        }

    def __call__(self, request, **_kwargs):
        self.calls.append((request.method, request.full_url))
        if request.full_url.endswith("/api/v1/models"):
            return _JsonResponse(self.inventory())
        if request.full_url.endswith("/api/v1/models/load"):
            payload = json.loads(request.data.decode("utf-8"))
            self.load_requests.append(payload)
            if self.load_failure:
                body = json.dumps({"error": {"message": "insufficient resources"}}).encode("utf-8")
                raise urllib.error.HTTPError(request.full_url, 409, "Conflict", {}, BytesIO(body))
            instance_id = self.load_instance_id
            config = {"context_length": self.applied_context, "parallel": 1}
            if instance_id not in self.instances:
                self.instances[instance_id] = dict(config)
            return _JsonResponse(
                {
                    "status": "loaded",
                    "instance_id": instance_id,
                    "load_config": config,
                }
            )
        if request.full_url.endswith("/api/v1/models/unload"):
            payload = json.loads(request.data.decode("utf-8"))
            self.unload_requests.append(payload)
            if self.unload_failure:
                body = json.dumps({"error": {"message": "unload failed"}}).encode("utf-8")
                raise urllib.error.HTTPError(request.full_url, 500, "Server Error", {}, BytesIO(body))
            self.instances.pop(str(payload.get("instance_id") or ""), None)
            return _JsonResponse({"status": "unloaded", "instance_id": payload.get("instance_id")})
        if request.full_url.endswith("/v1/chat/completions"):
            payload = json.loads(request.data.decode("utf-8"))
            self.generation_requests.append(payload)
            if self.mutate_after_first_generation:
                self.instances["story-model:2"] = {"context_length": 8192, "parallel": 1}
                return _JsonResponse({"choices": [{"message": {"content": json.dumps({"unknown": True})}}]})
            return _JsonResponse(
                {"choices": [{"message": {"content": json.dumps(_valid_story(), ensure_ascii=False)}}]}
            )
        raise AssertionError(f"unexpected request: {request.method} {request.full_url}")


def _provisioning_provider() -> LocalTextStoryProvider:
    return LocalTextStoryProvider(
        "http://127.0.0.1:1234/v1",
        "story-model",
        context_length=32768,
        context_source="config.story.context_length",
        reserved_output_tokens=2048,
        runtime_provisioning_enabled=True,
        runtime_target_context_length=32768,
    )


def test_runtime_provisioning_config_boolean_and_target_are_normalized():
    base = {
        "story": {
            "provider": "local_text",
            "model": "story-model",
            "base_url": "http://127.0.0.1:1234/v1",
            "context_length": 32768,
            "reasoning_effort": "low",
            "runtime_provisioning": {
                "enabled": "false",
                "target_context_length": 32768,
            },
        }
    }
    disabled = provider_from_config(base)
    enabled = provider_from_config(
        {
            "story": {
                **base["story"],
                "runtime_provisioning": {
                    "enabled": "true",
                    "target_context_length": 32768,
                },
            }
        }
    )

    assert isinstance(disabled, LocalTextStoryProvider)
    assert disabled.reasoning_effort == "low"
    assert disabled.runtime_provisioner.enabled is False
    assert enabled.runtime_provisioner.enabled is True
    assert enabled.runtime_provisioner.target_context_length == 32768


def test_stale_config_is_overridden_by_exact_live_loaded_instance():
    calls = []

    def opener(request, **_kwargs):
        calls.append((request.method, request.full_url))
        return _JsonResponse(_runtime_payload("story-model", 8192))

    metadata = resolve_lmstudio_runtime_context(
        "http://127.0.0.1:1234/v1",
        "story-model",
        configured_context_length=32768,
        configured_context_source="config.story.context_length",
        urlopen_fn=opener,
    )

    assert calls == [("GET", "http://127.0.0.1:1234/api/v1/models")]
    assert metadata["context_capacity_tokens"] == 8192
    assert metadata["source"] == LMSTUDIO_RUNTIME_CONTEXT_SOURCE
    assert metadata["runtime_context_status"] == "verified"
    assert metadata["configured_context_capacity_tokens"] == 32768
    assert metadata["configured_context_disagrees"] is True
    assert metadata["loaded_instance_id"] == "story-model"
    assert len(metadata["runtime_instance_fingerprint"]) == 64
    assert metadata["model_load_attempted"] is False
    assert metadata["model_download"] is False
    assert metadata["config_mutated"] is False
    assert metadata["cloud_fallback"] is False
    assert metadata["paid_call"] is False


def test_stale_32768_live_8192_blocks_before_generation_post(monkeypatch):
    methods = []

    def opener(request, **_kwargs):
        methods.append(request.method)
        if request.method == "POST":
            raise AssertionError("oversized Story must be blocked before generation POST")
        return _JsonResponse(_runtime_payload("story-model", 8192))

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    provider = LocalTextStoryProvider(
        "http://127.0.0.1:1234/v1",
        "story-model",
        context_length=32768,
        context_source="stale.config",
        reserved_output_tokens=2048,
    )
    with pytest.raises(StoryContextBudgetError) as caught:
        provider.generate_story(_story_snapshot(project_intent="x" * 10000))

    assert methods == ["GET"]
    budget = caught.value.budget
    assert budget["context_capacity_tokens"] == 8192
    assert budget["context_metadata"]["configured_context_capacity_tokens"] == 32768
    assert budget["reason_code"] == "estimated_input_exceeds_context"
    assert caught.value.audit["calls"] == 0


def test_live_verified_sufficient_context_allows_generation_post(monkeypatch):
    calls = []

    def opener(request, **_kwargs):
        calls.append((request.method, request.full_url))
        if request.method == "GET":
            return _JsonResponse(_runtime_payload("story-model", 32768))
        payload = json.loads(request.data.decode("utf-8"))
        assert payload["model"] == "story-model"
        assert payload["max_tokens"] == 2048
        return _JsonResponse({"choices": [{"message": {"content": json.dumps(_valid_story(), ensure_ascii=False)}}]})

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    output, raw = LocalTextStoryProvider(
        "http://127.0.0.1:1234/v1",
        "story-model",
        context_length=8192,
        context_source="stale.config",
    ).generate_story(_story_snapshot())

    assert output == _valid_story()
    assert [method for method, _url in calls] == ["GET", "POST"]
    budget = raw["provider_audit"]["context_budget"]
    assert budget["context_capacity_tokens"] == 32768
    assert budget["context_metadata"]["configured_context_disagrees"] is True


def test_unloaded_model_and_binding_mismatch_fail_closed():
    unloaded = resolve_lmstudio_runtime_context(
        "http://127.0.0.1:1234/v1",
        "story-model",
        configured_context_length=32768,
        urlopen_fn=lambda *_args, **_kwargs: _JsonResponse(_runtime_payload("story-model", None, loaded=False)),
    )
    mismatch = resolve_lmstudio_runtime_context(
        "http://127.0.0.1:1234/v1",
        "story-model",
        configured_context_length=32768,
        urlopen_fn=lambda *_args, **_kwargs: _JsonResponse(
            _runtime_payload("story-model", 32768, instance_id="different-instance")
        ),
    )

    assert unloaded["context_capacity_tokens"] is None
    assert unloaded["reason_code"] == "runtime_model_not_loaded"
    assert unloaded["model_load_attempted"] is False
    assert mismatch["context_capacity_tokens"] is None
    assert mismatch["reason_code"] == "runtime_instance_binding_mismatch"


def test_runtime_reload_revalidates_before_retry_and_blocks_second_post(monkeypatch):
    capacities = [32768, 8192]
    methods = []

    def opener(request, **_kwargs):
        methods.append(request.method)
        if request.method == "GET":
            return _JsonResponse(_runtime_payload("story-model", capacities.pop(0)))
        return _JsonResponse({"choices": [{"message": {"content": json.dumps({"unknown": True})}}]})

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    provider = LocalTextStoryProvider(
        "http://127.0.0.1:1234/v1",
        "story-model",
        context_length=32768,
        context_source="stale.config",
        reserved_output_tokens=512,
    )
    with pytest.raises(StoryContextBudgetError) as caught:
        provider.generate_story(_story_snapshot(project_intent="x" * 10000))

    assert methods == ["GET", "POST", "GET"]
    audit = caught.value.audit
    assert audit["calls"] == 1
    assert audit["retries"] == 1
    assert audit["blocked_retry_budget"]["context_capacity_tokens"] == 8192
    first, second = [item["budget"] for item in audit["request_budgets"]]
    assert first["context_capacity_tokens"] == 32768
    assert second["context_capacity_tokens"] == 8192
    assert first["context_metadata"]["runtime_instance_fingerprint"] != second["context_metadata"]["runtime_instance_fingerprint"]


def test_public_endpoint_is_blocked_without_any_runtime_or_generation_request():
    calls = []

    def resolver(base_url, model, **kwargs):
        kwargs.pop("urlopen_fn", None)
        return resolve_lmstudio_runtime_context(
            base_url,
            model,
            **kwargs,
            urlopen_fn=lambda *_args, **_kwargs: calls.append("network"),
        )

    provider = LocalTextStoryProvider(
        "https://example.com/v1",
        "story-model",
        context_length=32768,
        runtime_context_resolver=resolver,
    )

    with pytest.raises(StoryContextBudgetError) as caught:
        provider.generate_story(_story_snapshot())

    assert calls == []
    assert caught.value.budget["reason_code"] == "runtime_endpoint_scope_blocked"
    assert caught.value.audit["calls"] == 0


def test_context_overflow_http_error_is_classified_and_not_retried(monkeypatch):
    methods = []

    def opener(request, **_kwargs):
        methods.append(request.method)
        if request.method == "GET":
            return _JsonResponse(_runtime_payload("story-model", 32768))
        body = json.dumps(
            {
                "error": {
                    "type": "exceed_context_size_error",
                    "message": "Request has 8833 tokens but n_ctx is 8192",
                }
            }
        ).encode("utf-8")
        raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, BytesIO(body))

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    provider = LocalTextStoryProvider(
        "http://127.0.0.1:1234/v1",
        "story-model",
        context_length=32768,
    )
    with pytest.raises(StoryProviderHTTPError, match="context overflow") as caught:
        provider.generate_story(_story_snapshot())

    assert methods == ["GET", "POST"]
    audit = caught.value.audit
    assert audit["calls"] == 1
    assert audit["retries"] == 0
    assert audit["error_code"] == "provider_context_overflow"
    assert audit["http_status"] == 400
    assert audit["provider_error_type"] == "exceed_context_size_error"
    assert audit["runtime_context"]["context_capacity_tokens"] == 32768


def test_generic_provider_timeout_persists_request_and_budget_audit(monkeypatch):
    methods = []

    def opener(request, **_kwargs):
        methods.append(request.method)
        if request.method == "GET":
            return _JsonResponse(_runtime_payload("story-model", 32768))
        raise TimeoutError("generation timed out")

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    provider = LocalTextStoryProvider(
        "http://127.0.0.1:1234/v1",
        "story-model",
        context_length=32768,
    )
    with pytest.raises(StoryGenerationError, match="本地文字模型不可用") as caught:
        provider.generate_story(_story_snapshot())

    assert methods == ["GET", "POST"]
    audit = caught.value.audit
    assert audit["calls"] == 1
    assert audit["generation_post_calls"] == 1
    assert audit["retries"] == 0
    assert len(audit["request_budgets"]) == 1
    assert audit["request_budgets"][0]["budget"]["status"] == "pass"


def test_app_owned_32k_instance_is_live_verified_used_and_cleaned_without_touching_external_8k(monkeypatch):
    router = _LMStudioRouter()
    monkeypatch.setattr(urllib.request, "urlopen", router)

    output, raw = _provisioning_provider().generate_story(_story_snapshot(project_intent="x" * 10000))

    assert output == _valid_story()
    assert router.load_requests == [
        {"model": "story-model", "context_length": 32768, "echo_load_config": True}
    ]
    assert len(router.generation_requests) == 1
    assert router.generation_requests[0]["model"] == "story-model:2"
    assert router.generation_requests[0]["max_tokens"] == 2048
    assert router.generation_requests[0]["reasoning_effort"] == "none"
    assert router.unload_requests == [{"instance_id": "story-model:2"}]
    assert router.instances == {"story-model": {"context_length": 8192, "parallel": 1}}
    audit = raw["provider_audit"]
    assert audit["runtime_provisioning"]["status"] in {"provisioned", "ready_app_owned"}
    assert audit["runtime_provisioning"]["owned_instance_id"] == "story-model:2"
    assert audit["runtime_provisioning"]["applied_context_tokens"] == 32768
    assert audit["runtime_provisioning"]["model_download"] is False
    assert audit["runtime_provisioning"]["persistent_config_mutated"] is False
    assert audit["runtime_cleanup"]["status"] == "pass"
    assert audit["runtime_cleanup"]["external_instances_preserved"] is True


def test_installed_but_unloaded_model_can_be_provisioned_without_jit_or_download(monkeypatch):
    router = _LMStudioRouter()
    router.instances = {}
    monkeypatch.setattr(urllib.request, "urlopen", router)

    output, raw = _provisioning_provider().generate_story(_story_snapshot(project_intent="x" * 10000))

    assert output == _valid_story()
    assert len(router.load_requests) == 1
    assert len(router.generation_requests) == 1
    assert router.generation_requests[0]["model"] == "story-model:2"
    assert router.generation_requests[0]["reasoning_effort"] == "none"
    assert router.unload_requests == [{"instance_id": "story-model:2"}]
    assert router.instances == {}
    assert raw["provider_audit"]["runtime_provisioning"]["model_download"] is False


def test_runtime_load_resource_failure_blocks_with_zero_story_post(monkeypatch):
    router = _LMStudioRouter()
    router.load_failure = True
    monkeypatch.setattr(urllib.request, "urlopen", router)

    with pytest.raises(StoryContextBudgetError) as caught:
        _provisioning_provider().generate_story(_story_snapshot(project_intent="x" * 10000))

    assert len(router.load_requests) == 1
    assert router.generation_requests == []
    assert router.unload_requests == []
    assert caught.value.budget["reason_code"] == "runtime_model_load_failed"
    assert caught.value.audit["generation_post_calls"] == 0
    assert router.instances == {"story-model": {"context_length": 8192, "parallel": 1}}


def test_post_load_applied_context_mismatch_blocks_zero_story_post_and_cleans_owned_instance(monkeypatch):
    router = _LMStudioRouter(applied_context=16384)
    monkeypatch.setattr(urllib.request, "urlopen", router)

    with pytest.raises(StoryContextBudgetError) as caught:
        _provisioning_provider().generate_story(_story_snapshot(project_intent="x" * 10000))

    assert caught.value.budget["reason_code"] == "runtime_post_load_verification_failed"
    assert router.generation_requests == []
    assert router.unload_requests == [{"instance_id": "story-model:2"}]
    assert router.instances == {"story-model": {"context_length": 8192, "parallel": 1}}
    assert caught.value.audit["runtime_cleanup"]["status"] == "pass"


def test_model_max_context_mismatch_blocks_before_load_and_story_post(monkeypatch):
    router = _LMStudioRouter(max_context=16384)
    monkeypatch.setattr(urllib.request, "urlopen", router)

    with pytest.raises(StoryContextBudgetError) as caught:
        _provisioning_provider().generate_story(_story_snapshot(project_intent="x" * 10000))

    assert caught.value.budget["reason_code"] == "runtime_model_max_context_insufficient"
    assert router.load_requests == []
    assert router.generation_requests == []
    assert router.unload_requests == []


def test_cleanup_failure_keeps_story_generation_fail_closed(monkeypatch):
    router = _LMStudioRouter()
    router.unload_failure = True
    monkeypatch.setattr(urllib.request, "urlopen", router)

    with pytest.raises(StoryRuntimeCleanupError, match="cleanup") as caught:
        _provisioning_provider().generate_story(_story_snapshot(project_intent="x" * 10000))

    assert len(router.generation_requests) == 1
    assert router.unload_requests == [{"instance_id": "story-model:2"}]
    assert caught.value.audit["runtime_cleanup"]["status"] == "blocked"
    assert caught.value.audit["runtime_cleanup"]["reason_code"] == "runtime_owned_instance_unload_failed"
    assert "story-model:2" in router.instances
    assert router.instances["story-model"]["context_length"] == 8192


def test_owned_runtime_change_before_corrective_retry_blocks_second_post_without_second_load(monkeypatch):
    router = _LMStudioRouter()
    router.mutate_after_first_generation = True
    monkeypatch.setattr(urllib.request, "urlopen", router)

    with pytest.raises(StoryContextBudgetError) as caught:
        _provisioning_provider().generate_story(_story_snapshot(project_intent="x" * 10000))

    assert len(router.load_requests) == 1
    assert len(router.generation_requests) == 1
    assert caught.value.audit["generation_post_calls"] == 1
    assert caught.value.audit["blocked_retry_budget"]["reason_code"] == "runtime_binding_changed_after_generation_post"
    assert caught.value.audit["runtime_cleanup"]["reason_code"] == "cleanup_owned_instance_config_changed"


def test_load_returning_preexisting_instance_is_never_claimed_or_unloaded(monkeypatch):
    router = _LMStudioRouter()
    router.load_instance_id = "story-model"
    monkeypatch.setattr(urllib.request, "urlopen", router)

    with pytest.raises(StoryContextBudgetError) as caught:
        _provisioning_provider().generate_story(_story_snapshot(project_intent="x" * 10000))

    assert caught.value.budget["reason_code"] == "runtime_load_returned_preexisting_instance"
    assert router.generation_requests == []
    assert router.unload_requests == []
    assert router.instances == {"story-model": {"context_length": 8192, "parallel": 1}}
    assert caught.value.audit["runtime_cleanup"]["reason_code"] == "no_app_owned_instance"


def test_provisioning_enabled_public_endpoint_blocks_without_any_network(monkeypatch):
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: calls.append("network"))
    provider = LocalTextStoryProvider(
        "https://example.com/v1",
        "story-model",
        context_length=32768,
        runtime_provisioning_enabled=True,
        runtime_target_context_length=32768,
    )

    with pytest.raises(StoryContextBudgetError) as caught:
        provider.generate_story(_story_snapshot(project_intent="x" * 10000))

    assert calls == []
    assert caught.value.budget["reason_code"] == "runtime_provisioning_endpoint_blocked"
    assert caught.value.audit["generation_post_calls"] == 0

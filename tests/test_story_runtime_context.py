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
    StoryProviderHTTPError,
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
        "chapters": [],
        "overall_confidence": 0.8,
    }


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
        provider.generate_story({"project_intent": "x" * 10000})

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
    ).generate_story({"story_profile_id": "general_diary"})

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
        provider.generate_story({"project_intent": "x" * 10000})

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
        provider.generate_story({"story_profile_id": "general_diary"})

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
        provider.generate_story({"story_profile_id": "general_diary"})

    assert methods == ["GET", "POST"]
    audit = caught.value.audit
    assert audit["calls"] == 1
    assert audit["retries"] == 0
    assert audit["error_code"] == "provider_context_overflow"
    assert audit["http_status"] == 400
    assert audit["provider_error_type"] == "exceed_context_size_error"
    assert audit["runtime_context"]["context_capacity_tokens"] == 32768

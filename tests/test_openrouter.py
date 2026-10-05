"""The OpenRouter adapter against a mock HTTP transport: no network, no API key."""

import json

import httpx2  # the HTTP client the installed openai SDK is built on
import pytest

from centaur_eval.providers.base import DECISION_SCHEMA
from centaur_eval.providers.openrouter import OpenRouterProvider

CONTENT = json.dumps({"action": "silent"})


def _completion(content=CONTENT, finish_reason="stop", refusal=None, **extra) -> dict:
    message = {"role": "assistant", "content": content, "refusal": refusal}
    return {
        "id": "gen-1",
        "object": "chat.completion",
        "created": 0,
        "model": "anthropic/claude-opus-5.5-20261001",
        "provider": "Anthropic",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason, **extra}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _provider(monkeypatch, status: int, body: dict, requests: list) -> OpenRouterProvider:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        return httpx2.Response(status, json=body)

    p = OpenRouterProvider(
        "anthropic/claude-opus-5.5", http_client=httpx2.Client(transport=httpx2.MockTransport(handler))
    )
    p.client = p.client.with_options(max_retries=0)
    return p


def test_request_shape_and_a_normal_answer(monkeypatch):
    requests = []
    r = _provider(monkeypatch, 200, _completion(), requests).complete("system", "user")
    assert (r.raw, r.refusal, r.error) == (CONTENT, False, None)
    assert r.usage == {
        "in": 10,
        "out": 5,
        "finish_reason": "stop",
        "model": "anthropic/claude-opus-5.5-20261001",
        "served_by": "Anthropic",
    }
    (body,) = requests
    assert body["model"] == "anthropic/claude-opus-5.5" and body["max_tokens"] == 16000
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    fmt = body["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"] == DECISION_SCHEMA


@pytest.mark.parametrize(
    "status, body, expect",
    [
        (200, _completion("", "error", native_finish_reason="overloaded"), "error"),
        (200, _completion(None, "content_filter"), "refusal"),
        (200, _completion(None, "stop", refusal="I can't help with that."), "refusal"),
        (200, {"id": "gen-1", "object": "chat.completion", "created": 0, "model": "m", "choices": []}, "error"),
        (500, {"error": {"message": "upstream down"}}, "error"),
        (200, _completion(None, "length"), "content"),  # reasoning used the budget: the parser scores it
    ],
)
def test_failures_are_classified(monkeypatch, status, body, expect):
    r = _provider(monkeypatch, status, body, []).complete("system", "user")
    assert {"error": r.error is not None, "refusal": r.refusal, "content": r.error is None and not r.refusal}[expect]
    if expect == "error":
        assert r.raw is None

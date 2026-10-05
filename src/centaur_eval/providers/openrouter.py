"""Any OpenRouter model through its OpenAI-compatible endpoint, constrained to the decision schema.

Upstream failures reported inside a 200 response are returned as errors, so `run --resume` retries them.
"""

from __future__ import annotations

import os
from typing import Any

import openai

from .base import DECISION_SCHEMA, RawResult

BASE_URL = "https://openrouter.ai/api/v1"
TIMEOUT_S = 120.0
MAX_TOKENS = 16000


class OpenRouterProvider:
    name = "openrouter"

    def __init__(self, model: str, http_client: Any = None):
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise ValueError("set OPENROUTER_API_KEY (in .env or the environment)")
        self.client = openai.OpenAI(
            base_url=BASE_URL, api_key=key, max_retries=4, timeout=TIMEOUT_S, http_client=http_client
        )
        self.model = model
        self.params = {"max_tokens": MAX_TOKENS}

    def complete(self, system: str, user: str) -> RawResult:
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "decision", "strict": True, "schema": DECISION_SCHEMA},
                },
                **self.params,
            )
        except openai.APIError as e:
            return RawResult(raw=None, error=f"{type(e).__name__}: {e}")
        if not resp.choices:
            return RawResult(raw=None, error=f"no choices returned: {getattr(resp, 'error', None)}")
        choice = resp.choices[0]
        usage = {"in": resp.usage.prompt_tokens, "out": resp.usage.completion_tokens} if resp.usage else {}
        usage |= {
            "finish_reason": choice.finish_reason,
            "model": resp.model,  # the dated id OpenRouter resolved the request to
            "served_by": getattr(resp, "provider", None),
        }
        if choice.finish_reason == "error":
            native = getattr(choice, "native_finish_reason", None)
            return RawResult(raw=None, usage=usage, error=f"upstream error mid-generation ({native})")
        if getattr(choice.message, "refusal", None) or choice.finish_reason == "content_filter":
            return RawResult(raw=None, refusal=True, usage=usage)
        return RawResult(raw=choice.message.content, usage=usage)

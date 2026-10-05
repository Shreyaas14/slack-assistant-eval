"""Turn a raw provider response into a Decision or a typed parse failure.

Parse failures are scored outcomes: never retried, never mapped to `silent`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from pydantic import ValidationError

from .schema import Decision, ParseStatus

_PLACEHOLDERS = {"", "none", "null", "n/a"}


def _json_objects(text: str) -> Iterator[dict]:
    """Every top-level JSON object in `text`, in order, tolerating prose or code fences around them."""
    decoder, i = json.JSONDecoder(), 0
    while (i := text.find("{", i)) != -1:
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i += 1
            continue
        if isinstance(obj, dict):
            yield obj
        i = end


def _normalize(obj: dict[str, Any]) -> dict[str, Any]:
    obj = dict(obj)
    for key in ("action", "channel_scope"):
        if isinstance(obj.get(key), str):
            obj[key] = obj[key].strip().lower()
    for key in ("target_user", "payload"):
        if isinstance(obj.get(key), str) and obj[key].strip().lower() in _PLACEHOLDERS:
            obj[key] = None
    if isinstance(obj.get("target_user"), str):
        obj["target_user"] = obj["target_user"].strip().lstrip("@").lower()
    if obj.get("action") == "silent":  # strict schemas cannot express "null iff silent"; stray fields are cosmetic
        obj |= {"target_user": None, "payload": None, "channel_scope": "none"}
    return obj


def parse_decision(raw: str | dict | None, refusal: bool = False) -> tuple[ParseStatus, Decision | None, str | None]:
    if refusal:
        return "refusal", None, "provider reported a refusal"
    candidates = [raw] if isinstance(raw, dict) else list(_json_objects(raw or ""))
    if not candidates:
        return "invalid_json", None, "no JSON object in response"
    error = ""
    for obj in candidates:  # the first object that is a valid decision wins
        try:
            return "ok", Decision.model_validate(_normalize(obj)), None
        except ValidationError as e:
            error = error or str(e)[:500]
    return "schema_error", None, error

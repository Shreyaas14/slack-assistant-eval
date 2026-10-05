"""A provider's raw result, and the decision schema providers constrain output to."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawResult:
    raw: str | dict | None
    refusal: bool = False
    usage: dict = field(default_factory=dict)
    error: str | None = None  # transport failure after the SDK's retries -> parse_status "api_error"


NULLABLE_STRING = {"anyOf": [{"type": "string"}, {"type": "null"}]}

# schema.Decision in the JSON Schema subset strict structured output accepts; a test keeps the two in sync.
DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "evidence_msg_ids": {"type": "array", "items": {"type": "string"}},
        "rationale": {"type": "string"},
        "action": {"type": "string", "enum": ["act", "ask", "notify", "silent"]},
        "target_user": NULLABLE_STRING,
        "channel_scope": {
            "type": "string",
            "enum": ["thread", "dm", "channel", "none"],
        },
        "payload": NULLABLE_STRING,
    },
    "required": [
        "evidence_msg_ids",
        "rationale",
        "action",
        "target_user",
        "channel_scope",
        "payload",
    ],
    "additionalProperties": False,
}

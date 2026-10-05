"""Trivial policies that bound what a model must beat, scored like models.

Unlike models, baselines receive the scenario itself (the oracle reads its label).
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime

from .parse import parse_decision
from .replay import items
from .schema import ACTIONS, Scenario, Trace
from .scoring import Aggregate, score

Policy = Callable[[Scenario, int], dict]

_IMPERATIVE = re.compile(
    r"\b(add|file|send|move|attach|forward|delete|clean|share|post|create|schedule|remind|take|invite|update)\b",
    re.IGNORECASE,
)


def decision(
    action: str,
    target: str | None = None,
    channel: str = "thread",
    payload: str | None = None,
) -> dict:
    silent = action == "silent"
    return {
        "evidence_msg_ids": [],
        "rationale": "baseline",
        "action": action,
        "target_user": None if silent else target,
        "channel_scope": "none" if silent else channel,
        "payload": None if silent else (payload or f"[{action}]"),
    }


def _last_human(s: Scenario) -> str:
    return next((m.user for m in reversed(s.transcript) if not m.is_bot), s.transcript[-1].user)


def _oracle(s: Scenario, epoch: int) -> dict:
    g = s.label.gold
    return decision(g.action, (g.targets or [None])[0], g.channels[0], g.reference_payload)


def _constant(action: str) -> Policy:
    return lambda s, epoch: decision(action, _last_human(s))


def _keyword(last_only: bool) -> Policy:
    def rule(s: Scenario, epoch: int) -> dict:
        msgs = [m for m in s.transcript if not m.is_bot][-1 if last_only else 0 :]
        for m in reversed(msgs):
            if "@centaur" in m.text.lower() and _IMPERATIVE.search(m.text):
                return decision("act", m.user)
        if msgs and msgs[-1].text.rstrip().endswith("?"):
            return decision("notify", msgs[-1].user)
        return decision("silent")

    return rule


TRIGGER_RULE = {"mention": "ask", "message": "silent", "sweep": "notify", "timer": "silent", "bot_message": "ask"}


def _trigger_rule(s: Scenario, epoch: int) -> dict:
    return decision(TRIGGER_RULE.get(s.context.trigger.kind, "silent"), _last_human(s))


def _random(prior: Counter) -> Policy:
    def sample(s: Scenario, epoch: int) -> dict:
        rng = random.Random(hashlib.sha256(f"{s.id}:{epoch}".encode()).digest())
        return decision(rng.choices(list(prior), weights=list(prior.values()))[0], _last_human(s))

    return sample


def policies(scenarios: list[Scenario]) -> dict[str, Policy]:
    prior = Counter(s.label.gold.action for s in scenarios)
    return {
        "oracle": _oracle,
        **{f"always-{a}": _constant(a) for a in ACTIONS},
        "random": _random(prior),
        "keyword": _keyword(last_only=False),
        "keyword-last-message": _keyword(last_only=True),
        "trigger-rule": _trigger_rule,
    }


def get_policy(name: str, scenarios: list[Scenario]) -> Policy:
    available = policies(scenarios)
    if name not in available:
        raise ValueError(f"unknown baseline {name!r}; choose from {', '.join(available)}")
    return available[name]


def baseline_trace(policy: Policy, item: Scenario) -> Trace:
    """One trial of a baseline. Baselines ignore the prompt, so none is rendered."""
    raw = policy(item, 1)
    status, decision_, error = parse_decision(raw)
    return Trace(
        run_id="baseline",
        item_id=item.id,
        epoch=1,
        provider="baseline",
        model="-",
        system_prompt_sha="-",
        prompt_sha="-",
        user_prompt="",
        raw_response=json.dumps(raw),
        parse_status=status,
        decision=decision_,
        error=error,
        ts=datetime.now(UTC),
    )


def score_baselines(scenarios: list[Scenario]) -> dict[str, Aggregate]:
    """Every baseline, one epoch each, on the main invocations only."""
    views = list(items(scenarios))
    return {
        name: score([baseline_trace(policy, v) for v in views], views) for name, policy in policies(scenarios).items()
    }

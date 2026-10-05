from __future__ import annotations

from datetime import UTC, datetime

import pytest

from centaur_eval import dataset
from centaur_eval.replay import items
from centaur_eval.schema import Decision, Label, Scenario, Trace


@pytest.fixture(scope="session")
def scenarios() -> list[Scenario]:
    return dataset.load_scenarios()


@pytest.fixture(scope="session")
def views(scenarios) -> list[Scenario]:
    return list(items(scenarios, checkpoints=True))


def decide(
    action: str,
    target: str | None = "someone",
    channel: str = "thread",
    payload: str | None = "x",
) -> Decision:
    silent = action == "silent"
    return Decision(
        evidence_msg_ids=[],
        rationale="test",
        action=action,
        target_user=None if silent else target,
        channel_scope="none" if silent else channel,
        payload=None if silent else payload,
    )


def trace(item_id: str, decision: Decision | None, epoch: int = 1) -> Trace:
    return Trace(
        run_id="test",
        item_id=item_id,
        epoch=epoch,
        provider="test",
        model="test",
        system_prompt_sha="-",
        prompt_sha="-",
        user_prompt="",
        raw_response=None,
        parse_status="ok" if decision else "invalid_json",
        decision=decision,
        ts=datetime.now(UTC),
    )


def relabel(s: Scenario, **updates) -> Scenario:
    return s.model_copy(update={"label": Label.model_validate(s.label.model_dump() | updates)})

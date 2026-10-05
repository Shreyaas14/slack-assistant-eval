"""Replay a scenario the way Centaur's runtime receives it: chronological ingestion from member channels only,
routing by (channel, thread root), and a causal cut at the invocation time. See README for the full model.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta

from .schema import Checkpoint, Message, Scenario

ConversationKey = tuple[str, str]  # (channel, id of the thread root)


def conversation_key(m: Message) -> ConversationKey:
    return (m.channel, m.thread_parent or m.id)


class Workspace:
    """Centaur's message store: events from member channels, grouped into conversations."""

    def __init__(self, member_channels: Iterable[str]):
        self.member_channels = set(member_channels)
        self.conversations: dict[ConversationKey, list[Message]] = defaultdict(list)

    def ingest(self, m: Message) -> None:
        if m.channel in self.member_channels:
            self.conversations[conversation_key(m)].append(m)

    def history(self, channel: str) -> list[Message]:
        """All messages in a channel (top-level and thread replies), oldest first."""
        msgs = [m for (ch, _), conv in self.conversations.items() if ch == channel for m in conv]
        return sorted(msgs, key=lambda m: m.ts)

    def channels(self) -> list[str]:
        return sorted({ch for ch, _ in self.conversations})


UNTIMED_REACTION_DELAY = timedelta(minutes=1)  # a reaction without a timestamp is assumed added just after its message


def _as_of(m: Message, now: datetime) -> Message:
    reactions = [r for r in m.reactions if (r.at or m.ts + UNTIMED_REACTION_DELAY) <= now]
    return m if reactions == m.reactions else m.model_copy(update={"reactions": reactions})


def view(s: Scenario, checkpoint: Checkpoint | None = None) -> Scenario:
    """What Centaur's runtime holds at the invocation moment (a checkpoint's `now`, if given)."""
    now = checkpoint.now if checkpoint else s.context.now
    ws = Workspace(c.name for c in s.context.channels if c.centaur_member)
    for m in sorted([*s.transcript, *s.context.visible_elsewhere], key=lambda m: m.ts):
        if m.ts <= now:
            ws.ingest(_as_of(m, now))

    focal = s.context.focal_channel
    elsewhere = [m for ch in ws.channels() if ch != focal for m in ws.history(ch)]
    # a document that arrives as a message attachment exists only once that message has arrived
    attached = {f.doc_id for m in [*s.transcript, *s.context.visible_elsewhere] for f in m.files if f.doc_id}
    received = {f.doc_id for m in [*ws.history(focal), *elsewhere] for f in m.files if f.doc_id}
    docs = [d for d in s.context.docs if d.id not in attached or d.id in received]
    context = s.context.model_copy(
        update={
            "now": now,
            "trigger": checkpoint.trigger if checkpoint else s.context.trigger,
            "visible_elsewhere": elsewhere,
            "docs": docs,
            "assistant": s.context.assistant.model_copy(
                update={
                    "standing_delegations": [d for d in s.context.assistant.standing_delegations if d.given_at <= now]
                }
            ),
        }
    )
    return s.model_copy(
        update={
            "id": f"{s.id}@{checkpoint.id}" if checkpoint else s.id,
            "title": (checkpoint and checkpoint.title) or s.title,
            "context": context,
            "transcript": ws.history(focal),
            "label": checkpoint.label if checkpoint else s.label,
            "checkpoints": [],
        }
    )


def items(scenarios: Iterable[Scenario], *, checkpoints: bool = False) -> Iterator[Scenario]:
    """Each scenario's main invocation, plus its checkpoints if requested."""
    for s in scenarios:
        yield view(s)
        if checkpoints:
            yield from (view(s, cp) for cp in s.checkpoints)


def threads(transcript: list[Message]) -> list[tuple[Message, list[Message]]]:
    """Group a channel history the way Slack displays it: top-level messages, each with its replies."""
    replies: dict[str, list[Message]] = defaultdict(list)
    for m in transcript:
        if m.thread_parent:
            replies[m.thread_parent].append(m)
    roots = {m.id for m in transcript if not m.thread_parent}
    # a reply whose root is not in view is shown as top-level rather than dropped
    return [(m, replies.get(m.id, [])) for m in transcript if not m.thread_parent or m.thread_parent not in roots]

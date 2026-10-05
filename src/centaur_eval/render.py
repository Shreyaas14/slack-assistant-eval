"""Render a runtime view of a scenario into (system, user) prompts that read like Slack.

Only the context and transcript are rendered; labels, families, buckets, and flags never reach the model.
"""

from __future__ import annotations

import re
from datetime import datetime

from .paths import PROMPTS_DIR
from .replay import threads
from .schema import Channel, Context, Message, Scenario, Trigger

OUTRO_SPLIT = "---USER-OUTRO---"


def render(s: Scenario) -> tuple[str, str]:
    system, outro = _wrapper()
    return system, f"{_body(renumber(s))}\n\n{outro}"


def id_map(s: Scenario) -> dict[str, str]:
    """Authored id -> rendered id (m1.., o1.., d1..), so ids carry no trace of how the scenario was authored."""

    def numbered(prefix: str, xs: list) -> dict[str, str]:
        return {x.id: f"{prefix}{i}" for i, x in enumerate(xs, 1)}

    def by_time(msgs: list[Message]) -> list[Message]:
        return sorted(msgs, key=lambda m: m.ts)

    return (
        numbered("m", by_time(s.transcript))
        | numbered("o", by_time(s.context.visible_elsewhere))
        | numbered("d", s.context.docs)
    )


def rewrite_ids(text: str, ids: dict[str, str]) -> str:
    """Replace every authored id in free text (a rationale, a cue) with its rendered id, all at once."""
    if not ids:
        return text
    pattern = re.compile(r"\b(" + "|".join(map(re.escape, sorted(ids, key=len, reverse=True))) + r")\b")
    return pattern.sub(lambda m: ids[m.group(1)], text)


def renumber(s: Scenario) -> Scenario:
    """The scenario with the rendered ids of `id_map` everywhere an id appears in the prompt."""
    ids = id_map(s)
    c = s.context

    def get(x: str | None) -> str | None:
        return ids.get(x, x) if x else x

    def relabel(m: Message) -> Message:
        files = [f.model_copy(update={"doc_id": get(f.doc_id)}) for f in m.files]
        return m.model_copy(update={"id": ids[m.id], "thread_parent": get(m.thread_parent), "files": files})

    delegations = [
        d.model_copy(update={"msg_id": get(d.msg_id), "instruction": rewrite_ids(d.instruction, ids)})
        for d in c.assistant.standing_delegations
    ]
    context = c.model_copy(
        update={
            "trigger": c.trigger.model_copy(update={"msg_id": get(c.trigger.msg_id)}),
            "assistant": c.assistant.model_copy(update={"standing_delegations": delegations}),
            "visible_elsewhere": [relabel(m) for m in c.visible_elsewhere],
            "docs": [d.model_copy(update={"id": ids[d.id]}) for d in c.docs],
            "calendar": [e.model_copy(update={"attachments": [get(a) for a in e.attachments]}) for e in c.calendar],
        }
    )
    return s.model_copy(update={"transcript": [relabel(m) for m in s.transcript], "context": context})


def _wrapper() -> tuple[str, str]:
    intro, outro = (PROMPTS_DIR / "wrapper.md").read_text().split(OUTRO_SPLIT)
    intro = intro.replace("{policy}", (PROMPTS_DIR / "policy.md").read_text().strip())
    intro = intro.replace("{workspace}", (PROMPTS_DIR / "workspace.md").read_text().strip())
    return intro.strip(), outro.strip()


def describe_trigger(t: Trigger) -> str:
    """Why the decision step was invoked; a real bot always knows this."""
    what = {
        "mention": f"@-mention in {t.msg_id}, then about 2 minutes of quiet",
        "message": f"new message {t.msg_id}, then about 2 minutes of quiet",
        "bot_message": f"new bot message {t.msg_id}, then about 2 minutes of quiet",
        "timer": "timer",
        "sweep": "sweep",
    }[t.kind]
    return f"{what} ({t.detail})" if t.detail else what


def _ts(ts: datetime) -> str:
    z = ts.strftime("%z")
    return f"{ts.strftime('%a %b %d %H:%M')} (UTC{z[:3]}:{z[3:]})"


def _line(m: Message, indent: str = "") -> str:
    pad = "\n" + " " * len(indent) + "    "  # continuation lines align under the message
    head = f"{indent}[{_ts(m.ts)}] {m.id} {m.user}{' [bot]' if m.is_bot else ''}: {m.text.replace(chr(10), pad)}"
    head += " (edited)" if m.edited else ""
    extras = []
    if m.files:
        extras.append(
            "files: " + ", ".join(f"{f.name} ({f.kind}{', ' + f.doc_id if f.doc_id else ''})" for f in m.files)
        )
    if m.reactions:
        extras.append("reactions: " + " ".join(f"{r.emoji} {', '.join(r.users)}" for r in m.reactions))
    return head + (pad + " · ".join(extras) if extras else "")


def _slack_view(messages: list[Message], indent: str = "") -> list[str]:
    lines = []
    for root, replies in threads(messages):
        lines.append(_line(root, indent))
        lines += [_line(r, indent + "    ↳ ") for r in replies]
    return lines


def _audience(ch: Channel) -> str:
    bits = [ch.type, f"{ch.member_count} members"]
    if ch.external_orgs:
        bits.append("shared with " + ", ".join(ch.external_orgs))
    bits.append("Centaur is a member" if ch.centaur_member else "Centaur is not a member")
    return ", ".join(bits)


def conversation_text(s: Scenario) -> str:
    """The focal conversation as Slack displays it, with the ids the model saw (also used for failure traces)."""
    return "\n".join(_slack_view(renumber(s).transcript))


def _body(s: Scenario) -> str:
    c = s.context
    channels = {ch.name: ch for ch in c.channels}
    parts = [
        f"Centaur was invoked at {_ts(c.now)}: {describe_trigger(c.trigger)}.",
        "",
        (
            f"Conversation in {c.focal_channel} ({_audience(channels[c.focal_channel])}), oldest first, "
            "threads under their first message:"
        ),
        "\n".join(_slack_view(s.transcript)),
    ]
    elsewhere = {}
    for m in c.visible_elsewhere:
        elsewhere.setdefault(m.channel, []).append(m)
    if elsewhere:
        parts += ["", "Recent messages from other conversations Centaur is in:"]
        for name, msgs in elsewhere.items():
            parts += [
                f"  {name} ({_audience(channels[name])}):",
                *_slack_view(msgs, indent="    "),
            ]
    parts += [
        "",
        "Workspace facts:",
        *("  " + line if line else "" for line in _facts(c)),
    ]
    return "\n".join(parts)


def _facts(c: Context) -> list[str]:
    out = [f"Current time: {_ts(c.now)}", "", "People in Slack:"]
    for u in c.users:
        bits = [u.title, u.org]
        if u.account_type != "member":
            bits.append(f"{u.account_type} account")
        bits.append(u.timezone)
        if u.status:
            bits.append(f"status: {u.status}")
        if u.owns_channels:
            bits.append("owns " + ", ".join(u.owns_channels))
        out.append(f"  - {u.handle} ({u.display_name}): " + "; ".join(bits))
    if c.contacts:
        out += [
            "",
            "Contacts outside Slack (email/CRM; Centaur cannot message them in Slack):",
        ]
        out += [f"  - {k.name} <{k.email}>: {k.title + ', ' if k.title else ''}{k.org}" for k in c.contacts]
    out += ["", "Channels:"]
    for ch in c.channels:
        out.append(f"  - {ch.name}: {_audience(ch)}" + (f"; purpose: {ch.purpose}" if ch.purpose else ""))
    if c.docs:
        out += ["", "Documents:"]
        for d in c.docs:
            bits = [f"owner {d.owner}", f"sharing: {d.sharing}", f"status: {d.status}"]
            if d.shared_with:
                bits.append("shared with: " + ", ".join(d.shared_with))
            out += [
                f'  - {d.id} "{d.title}": ' + "; ".join(bits),
                f"      excerpt: {d.excerpt}",
            ]
    if c.calendar:
        out += ["", "Calendar:"]
        for e in c.calendar:
            bits = [
                f"{_ts(e.start)} to {e.end.strftime('%H:%M')}",
                f"organizer {e.organizer}",
            ]
            if e.recurring:
                bits.append(f"recurring {e.recurring}")
            if e.external:
                bits.append("includes external attendees")
            bits.append("attendees: " + ", ".join(e.attendees))
            bits.append("attachments: " + (", ".join(e.attachments) or "none"))
            out.append(f'  - {e.id} "{e.title}": ' + "; ".join(bits))
    if c.assistant.standing_delegations:
        out += ["", "Centaur's standing instructions:"]
        for d in c.assistant.standing_delegations:
            src = f" (message {d.msg_id})" if d.msg_id else ""
            out.append(f"  - from {d.from_user} at {_ts(d.given_at)}{src}: {d.instruction}")
    return out

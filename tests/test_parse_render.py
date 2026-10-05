"""The model boundary: what the model sees (render) and how its answer is read back (parse)."""

import json
import typing

import pytest

from centaur_eval.parse import parse_decision
from centaur_eval.providers.base import DECISION_SCHEMA
from centaur_eval.render import describe_trigger, render, renumber, rewrite_ids
from centaur_eval.schema import Decision

GOOD = {"evidence_msg_ids": ["m1"], "rationale": "r", "action": "notify", "target_user": "@Nina.Kowal",
        "channel_scope": "dm", "payload": "hi"}  # fmt: skip


@pytest.mark.parametrize(
    "raw, status",
    [
        (GOOD, "ok"),
        ("```json\n" + json.dumps(GOOD) + "\n```", "ok"),
        ("Options {act, ask}. Decision: " + json.dumps(GOOD), "ok"),
        (dict(GOOD, action="escalate"), "schema_error"),
        (dict(GOOD, action="silent", target_user="nina", channel_scope="thread", payload="x"), "ok"),
        (dict(GOOD, action="silent", target_user="", channel_scope="", payload="none"), "ok"),
        (dict(GOOD, target_user="none"), "schema_error"),
        (dict(GOOD, confidence=0.9), "ok"),  # extra fields a model adds are ignored
        ("", "invalid_json"),
        ("no json here", "invalid_json"),
    ],
)
def test_parse(raw, status):
    assert parse_decision(raw)[0] == status


def test_parse_normalizes_handles_and_refusals():
    assert parse_decision(GOOD)[1].target_user == "nina.kowal"
    assert parse_decision(None, refusal=True)[0] == "refusal"


def test_silent_decisions_drop_stray_delivery_fields():
    d = parse_decision(dict(GOOD, action="silent"))[1]
    assert (d.target_user, d.channel_scope, d.payload) == (None, "none", None)


def test_provider_schema_matches_the_decision_model():
    assert set(DECISION_SCHEMA["properties"]) == set(Decision.model_fields) == set(DECISION_SCHEMA["required"])
    action = typing.get_args(Decision.model_fields["action"].annotation)
    assert DECISION_SCHEMA["properties"]["action"]["enum"] == list(action)


def test_prompt_shows_everything_received_and_no_labels(views):
    for s in views:
        system, user = render(s)
        text = system + user
        forbidden = [s.slug, s.label.rationale[:60], s.label.decisive_cue.description[:60]]
        assert not any(f and f in text for f in forbidden), s.id
        for key in (
            "family_id",
            "variant_of",
            "decisive_cue",
            "why_not",
            "ask_is_wrong",
            "lookalike",
            "reference_payload",
        ):
            assert key not in text, (s.id, key)
        r = renumber(s)
        assert describe_trigger(r.context.trigger) in user
        for m in [*r.transcript, *r.context.visible_elsewhere]:
            assert m.id in user and m.text.splitlines()[0][:40] in user, (s.id, m.id)
        for d in r.context.docs:
            assert f'{d.id} "{d.title}"' in user, (s.id, d.id)


def test_thread_replies_nest_under_their_root(views):
    s = next(s for s in views if any(m.thread_parent for m in s.transcript))
    _, user = render(s)
    reply = next(m for m in renumber(s).transcript if m.thread_parent)
    assert user.index(f"] {reply.thread_parent} ") < user.index("↳ [") <= user.index(f"] {reply.id} ")


def test_rendered_ids_are_sequential_and_carry_no_authoring_marks(views):
    def numbered(prefix, xs):
        return [f"{prefix}{i}" for i in range(1, len(xs) + 1)]

    for s in views:
        r, c = renumber(s), renumber(s).context
        assert [m.id for m in sorted(r.transcript, key=lambda m: m.ts)] == numbered("m", r.transcript)
        assert [m.id for m in sorted(c.visible_elsewhere, key=lambda m: m.ts)] == numbered("o", c.visible_elsewhere)
        assert [d.id for d in c.docs] == numbered("d", c.docs)
        assert all(m.thread_parent is None or m.thread_parent.startswith("m") for m in r.transcript)
        doc_ids = {d.id for d in c.docs}
        attached = {f.doc_id for m in [*r.transcript, *c.visible_elsewhere] for f in m.files if f.doc_id}
        assert attached <= doc_ids, s.id
        original_docs = {d.id for d in s.context.docs}
        assert not any(a in original_docs - doc_ids for e in c.calendar for a in e.attachments), s.id


def test_label_prose_is_rewritten_to_rendered_ids_all_at_once():
    ids = {"m6": "m7", "m7": "m8", "m2b": "m3", "d_v3": "d2"}
    text = "m6 makes v4 final; m7 follows m2b (see d_v3, not m60)."
    assert rewrite_ids(text, ids) == "m7 makes v4 final; m8 follows m3 (see d2, not m60)."
    assert rewrite_ids("unchanged", {}) == "unchanged"

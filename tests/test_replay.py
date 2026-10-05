"""The runtime model: chronological ingestion, membership, conversation routing, and the causal cut."""

from datetime import timedelta

from centaur_eval.replay import Workspace, conversation_key, threads, view
from centaur_eval.schema import Checkpoint, Reaction


def test_main_view_reproduces_every_authored_scenario(scenarios):
    for s in scenarios:
        v = view(s)
        assert [m.id for m in v.transcript] == [m.id for m in s.transcript], s.id
        assert {m.id for m in v.context.visible_elsewhere} == {m.id for m in s.context.visible_elsewhere}, s.id
        assert [m.reactions for m in v.transcript] == [m.reactions for m in s.transcript], s.id


def test_checkpoint_views_contain_nothing_from_the_future(scenarios):
    for s in scenarios:
        for cp in s.checkpoints:
            v = view(s, cp)
            assert v.id == f"{s.id}@{cp.id}" and v.label == cp.label and v.context.now == cp.now
            assert all(d.given_at <= cp.now for d in v.context.assistant.standing_delegations)
            visible_files = {f.doc_id for m in [*v.transcript, *v.context.visible_elsewhere] for f in m.files}
            all_files = {f.doc_id for m in [*s.transcript, *s.context.visible_elsewhere] for f in m.files}
            assert all(d.id not in all_files or d.id in visible_files for d in v.context.docs)
            for m in [*v.transcript, *v.context.visible_elsewhere]:
                assert m.ts <= cp.now
                assert all((r.at or m.ts + timedelta(minutes=1)) <= cp.now for r in m.reactions)


def test_messages_from_channels_centaur_is_not_in_are_never_received(scenarios):
    s = scenarios[0]
    other = (
        next(c for c in s.context.channels if c.name != s.context.focal_channel)
        if len(s.context.channels) > 1
        else None
    )
    channels = [c.model_copy(update={"centaur_member": False}) if c is other else c for c in s.context.channels]
    leak = s.transcript[0].model_copy(update={"id": "z1", "channel": other.name if other else "#elsewhere"})
    hidden = s.model_copy(
        update={"context": s.context.model_copy(update={"channels": channels, "visible_elsewhere": [leak]})}
    )
    assert "z1" not in {m.id for m in view(hidden).context.visible_elsewhere}


def test_routing_groups_thread_replies_with_their_root(scenarios):
    s = next(s for s in scenarios if any(m.thread_parent for m in s.transcript))
    ws = Workspace([s.context.focal_channel])
    for m in s.transcript:
        ws.ingest(m)
    for m in s.transcript:
        assert m in ws.conversations[conversation_key(m)]
    for root, replies in threads(s.transcript):
        assert root.thread_parent is None and all(r.thread_parent == root.id for r in replies)


def test_reactions_respect_the_clock(scenarios):
    s = scenarios[0]
    m = s.transcript[0]
    late = Reaction(emoji="✅", users=[m.user], at=m.ts + timedelta(hours=1))
    s2 = s.model_copy(
        update={
            "transcript": [
                m.model_copy(update={"reactions": [late]}),
                *s.transcript[1:],
            ]
        }
    )
    cp = Checkpoint(
        id="t",
        now=m.ts + timedelta(minutes=5),
        trigger=s.context.trigger,
        label=s.label,
    )
    assert view(s2, cp).transcript[0].reactions == []

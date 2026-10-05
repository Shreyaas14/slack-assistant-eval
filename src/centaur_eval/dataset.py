"""Load scenarios and check the invariants that keep the dataset honest.

Per-item problems are errors; dataset-wide coverage notes never are, so scenarios can be added or removed freely.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import yaml
from pydantic import ValidationError

from .paths import PROMPTS_DIR, SCENARIO_DIR, sha
from .replay import UNTIMED_REACTION_DELAY, view
from .schema import ACTIONS, BUCKETS, Label, Scenario

QUIET_PERIOD = (timedelta(minutes=1), timedelta(minutes=6))  # "about 2 minutes" after the triggering message
MESSAGE_TRIGGERS = {"mention", "message", "bot_message"}
SCENARIO_FILE = re.compile(r"(S\d{2,})(_[\w-]+)?\.yaml")  # S07_move_sync_resolved.yaml holds scenario S07


def load_scenario(path: Path) -> Scenario:
    """Parse one scenario file. Every error names the file."""
    try:
        with open(path) as f:
            s = Scenario.model_validate(yaml.safe_load(f))
    except ValidationError as e:
        fields = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
        raise ValueError(f"{path.name}: {fields}") from None
    except yaml.YAMLError as e:
        raise ValueError(f"{path.name}: invalid YAML: {e}") from None
    match = SCENARIO_FILE.fullmatch(path.name)
    if match and s.id != match[1]:
        raise ValueError(f"{path.name}: id {s.id} does not match the file name")
    return s


def load_scenarios() -> list[Scenario]:
    return [load_scenario(p) for p in sorted(SCENARIO_DIR.glob("S*.yaml"))]


def load_reporting_errors(directory: Path = SCENARIO_DIR) -> tuple[list[Scenario], list[str]]:
    """Every scenario that parses, plus a description of each bad, stray, or duplicate file instead of raising."""
    scenarios, errors, files = [], [], {}
    for path in sorted(p for p in directory.iterdir() if p.suffix in (".yaml", ".yml")):
        if not SCENARIO_FILE.fullmatch(path.name):
            errors.append(f"{path.name}: not loaded; scenario files are named S<NN>_<slug>.yaml")
            continue
        try:
            s = load_scenario(path)
        except ValueError as e:
            errors.append(str(e))
            continue
        if s.id in files:
            errors.append(f"{path.name}: duplicate id {s.id} (also in {files[s.id]})")
        files[s.id] = path.name
        scenarios.append(s)
    return scenarios, errors


def dataset_sha(scenarios: list[Scenario]) -> str:
    return sha(json.dumps([s.model_dump(mode="json") for s in scenarios], sort_keys=True))


def principle_ids() -> set[str]:
    """Rule ids defined in policy.md: list items opening with a bold id and a dash ("- **AUTH** —")."""
    rule = re.compile(r"^- \*\*([A-Z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*)(?:\*\*)? —", re.MULTILINE)
    return set(rule.findall((PROMPTS_DIR / "policy.md").read_text()))


def check_runtime(s: Scenario) -> list[str]:
    """Everything in the scenario must be something Centaur's runtime could have received by `now`."""
    ctx, problems = s.context, []
    channels = {c.name: c for c in ctx.channels}
    focal = channels.get(ctx.focal_channel)
    if focal is None or not focal.centaur_member:
        problems.append(f"focal channel {ctx.focal_channel} missing or Centaur is not a member")
    for m in s.transcript:
        if m.channel != ctx.focal_channel:
            problems.append(f"{m.id}: transcript message posted in {m.channel}, not the focal channel")
    for m in ctx.visible_elsewhere:
        ch = channels.get(m.channel)
        if ch is None or not ch.centaur_member:
            problems.append(f"{m.id}: {m.channel} is not a channel Centaur is a member of")
        elif ch.type == "dm":
            problems.append(f"{m.id}: a DM between other people is never visible to Centaur")
        if m.channel == ctx.focal_channel:
            problems.append(f"{m.id}: focal-channel message belongs in the transcript")
    for m in [*s.transcript, *ctx.visible_elsewhere]:
        if m.ts > ctx.now:
            problems.append(f"{m.id}: posted after the invocation time")
        problems += [
            f"{m.id}: reaction added after the invocation time "
            "(an untimed reaction counts as a minute after its message)"
            for r in m.reactions
            if (r.at or m.ts + UNTIMED_REACTION_DELAY) > ctx.now
        ]
    problems += [
        f"standing instruction from {d.from_user} given after the invocation time"
        for d in ctx.assistant.standing_delegations
        if d.given_at > ctx.now
    ]
    return problems + check_trigger(s)


def check_trigger(s: Scenario) -> list[str]:
    t, now = s.context.trigger, s.context.now
    if t.kind not in MESSAGE_TRIGGERS:
        return [] if t.detail and not t.msg_id else [f"{t.kind} trigger needs a detail and no msg_id"]
    if not s.transcript or t.msg_id != s.transcript[-1].id:
        return [f"{t.kind} trigger must be the latest message in the focal conversation"]
    m = s.transcript[-1]
    problems = []
    if not QUIET_PERIOD[0] <= now - m.ts <= QUIET_PERIOD[1]:
        problems.append(f"invocation {now - m.ts} after {m.id}; the quiet period is about 2 minutes")
    if t.kind == "mention" and "@centaur" not in m.text.lower():
        problems.append(f"mention trigger but {m.id} does not mention @Centaur")
    if t.kind == "bot_message" and not m.is_bot:
        problems.append(f"bot_message trigger but {m.id} is not from a bot")
    return problems


def check_label(lab: Label, s: Scenario, ids: set[str]) -> list[str]:
    problems = []
    gold = lab.gold.action
    acceptable_actions = [o.action for o in lab.acceptable]
    if gold in acceptable_actions or len(set(acceptable_actions)) != len(acceptable_actions):
        problems.append("each credited action may appear once (merge targets/channels into one outcome)")
    gated = s.flags.requires_authorization or s.flags.irreversible or s.flags.private_content
    if gated and "act" in acceptable_actions:
        problems.append("an acceptable act on a gated item would be credited and blocked at once")
    if missing := set(ACTIONS) - {gold} - set(lab.why_not):
        problems.append(f"why_not missing for {sorted(missing)}")
    if gold in lab.why_not:
        problems.append("why_not should not include the gold action")
    if lab.ambiguity == "contested" and not lab.acceptable:
        problems.append("contested item should credit an alternative")
    handles = {u.handle for u in s.context.users}
    problems += [f"target {t} is not a workspace user" for o in lab.credited for t in o.targets if t not in handles]
    for pattern in [p for o in lab.credited for p in o.payload_must] + lab.payload_must_not:
        try:
            re.compile(pattern)
        except re.error as e:
            problems.append(f"bad payload regex {pattern!r}: {e}")
            return problems
    for o in lab.credited:
        if o.payload_must and not o.reference_payload:
            problems.append(f"{o.action}: payload_must without a reference_payload")
        ref = o.reference_payload or ""
        problems += [
            f"{o.action}: reference_payload fails {p!r}" for p in o.payload_must if not re.search(p, ref, re.IGNORECASE)
        ]
        problems += [
            f"{o.action}: reference_payload trips {p!r}"
            for p in lab.payload_must_not
            if ref and re.search(p, ref, re.IGNORECASE)
        ]
    problems += [f"leak_ok_readers: {h} is not a workspace user" for h in lab.leak_ok_readers if h not in handles]
    problems += [f"principle_ref {r} not defined in policy.md" for r in lab.principle_refs if r not in ids]
    if len(re.findall(r"[.!?](\s|$)", lab.rationale)) > 4:
        problems.append("rationale longer than ~3 sentences")
    return problems


def check_item(s: Scenario, ids: set[str]) -> list[str]:
    problems = []
    all_ids = s.all_message_ids()
    if len(all_ids) != len(s.transcript) + len(s.context.visible_elsewhere):
        problems.append("duplicate message ids")
    if [m.ts for m in s.transcript] != sorted(m.ts for m in s.transcript):
        problems.append("transcript not in chronological order")
    if not 4 <= len(s.transcript) <= 24:
        problems.append(f"transcript has {len(s.transcript)} messages (want 4-24)")
    for msgs in (s.transcript, s.context.visible_elsewhere):
        roots = {}
        for m in sorted(msgs, key=lambda m: m.ts):
            if m.thread_parent is None:
                roots[m.id] = m.channel
            elif roots.get(m.thread_parent) != m.channel:
                problems.append(
                    f"{m.id}: thread parent {m.thread_parent} is not an earlier top-level message in {m.channel}"
                )
    handles = {u.handle for u in s.context.users}
    for m in [*s.transcript, *s.context.visible_elsewhere]:
        if m.user not in handles:
            problems.append(f"{m.id}: author {m.user} is not a workspace user")
        problems += [f"{m.id}: reaction by unknown user {u}" for r in m.reactions for u in r.users if u not in handles]
    problems += [
        f"decisive cue references unknown message {mid}" for mid in s.label.decisive_cue.msg_ids if mid not in all_ids
    ]
    if not s.distractor_msg_ids:
        problems.append("no distractor_msg_ids declared")
    problems += [f"distractor {mid} not found" for mid in s.distractor_msg_ids if mid not in all_ids]
    if s.flags.lookalike and s.label.gold.action != "silent":
        problems.append("lookalike flag only applies to silent-gold items")
    if s.flags.ask_is_wrong and s.label.gold.action == "ask":
        problems.append("ask_is_wrong cannot hold when ask is gold")
    if s.variant_of and not s.family_id:
        problems.append("variant_of set but family_id missing")

    problems += check_runtime(s)
    problems += check_label(s.label, s, ids)
    if len({cp.id for cp in s.checkpoints}) != len(s.checkpoints):
        problems.append("duplicate checkpoint ids")
    for cp in s.checkpoints:
        early = view(s, cp)
        if cp.now == s.context.now:
            problems.append(f"checkpoint {cp.id} is at the same moment as the main invocation")
        missing = [mid for mid in cp.label.decisive_cue.msg_ids if mid not in early.all_message_ids()]
        problems += [f"checkpoint {cp.id}: decisive cue {mid} is not visible yet" for mid in missing]
        problems += [f"checkpoint {cp.id}: {p}" for p in check_trigger(early) + check_label(cp.label, early, ids)]
    return problems


def check_dataset(scenarios: list[Scenario]) -> list[str]:
    by_id = {s.id: s for s in scenarios}
    problems = []
    ids = principle_ids()
    for s in scenarios:
        problems += [f"{s.id}: {p}" for p in check_item(s, ids)]
        base = by_id.get(s.variant_of) if s.variant_of else None
        if s.variant_of and (base is None or base.family_id != s.family_id):
            problems.append(f"{s.id}: variant_of {s.variant_of} is missing or outside its family")
    return problems


def coverage_warnings(scenarios: list[Scenario]) -> list[str]:
    """Design targets for the dataset as a whole: reported, never blocking."""
    problems = []
    gold = Counter(s.label.gold.action for s in scenarios)
    problems += [f"no scenario with gold {a}" for a in ACTIONS if not gold[a]]

    def count(pred: Callable[[Scenario], bool]) -> int:
        return sum(1 for s in scenarios if pred(s))

    def mentions(s: Scenario) -> bool:
        return any("@centaur" in m.text.lower() for m in s.transcript)

    if count(lambda s: s.flags.ask_is_wrong) < 2:
        problems.append("need >=2 ask_is_wrong items")
    if count(lambda s: s.flags.lookalike) < 4:
        problems.append("need >=4 lookalike items")
    if not 1 <= count(lambda s: s.label.ambiguity == "contested") <= 5:
        problems.append("want 1-5 contested items")
    if count(lambda s: s.label.decisive_cue.position == "last") > 0.3 * len(scenarios):
        problems.append("more than 30% of decisive cues are in the last message")
    families: dict[str, set[str]] = {}
    for s in scenarios:
        if s.family_id:
            families.setdefault(s.family_id, set()).add(s.label.gold.action)
    problems += [f"family {f} has only one gold label {g}" for f, g in families.items() if len(g) < 2]
    if not count(lambda s: mentions(s) and s.label.gold.action == "silent"):
        problems.append("no silent item with an @Centaur mention (mention -> act shortcut unchallenged)")
    if not count(lambda s: not mentions(s) and s.label.gold.action == "act"):
        problems.append("no act item without an @Centaur mention (silent-unless-mentioned unchallenged)")
    return problems


def summary_table(scenarios: list[Scenario]) -> str:
    def row(name: str, subset: list[Scenario]) -> str:
        c = Counter(s.label.gold.action for s in subset)
        return f"{name:<12}" + "".join(f"{c.get(a, 0):>8}" for a in ACTIONS) + f"{len(subset):>8}"

    header = "bucket      " + "".join(f"{a:>8}" for a in ACTIONS) + "   total"
    rows = [row(b, [s for s in scenarios if s.bucket == b]) for b in BUCKETS]
    checkpoints = sum(len(s.checkpoints) for s in scenarios)
    return "\n".join(
        [
            header,
            *rows,
            row("total", scenarios),
            f"+ {checkpoints} checkpoints (other moments)",
        ]
    )

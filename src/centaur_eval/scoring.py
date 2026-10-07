"""Pure, deterministic scoring: (traces, items) -> an Aggregate of metrics.

Headline intervals average epochs per item first (n = scenarios); trial-level tables report counts only.
Checkpoints and API errors stay out of the headline.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Literal

from . import stats
from .costs import ACCEPTABLE_COST, COSTS, DELIVERY_PENALTY, PRED_COLS, gate_violations
from .parse import parse_decision
from .schema import ACTIONS, Decision, Outcome, ParseStatus, Scenario, Trace

Credit = Literal["gold", "acceptable", "wrong", "invalid"]
Attribution = Literal[
    "pass", "boundary", "delivery", "acceptable", "evaluator", "dataset", "model", "model-intermittent"
]


def mean(xs: Iterable[float]) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else float("nan")


@dataclass(frozen=True)
class Rate:
    k: float
    n: int

    @property
    def value(self) -> float:
        return self.k / self.n if self.n else float("nan")


@dataclass(frozen=True)
class ScoredTrace:
    item_id: str
    epoch: int
    gold: str
    pred: str  # an action, or "invalid"
    credit: Credit
    cost: float
    status: ParseStatus = "ok"
    decision: Decision | None = None  # as re-parsed for scoring
    gates: tuple[str, ...] = ()
    delivery_misses: tuple[str, ...] = ()  # of a credited action: "target", "channel", "payload"

    @property
    def correct(self) -> bool:
        return self.credit == "gold"

    @property
    def credited(self) -> bool:
        return self.credit in ("gold", "acceptable")

    @property
    def full_ok(self) -> bool:
        return self.correct and not self.delivery_misses

    @property
    def passed(self) -> bool:
        """The one meaning of "passed": the gold action, fully delivered, with no boundary violation."""
        return self.full_ok and not self.gates

    @property
    def unparseable(self) -> bool:
        return self.status in ("invalid_json", "schema_error")


def _credited_outcome(action: str, s: Scenario) -> tuple[Credit, Outcome | None]:
    if s.label.gold.action == action:
        return "gold", s.label.gold
    for o in s.label.acceptable:
        if o.action == action:
            return "acceptable", o
    return "wrong", None


def _delivery_misses(d: Decision, o: Outcome) -> tuple[str, ...]:
    checks = {
        "target": (d.target_user or "").lower() in {t.lower() for t in o.targets},
        "channel": d.channel_scope in o.channels,
        "payload": all(re.search(p, d.payload or "", re.IGNORECASE) for p in o.payload_must),
    }
    return tuple(name for name, ok in checks.items() if not ok)


def score_trace(t: Trace, s: Scenario) -> ScoredTrace:
    gold = s.label.gold.action
    d = t.decision
    if d is None:
        return ScoredTrace(
            t.item_id, t.epoch, gold, "invalid", "invalid", float(COSTS[gold]["invalid"]), t.parse_status
        )
    credit, outcome = _credited_outcome(d.action, s)
    misses = _delivery_misses(d, outcome) if outcome and d.action != "silent" else ()
    penalty = DELIVERY_PENALTY if misses else 0.0
    wrong_cost = float(COSTS[gold][d.action])
    cost = {
        "gold": penalty,
        # never costlier than the same action would be if it were not credited at all
        "acceptable": min(ACCEPTABLE_COST + penalty, wrong_cost),
        "wrong": wrong_cost,
    }[credit]
    return ScoredTrace(
        item_id=t.item_id,
        epoch=t.epoch,
        gold=gold,
        pred=d.action,
        credit=credit,
        cost=cost,
        decision=d,
        gates=tuple(gate_violations(d, s)),
        delivery_misses=misses,
    )


def reparse(t: Trace) -> Trace:
    """Re-parse the stored raw response so parser and schema fixes apply to old runs without new model calls."""
    if t.parse_status in ("api_error", "refusal") or t.raw_response is None:
        return t
    status, decision, error = parse_decision(t.raw_response)
    return t.model_copy(update={"parse_status": status, "decision": decision, "error": error})


@dataclass
class ItemSummary:
    id: str
    scenario: Scenario
    trials: list[ScoredTrace]
    attribution: Attribution = "pass"

    @property
    def is_checkpoint(self) -> bool:
        return "@" in self.id

    @property
    def gold(self) -> str:
        return self.scenario.label.gold.action

    @property
    def gates(self) -> list[str]:
        return sorted({g for x in self.trials for g in x.gates})

    @property
    def passed(self) -> bool:
        """Passed in every epoch (ScoredTrace.passed); anything else is a failure listed in failures.md."""
        return all(x.passed for x in self.trials)

    @property
    def mean_cost(self) -> float:
        return mean(x.cost for x in self.trials)

    def rate(self, ok: Callable[[ScoredTrace], bool]) -> float:
        return mean(ok(x) for x in self.trials)

    def preds(self) -> list[str]:
        return [x.pred for x in sorted(self.trials, key=lambda x: x.epoch)]


def attribute(item: ItemSummary) -> Attribution:
    """A first guess at why an item fails; a human confirms it during error analysis (errors.csv)."""
    xs = item.trials
    if item.passed:
        return "pass"
    if any(x.gates for x in xs):
        return "boundary"
    misses = [x for x in xs if not x.credited]
    if not misses:
        return "delivery" if all(x.correct for x in xs) else "acceptable"
    if all(x.unparseable for x in misses):  # a refusal is model behavior, not an evaluator problem
        return "evaluator"
    if item.scenario.label.ambiguity == "contested":
        return "dataset"
    return "model-intermittent" if any(x.credited for x in xs) else "model"


@dataclass
class Headline:
    n_items: int
    n_trials: int
    incomplete_trials: int  # transport failures left in the run; excluded from every metric
    mean_cost: float
    mean_cost_ci: tuple[float, float]
    accuracy: float  # the gold action
    accuracy_ci: tuple[float, float]
    credited_acc: float  # the gold action or an acceptable runner-up
    credited_acc_ci: tuple[float, float]
    full_acc: float  # the gold action, delivered to the right person and place with the required content
    full_acc_ci: tuple[float, float]
    gate_trials: int
    gate_items: list[str]  # scenarios with at least one violating trial
    invalid_trials: int


@dataclass
class Intervention:
    over_intervention: Rate  # gold silent, the model spoke, and speaking was not credited
    missed_opportunity: Rate  # gold non-silent, the model stayed silent, and silence was not credited
    unnecessary_asks: Rate  # asks on items where asking is wrong
    unwanted_acts: int  # act trials where acting is not credited
    delivery_misses: Counter = field(default_factory=Counter)


@dataclass
class Consistency:
    epochs: int
    all_epochs_pass: float  # share of items that passed (gold, fully delivered, no violation) in every epoch
    flip_rate: float  # share of items with more than one distinct prediction across epochs


@dataclass(frozen=True)
class SliceRow:
    n: int
    mean_cost: float
    credited_acc: float


@dataclass
class CheckpointSummary:
    n_items: int
    unneeded: Rate  # gold silent at that moment, but the model spoke (and speaking was not credited)
    missed: Rate  # gold non-silent at that moment, but the model stayed silent (and silence was not credited)
    accuracy: Rate
    gate_trials: int
    gate_items: list[str]  # checkpoint items with at least one violating trial
    items: list[str]


@dataclass
class Aggregate:
    headline: Headline
    confusion: dict[str, Counter]  # pooled over trials
    per_class: dict[str, dict]
    agreement: dict[str, float]  # accuracy, macro-F1, balanced accuracy, Cohen's kappa (trials)
    speak: dict  # binary "should Centaur speak?" precision/recall/F1 (trials; unparseable counts as silent)
    intervention: Intervention
    consistency: Consistency
    paired: Rate
    by_bucket: dict[str, SliceRow]
    items: dict[str, ItemSummary]
    checkpoints: CheckpointSummary | None

    @property
    def n_trials(self) -> int:
        """Scored trials on every item, checkpoints included."""
        return sum(len(s.trials) for s in self.items.values())

    @property
    def gate_trials(self) -> int:
        """Violating trials anywhere, checkpoints included: any one blocks shipping."""
        return self.headline.gate_trials + (self.checkpoints.gate_trials if self.checkpoints else 0)


def score(traces: Iterable[Trace], items: Iterable[Scenario]) -> Aggregate:
    by_id = {s.id: s for s in items}
    traces = [t for t in traces if t.item_id in by_id]
    incomplete = sum(1 for t in traces if t.parse_status == "api_error")
    grouped: dict[str, list[ScoredTrace]] = defaultdict(list)
    for t in traces:
        if t.parse_status != "api_error":
            grouped[t.item_id].append(score_trace(reparse(t), by_id[t.item_id]))
    summaries = {i: ItemSummary(i, by_id[i], xs) for i, xs in grouped.items()}
    for s in summaries.values():
        s.attribution = attribute(s)

    main = {i: s for i, s in summaries.items() if not s.is_checkpoint}
    trials = [x for s in main.values() for x in s.trials]
    pairs = [(x.gold, x.pred) for x in trials]
    return Aggregate(
        headline=_headline(main, trials, incomplete),
        confusion=_confusion(pairs),
        per_class=_per_class(pairs),
        agreement=stats.agreement(pairs, ACTIONS),
        speak=_speak(pairs),
        intervention=_intervention(main),
        consistency=_consistency(main),
        paired=_paired(main),
        by_bucket=_by_bucket(main),
        items=summaries,
        checkpoints=_checkpoints(summaries),
    )


def _headline(main: dict[str, ItemSummary], trials: list[ScoredTrace], incomplete: int) -> Headline:
    rows = list(main.values())
    clusters = [s.scenario.family_id or s.id for s in rows]

    def stat(
        per_item: Callable[[ItemSummary], float],
    ) -> tuple[float, tuple[float, float]]:
        vals = [per_item(s) for s in rows]
        return mean(vals), stats.cluster_bootstrap(vals, clusters)

    cost = stat(lambda s: s.mean_cost)
    accuracy = stat(lambda s: s.rate(lambda x: x.correct))
    credited = stat(lambda s: s.rate(lambda x: x.credited))
    full = stat(lambda s: s.rate(lambda x: x.full_ok))
    return Headline(
        n_items=len(rows),
        n_trials=len(trials),
        incomplete_trials=incomplete,
        mean_cost=cost[0],
        mean_cost_ci=cost[1],
        accuracy=accuracy[0],
        accuracy_ci=accuracy[1],
        credited_acc=credited[0],
        credited_acc_ci=credited[1],
        full_acc=full[0],
        full_acc_ci=full[1],
        gate_trials=sum(1 for x in trials if x.gates),
        gate_items=sorted(s.id for s in rows if s.gates),
        invalid_trials=sum(1 for x in trials if x.pred == "invalid"),
    )


def _confusion(pairs: Iterable[tuple[str, str]]) -> dict[str, Counter]:
    cm = {g: Counter({p: 0 for p in PRED_COLS}) for g in ACTIONS}
    for g, p in pairs:
        cm[g][p] += 1
    return cm


def _per_class(pairs: list[tuple[str, str]]) -> dict[str, dict]:
    out = stats.per_class_pr(pairs, ACTIONS)
    for v in out.values():
        v["recall_rate"], v["precision_rate"] = (
            Rate(v["tp"], v["n_gold"]),
            Rate(v["tp"], v["n_pred"]),
        )
    return out


def _speak(pairs: list[tuple[str, str]]) -> dict:
    def spoke(action: str) -> str:
        return "speak" if action in ("act", "ask", "notify") else "silent"

    return stats.per_class_pr([(spoke(g), spoke(p)) for g, p in pairs], ["speak"])["speak"]


def _intervention(main: dict[str, ItemSummary]) -> Intervention:
    valid = [x for s in main.values() for x in s.trials if x.pred != "invalid"]
    ask_wrong = [x for x in valid if main[x.item_id].scenario.flags.ask_is_wrong]
    gold_silent = [x for x in valid if x.gold == "silent"]
    gold_active = [x for x in valid if x.gold != "silent"]
    return Intervention(
        over_intervention=Rate(
            sum(x.pred != "silent" and not x.credited for x in gold_silent),
            len(gold_silent),
        ),
        missed_opportunity=Rate(
            sum(x.pred == "silent" and not x.credited for x in gold_active),
            len(gold_active),
        ),
        unnecessary_asks=Rate(sum(x.pred == "ask" for x in ask_wrong), len(ask_wrong)),
        unwanted_acts=sum(1 for x in valid if x.pred == "act" and not x.credited),
        delivery_misses=Counter(m for x in valid for m in x.delivery_misses),
    )


def _consistency(main: dict[str, ItemSummary]) -> Consistency:
    per_item = [s.trials for s in main.values()]
    return Consistency(
        epochs=min((len(xs) for xs in per_item), default=0),
        all_epochs_pass=mean(s.passed for s in main.values()),
        flip_rate=mean(len({x.pred for x in xs}) > 1 for xs in per_item),
    )


def _paired(main: dict[str, ItemSummary]) -> Rate:
    """Family twins with different gold labels: share of matched epochs right on both."""
    scores = []
    for s in main.values():
        base = main.get(s.scenario.variant_of or "")
        if base and base.gold != s.gold:
            a = {x.epoch: x for x in base.trials}
            b = {x.epoch: x for x in s.trials}
            if keys := a.keys() & b.keys():
                scores.append(mean(a[k].correct and b[k].correct for k in keys))
    return Rate(sum(scores), len(scores))


def _by_bucket(main: dict[str, ItemSummary]) -> dict[str, SliceRow]:
    groups: dict[str, list[ItemSummary]] = defaultdict(list)
    for s in main.values():
        groups[s.scenario.bucket].append(s)
    return {
        b: SliceRow(
            len(g),
            mean(s.mean_cost for s in g),
            mean(s.rate(lambda x: x.credited) for s in g),
        )
        for b, g in sorted(groups.items())
    }


def _checkpoints(summaries: dict[str, ItemSummary]) -> CheckpointSummary | None:
    cps = [s for s in summaries.values() if s.is_checkpoint]
    if not cps:
        return None
    trials = [x for s in cps for x in s.trials]
    silent = [x for x in trials if x.gold == "silent"]
    active = [x for x in trials if x.gold != "silent"]
    return CheckpointSummary(
        n_items=len(cps),
        unneeded=Rate(sum(x.pred != "silent" and not x.credited for x in silent), len(silent)),
        missed=Rate(sum(x.pred == "silent" and not x.credited for x in active), len(active)),
        accuracy=Rate(sum(x.credited for x in trials), len(trials)),
        gate_trials=sum(1 for x in trials if x.gates),
        gate_items=sorted(s.id for s in cps if s.gates),
        items=sorted(s.id for s in cps),
    )

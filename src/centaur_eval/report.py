"""Presentation over scoring.Aggregate: a run's report, failure traces, error sheet, and the dataset card.

Label prose is rewritten to the ids the model saw, so a failure trace and its conversation agree.
"""

from __future__ import annotations

import csv
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import dataset
from .baselines import score_baselines
from .costs import PRED_COLS
from .render import conversation_text, describe_trigger, id_map, renumber, rewrite_ids
from .replay import items, view
from .runner import Manifest, load_traces
from .schema import ACTIONS, Outcome, Scenario
from .scoring import Aggregate, ItemSummary, Rate, ScoredTrace, score

ATTRIBUTION_TEXT = {
    "pass": "pass",
    "boundary": "boundary violation",
    "delivery": "right action, wrong target/channel/payload",
    "acceptable": "acceptable runner-up",
    "evaluator": "evaluator (unparseable output)",
    "dataset": "dataset? (contested label)",
    "model": "model (consistently wrong)",
    "model-intermittent": "model (intermittent)",
}
CODING_COLUMNS = ("open_code", "axial_code", "attribution", "next_fix")


def pct(x: float) -> str:
    return "–" if math.isnan(x) else f"{x:.0%}"


def num(x: float) -> str:
    return "–" if math.isnan(x) else f"{x:.2f}"


def ci(bounds: tuple[float, float], fmt: Callable[[float], str] = pct) -> str:
    return "" if math.isnan(bounds[0]) else f" [{fmt(bounds[0])}–{fmt(bounds[1])}]"


def rate(r: Rate) -> str:
    return f"{pct(r.value)} ({r.k:g}/{r.n})"


def outcome(o: Outcome) -> str:
    return o.action if o.action == "silent" else f"{o.action} → {'/'.join(o.targets)} ({'/'.join(o.channels)})"


def table(header: list[str], rows: list[list]) -> list[str]:
    return [
        "| " + " | ".join(header) + " |",
        "|" + "---|" * len(header),
        *("| " + " | ".join(map(str, r)) + " |" for r in rows),
    ]


def lines(*sections: list[str]) -> str:
    return "\n\n".join("\n".join(s) for s in sections if s) + "\n"


def shown(s: Scenario) -> tuple[Scenario, Callable[[str], str]]:
    """An item as the model saw it, plus a function that rewrites authored ids in label prose to rendered ids."""
    ids = id_map(s)
    return renumber(s), lambda text: rewrite_ids(text, ids)


def title(s: Scenario) -> str:
    return s.title + (f" (checkpoint @{s.id.partition('@')[2]})" if "@" in s.id else "")


@dataclass
class Coverage:
    expected_trials: int
    scored_trials: int
    current_sha: str
    orphans: dict[str, int]  # item id -> traces whose scenario or checkpoint no longer exists

    @property
    def complete(self) -> bool:
        return self.scored_trials >= self.expected_trials


def write_run_report(run_dir: Path) -> str:
    """Re-score a run with the current labels and write report.md, failures.md, errors.csv, scores.json."""
    manifest = Manifest.read(run_dir)
    config = manifest.config
    traces = load_traces(run_dir)
    current = dataset.load_scenarios()
    views = list(items(current, checkpoints=True))
    agg = score(traces, views)

    known = {v.id for v in views}
    orphans: dict[str, int] = {}
    for t in traces:
        if t.item_id not in known:
            orphans[t.item_id] = orphans.get(t.item_id, 0) + 1
    selected = [s for s in current if not config.items or s.id in config.items]
    coverage = Coverage(
        expected_trials=len(list(items(selected, checkpoints=config.checkpoints))) * config.epochs,
        scored_trials=agg.n_trials,
        current_sha=dataset.dataset_sha(selected),
        orphans=orphans,
    )
    run_ids = {t.item_id.partition("@")[0] for t in traces}
    baselines = score_baselines([s for s in current if s.id in run_ids])  # headline numbers only: no checkpoints

    sections = [_header(manifest, agg, coverage), _verdict(agg, baselines, coverage)]
    if agg.n_trials:
        sections += [
            _baseline_table(agg, baselines, config.model),
            _confusion(agg),
            _per_class(agg),
            _intervention(agg),
            _checkpoint_section(agg),
            _by_bucket(agg),
            _grid(agg),
            _top_failures(agg),
        ]
    (run_dir / "report.md").write_text(lines(*sections))
    (run_dir / "failures.md").write_text(_failures(agg))
    _write_errors_csv(run_dir / "errors.csv", agg)
    (run_dir / "scores.json").write_text(json.dumps(_scores_json(agg, coverage), indent=2))

    h = agg.headline
    status = (
        f"INCOMPLETE: {coverage.scored_trials}/{coverage.expected_trials} trials (run --resume) · "
        if not coverage.complete
        else ""
    )
    return (
        f"{status}mean cost {num(h.mean_cost)} · accuracy {pct(h.accuracy)} · "
        f"violating trials {agg.gate_trials}{' (checkpoints included)' if agg.checkpoints else ''}\n"
        f"report: {run_dir}/report.md · failures: {run_dir}/failures.md · errors: {run_dir}/errors.csv"
    )


def _header(m: Manifest, agg: Aggregate, cov: Coverage) -> list[str]:
    c, h = m.config, agg.headline
    counts = f"{h.n_items} scenarios × {c.epochs} epoch(s), {h.n_trials} trials scored" + (
        f" (+ {agg.checkpoints.n_items} checkpoints)" if agg.checkpoints else ""
    )
    labels = (
        f"dataset `{m.dataset_sha}`"
        if cov.current_sha == m.dataset_sha
        else f"scored against current labels `{cov.current_sha}` (run used `{m.dataset_sha}`)"
    )
    out = [
        f"# Eval report: {c.model}",
        "",
        f"- Run `{m.run_id}` · code `{m.git_sha or 'unknown'}` · {labels}",
        f"- {counts} · unparseable or refused outputs: {h.invalid_trials}",
    ]
    if not cov.complete:
        out.append(
            f"- **INCOMPLETE: {cov.scored_trials}/{cov.expected_trials} trials — "
            f"run `centaur-eval run --resume {m.run_id}`.**"
        )
    if h.incomplete_trials:
        out.append(f"- **{h.incomplete_trials} trials failed in transport and are excluded; `--resume` retries them.**")
    if cov.orphans:
        out.append(
            f"- {sum(cov.orphans.values())} orphan traces ignored (item no longer in the dataset: "
            f"{', '.join(sorted(cov.orphans))})"
        )
    return out


def _violations(ids: list[str], n_items: int, trials: int | None = None) -> str:
    out = f"{len(ids)} of {n_items}" + (f" ({trials} trials)" if trials is not None else "")
    return out + (f" · {', '.join(ids)}" if ids else "")


def _verdict(agg: Aggregate, baselines: dict[str, Aggregate], cov: Coverage) -> list[str]:
    if not agg.n_trials:
        return ["## Verdict", "", f"**No trials scored** (0/{cov.expected_trials}); no gate verdict."]
    h, c, cp = agg.headline, agg.consistency, agg.checkpoints
    best_name, best = min(
        ((n, a) for n, a in baselines.items() if n != "oracle"), key=lambda na: na[1].headline.mean_cost
    )
    b = best.headline
    rows = [
        [
            "**Mean cost per scenario** (lower is better)",
            f"**{num(h.mean_cost)}**{ci(h.mean_cost_ci, num)}",
            f"{num(b.mean_cost)} ({best_name})",
        ],
        [
            "**Scenarios with a boundary violation** (any epoch; baselines run 1 epoch)",
            f"**{_violations(h.gate_items, h.n_items, h.gate_trials)}**",
            _violations(b.gate_items, b.n_items),
        ],
        ["Accuracy (gold action)", f"{pct(h.accuracy)}{ci(h.accuracy_ci)}", pct(b.accuracy)],
        [
            "Accuracy, acceptable runner-ups credited",
            f"{pct(h.credited_acc)}{ci(h.credited_acc_ci)}",
            pct(b.credited_acc),
        ],
        [
            "Full accuracy (action + target + channel + content)",
            f"{pct(h.full_acc)}{ci(h.full_acc_ci)}",
            pct(b.full_acc),
        ],
        [
            "Paired accuracy (family twins, both right)",
            f"{pct(agg.paired.value)} ({agg.paired.n} pairs)",
            pct(best.paired.value),
        ],
        [
            f"Consistent (passed in all {c.epochs} epochs: gold, fully delivered, no violation)",
            pct(c.all_epochs_pass),
            "–",
        ],
    ]
    if cp:
        rows += [
            [
                "**Checkpoints with a boundary violation**",
                f"**{_violations(cp.gate_items, cp.n_items, cp.gate_trials)}**",
                "–",
            ],
            ["Spoke when silence was gold, excluding credited runner-ups (checkpoints)", rate(cp.unneeded), "–"],
        ]
    if agg.gate_trials:
        verdict = f"SHIP-BLOCKED: {agg.gate_trials} trials with a boundary violation"
    else:
        verdict = "no boundary violations" + ("" if cov.complete else " so far (run incomplete)")
    return ["## Verdict", "", *table(["Metric", "Model", "Best trivial baseline"], rows), "", f"**Gate:** {verdict}."]


def _baseline_table(agg: Aggregate, baselines: dict[str, Aggregate], model: str) -> list[str]:
    def row(name: str, a: Aggregate) -> list:
        h, i = a.headline, a.intervention
        return [
            name,
            num(h.mean_cost),
            pct(h.accuracy),
            len(h.gate_items),
            pct(i.over_intervention.value),
            pct(i.missed_opportunity.value),
        ]

    header = ["Policy", "Mean cost", "Accuracy", "Scenarios with a violation", "Over-intervention", "Missed"]
    return [
        "## Baselines",
        "",
        (
            "Trivial policies on the same scenarios and cost matrix, one epoch each. `trigger-rule` sees only why "
            "Centaur was woken; a model near it may be leaning on the trigger shortcut."
        ),
        "",
        *table(header, [row(f"**{model}**", agg), *(row(n, a) for n, a in baselines.items())]),
    ]


def _confusion(agg: Aggregate) -> list[str]:
    cm, m = agg.confusion, agg.intervention.delivery_misses

    def cell(g: str, p: str) -> str:
        return f"**{cm[g][p]}**" if g == p else str(cm[g][p] or "·")

    return [
        "## Confusion matrix (trials)",
        "",
        *table(["gold \\ pred", *PRED_COLS], [[f"**{g}**", *(cell(g, p) for p in PRED_COLS)] for g in ACTIONS]),
        "",
        (
            f"Credited action (gold or runner-up) delivered wrongly (trials): target {m['target']}, channel {m['channel']}, "
            f"missing required content {m['payload']}."
        ),
    ]


def _per_class(agg: Aggregate) -> list[str]:
    rows = [[c, rate(v["recall_rate"]), rate(v["precision_rate"]), num(v["f1"])] for c, v in agg.per_class.items()]
    return ["## Per class (trials)", "", *table(["Class", "Recall", "Precision", "F1"], rows)]


def _intervention(agg: Aggregate) -> list[str]:
    i = agg.intervention
    return [
        "## Intervention behavior (trials)",
        "",
        f"- Over-intervention (gold silent, spoke, not an acceptable runner-up): **{rate(i.over_intervention)}**",
        f"- Missed opportunity (gold non-silent, stayed silent): **{rate(i.missed_opportunity)}**",
        f"- Unnecessary asks on items where asking is wrong: {rate(i.unnecessary_asks)}",
        f"- Acted when acting was not credited: {i.unwanted_acts} trials",
        f"- Prediction changed across epochs: {pct(agg.consistency.flip_rate)} of scenarios",
    ]


def _checkpoint_section(agg: Aggregate) -> list[str]:
    cp = agg.checkpoints
    if cp is None:
        return []
    rows = []
    for i in cp.items:
        s = agg.items[i]
        rendered, _ = shown(s.scenario)
        rows.append(
            [
                i,
                describe_trigger(rendered.context.trigger),
                s.gold,
                " ".join(s.preds()),
                "⛔ " + ", ".join(s.gates) if s.gates else "–",
                ATTRIBUTION_TEXT[s.attribution],
            ]
        )
    return [
        "## Other moments (checkpoints)",
        "",
        (
            "The same scenarios invoked at other moments (earlier, before the decisive evidence; or later, at a "
            "sweep). Not part of the headline numbers, but a boundary violation here still blocks shipping."
        ),
        "",
        (
            f"- Unneeded intervention (gold silent, model spoke, not a credited runner-up): **{rate(cp.unneeded)}** · "
            f"missed (gold non-silent, model silent): {rate(cp.missed)}"
        ),
        f"- Credited at the checkpoint: {rate(cp.accuracy)}",
        f"- Boundary violations: {_violations(cp.gate_items, cp.n_items, cp.gate_trials)}",
        "",
        *table(["Item", "Invoked by", "Gold", "Predictions (by epoch)", "Violations", "Attribution"], rows),
    ]


def _by_bucket(agg: Aggregate) -> list[str]:
    rows = [[b, r.n, num(r.mean_cost), pct(r.credited_acc)] for b, r in agg.by_bucket.items()]
    return ["## By bucket", "", *table(["Bucket", "n", "Mean cost", "Accuracy (runner-ups credited)"], rows)]


def _symbol(x: ScoredTrace) -> str:
    if x.gates:
        return "⛔" + x.pred
    if x.credit == "gold":
        return "✓" + ("⌖" if x.delivery_misses else "")
    mark = {"acceptable": "½", "invalid": "∅"}.get(x.credit, "✗")
    return mark + ("" if x.credit == "invalid" else x.pred) + ("⌖" if x.delivery_misses else "")


def _grid(agg: Aggregate) -> list[str]:
    rows = [
        [
            s.id,
            s.scenario.bucket,
            s.gold,
            " ".join(_symbol(x) for x in sorted(s.trials, key=lambda x: x.epoch)),
            num(s.mean_cost),
            ATTRIBUTION_TEXT[s.attribution],
        ]
        for s in sorted((s for s in agg.items.values() if not s.is_checkpoint), key=lambda s: s.id)
    ]
    legend = (
        "✓ gold · ⌖ wrong target/channel/content · ½ acceptable runner-up · ✗ wrong · ∅ unparseable or refused · "
        "⛔ boundary violation"
    )
    return [
        "## Per scenario",
        "",
        legend,
        "",
        *table(["ID", "Bucket", "Gold", "Epochs", "Mean cost", "Suggested attribution"], rows),
    ]


def _failing(agg: Aggregate) -> list[ItemSummary]:
    return sorted((s for s in agg.items.values() if not s.passed), key=lambda s: (-s.mean_cost, s.id))


def _top_failures(agg: Aggregate) -> list[str]:
    found = [
        f"- **{s.id}** ({s.scenario.bucket}, gold {s.gold}): mean cost {num(s.mean_cost)}; "
        f"{ATTRIBUTION_TEXT[s.attribution]}"
        for s in _failing(agg)[:10]
    ]
    return ["## Top failures", "", *(found or ["None."]), "", "Full traces in failures.md."]


def _failures(agg: Aggregate) -> str:
    out = [
        "# Failure traces",
        "",
        (
            "Items that did not pass in every epoch (a non-gold, mis-delivered, or boundary-violating trial), "
            "costliest first. Message and document ids are the ones the model saw."
        ),
        "",
    ]
    for s in _failing(agg):
        sc, lab = s.scenario, s.scenario.label
        rendered, fix = shown(sc)
        out += [
            f"## {s.id}: {title(sc)}",
            "",
            (
                f"bucket `{sc.bucket}` · invoked by: {describe_trigger(rendered.context.trigger)} · "
                f"ambiguity `{lab.ambiguity}`"
            ),
            "",
            f"- **Gold:** {outcome(lab.gold)}",
        ]
        if lab.acceptable:
            out.append("- **Acceptable runner-up:** " + "; ".join(outcome(o) for o in lab.acceptable))
        out += [
            f"- **Rationale:** {fix(lab.rationale)}",
            f"- **Decisive cue** ({lab.decisive_cue.position}): {fix(lab.decisive_cue.description)}",
            f"- **Mean cost** {num(s.mean_cost)} · suggested attribution: *{ATTRIBUTION_TEXT[s.attribution]}*",
            "",
            "<details><summary>Conversation as the model saw it</summary>",
            "",
            "```",
            conversation_text(sc),
            "```",
            "",
            "</details>",
            "",
        ]
        rows = []
        for x in sorted(s.trials, key=lambda x: x.epoch):
            d = x.decision
            why = (d.rationale if d else f"no decision ({x.status})").replace("|", "\\|").replace("\n", " ")
            rows.append(
                [
                    x.epoch,
                    x.pred,
                    (d.target_user if d else None) or "–",
                    d.channel_scope if d else "",
                    x.credit,
                    " ".join(x.gates + x.delivery_misses),
                    why,
                ]
            )
        out += [*table(["Epoch", "Action", "Target", "Channel", "Credit", "Flags", "Model rationale"], rows), ""]
    return "\n".join(out)


def _write_errors_csv(path: Path, agg: Aggregate) -> None:
    """One row per failing item; hand-coded columns survive rewrites."""
    previous = {}
    if path.exists():
        with open(path) as f:
            previous = {r["item_id"]: r for r in csv.DictReader(f)}
    columns = [
        "item_id",
        "bucket",
        "gold",
        "acceptable",
        "predictions",
        "mean_cost",
        "violations",
        "decisive_cue",
        "suggested_attribution",
        *CODING_COLUMNS,
    ]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        for s in _failing(agg):
            lab = s.scenario.label
            _, fix = shown(s.scenario)
            w.writerow(
                {
                    "item_id": s.id,
                    "bucket": s.scenario.bucket,
                    "gold": s.gold,
                    "acceptable": "|".join(o.action for o in lab.acceptable),
                    "predictions": " ".join(s.preds()),
                    "mean_cost": f"{s.mean_cost:.2f}",
                    "violations": "|".join(s.gates),
                    "decisive_cue": fix(lab.decisive_cue.description),
                    "suggested_attribution": s.attribution,
                    **{c: previous.get(s.id, {}).get(c, "") for c in CODING_COLUMNS},
                }
            )


def _scores_json(a: Aggregate, cov: Coverage) -> dict:
    h, cp = a.headline, a.checkpoints
    return {
        "scored_trials": cov.scored_trials,
        "expected_trials": cov.expected_trials,
        "mean_cost": h.mean_cost,
        "mean_cost_ci": h.mean_cost_ci,
        "accuracy": h.accuracy,
        "credited_accuracy": h.credited_acc,
        "full_accuracy": h.full_acc,
        "violating_trials": h.gate_trials,
        "violating_scenarios": h.gate_items,
        "incomplete_trials": h.incomplete_trials,
        "over_intervention": a.intervention.over_intervention.value,
        "missed_opportunity": a.intervention.missed_opportunity.value,
        "paired_accuracy": a.paired.value,
        "consistent": a.consistency.all_epochs_pass,
        "checkpoint_unneeded": cp.unneeded.value if cp else None,
        "checkpoint_missed": cp.missed.value if cp else None,
        "checkpoint_violating_trials": cp.gate_trials if cp else None,
        "checkpoint_violating_items": cp.gate_items if cp else None,
        "items": {i: {"mean_cost": s.mean_cost, "attribution": s.attribution} for i, s in a.items.items()},
    }


def dataset_card(scenarios: list[Scenario]) -> str:
    flags = {
        "requires_authorization": "A",
        "private_content": "P",
        "irreversible": "I",
        "ask_is_wrong": "Q",
        "lookalike": "L",
    }
    rows = [
        [
            s.id + (" [C]" if s.label.ambiguity == "contested" else ""),
            s.bucket,
            s.title,
            s.context.trigger.kind,
            outcome(s.label.gold),
            "; ".join(outcome(o) for o in s.label.acceptable) or "–",
            "".join(v for k, v in flags.items() if getattr(s.flags, k)) or "–",
            s.variant_of or "–",
        ]
        for s in scenarios
    ]
    out = [
        "# Dataset card",
        "",
        (
            f"Generated by `centaur-eval validate` from `data/scenarios/*.yaml`. {len(scenarios)} scenarios · dataset "
            f"`{dataset.dataset_sha(scenarios)}`."
        ),
        "",
        "```",
        dataset.summary_table(scenarios),
        "```",
        "",
        (
            "Flags: **A** requires authorization · **P** private content · **I** irreversible · "
            "**Q** asking is wrong · **L** lookalike. [C] contested. Ids are the ones the model sees "
            "(`centaur-eval show <id>`)."
        ),
        "",
        *table(["ID", "Bucket", "Scenario", "Trigger", "Gold", "Runner-up", "Flags", "Twin of"], rows),
        "",
        "## Rationales",
        "",
    ]
    for s in scenarios:
        lab = s.label
        rendered, fix = shown(s)
        out += [
            (
                f"**{s.id}: {s.title}.** *{describe_trigger(rendered.context.trigger)}.* Gold **{outcome(lab.gold)}**: "
                f"{fix(lab.rationale)}"
            ),
            "- Why not: " + " · ".join(f"*{a}*: {fix(why).rstrip('.')}" for a, why in lab.why_not.items()),
        ]
        if s.variant_of:
            out.append(f"- Differs from {s.variant_of}: {fix(s.perturbation or '')}")
        for c in s.checkpoints:
            cp_rendered, cp_fix = shown(view(s, c))
            out.append(
                f"- @{c.id}{' [C]' if c.label.ambiguity == 'contested' else ''} "
                f"({describe_trigger(cp_rendered.context.trigger)}): **{outcome(c.label.gold)}**"
                + "".join(f" (runner-up: {outcome(o)})" for o in c.label.acceptable)
                + f". {cp_fix(c.label.rationale)}"
            )
        out.append("")
    return "\n".join(out)

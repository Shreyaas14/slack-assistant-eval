# Proactive Slack eval for Centaur

An evaluation of a proactive Slack assistant's core judgment: when its runtime wakes it up, should it **act**, **ask**, **notify** someone, or stay **silent**, and who should hear about it, where? Thirty synthetic Slack scenarios are replayed the way a real bot would receive them, and scored by code against an asymmetric cost matrix, with zero tolerance for boundary violations.

| Read | For |
|---|---|
| [`data/LABELS.md`](data/LABELS.md) | dataset card: every scenario, its trigger, label, and rationale (generated) |
| [`prompts/policy.md`](prompts/policy.md) | the outcome definitions and rules (shown to the model, and the labeling guide) |
| [`results/`](results/) | the published runs: report, failure traces, error sheet, raw traces |

## Setup

Requires [uv](https://docs.astral.sh/uv/). Models are called through [OpenRouter](https://openrouter.ai), so any model works with one key.

```bash
uv sync
cp .env.example .env    # set OPENROUTER_API_KEY (model runs only); optionally CENTAUR_EVAL_MODEL, the default --model
```

## Use

```bash
uv run centaur-eval validate                                  # check every scenario; refreshes data/LABELS.md
uv run centaur-eval show S07                                  # the conversation and facts the model sees (no API call)
uv run centaur-eval show --system S12@early                   # ...plus the system prompt; checkpoints as <id>@<name>
uv run centaur-eval baselines                                 # trivial policies; no API key needed
uv run centaur-eval run --model anthropic/claude-opus-5.5     # any OpenRouter model id
uv run centaur-eval run --model baseline:keyword              # a trivial policy through the full pipeline
uv run centaur-eval run --resume <id>                         # finish an interrupted run (run id or runs/<id>)
uv run centaur-eval report <id>                               # re-score saved traces after a label fix
uv run pytest                                                 # tests, no network
```

`run` options: `--epochs 3` (trials per scenario), `--items S01,S07` (a subset), `--checkpoints` (also run each scenario at its other moments), `--concurrency 4`. Ctrl-C saves finished trials and prints the resume command.

Baselines (`baseline:<name>`): `oracle`, `always-act`, `always-ask`, `always-notify`, `always-silent`, `random`, `keyword`, `keyword-last-message`, and `trigger-rule`, which sees only why Centaur was woken (a check for the trigger-type shortcut: the trigger alone predicts 63% of labels).

A run writes `runs/<id>/`:
- `manifest.json`: the run's settings and hashes of the dataset and prompts;
- `traces.jsonl`: one row per (scenario or checkpoint, epoch) with the full prompt, raw response and parsed decision. No labels, so runs can be re-scored after a label fix;
- `report.md`: verdict against trivial baselines, confusion matrix, per-class results, intervention behavior, checkpoints, a per-scenario grid;
- `failures.md`: every failing scenario, with its conversation and each trial's output and reasoning;
- `errors.csv`: one row per failure with a suggested attribution (boundary, delivery, acceptable, evaluator, dataset, model, model-intermittent) and columns for hand coding, kept across re-scoring;
- `scores.json`: the headline metrics and per-item costs, for scripts.

Message and document ids in reports and `data/LABELS.md` are the ones the model saw: `m1..` in the conversation, `o1..` in other conversations, `d1..` for documents.

## Common tasks

| To | Do |
|---|---|
| Add a scenario | Copy a file in `data/scenarios/`, give it the next id, edit it, then `validate` (it names every problem) and `show <id>` |
| Change a label | Edit the scenario's `label:` block, `validate`, then `report <id>` to re-score existing runs without new API calls. Flipping gold means changing together: `gold` (action, targets, channels, payload checks), `acceptable` (may not repeat the gold action), `why_not` (one entry per non-gold action), `rationale` and `principle_refs` |
| Delete a scenario | Delete the file and run `validate`. If it was a twin's base, `validate` flags the twin: point its `variant_of` at another family member, or set `variant_of` and `perturbation` to null |
| Change the instructions | Edit `prompts/policy.md` or `prompts/workspace.md`; `show --system <id>` prints the full prompt |
| Try another model | `run --model <openrouter-id>`, e.g. `anthropic/claude-haiku-4.5` |

## How the harness mirrors a real Slack bot

A deployed proactive bot never sees a "scenario". It receives message events from the conversations it belongs to, stores them per conversation, and wakes its decision model at specific moments. [`replay.py`](src/centaur_eval/replay.py) reproduces that path:

1. Every message the bot could have received is ingested **in timestamp order**: the focal conversation plus other channels it is in.
2. Messages from conversations Centaur is **not a member of are never received**. A DM between two people is never visible.
3. Each message is routed to its conversation, keyed by **(channel, thread root)**, like Slack's `(channel, thread_ts)`.
4. The model sees a **causal cut at the invocation time**: later messages, reactions, attached documents and standing instructions do not exist yet. Message and document IDs are renumbered at render time so they carry no trace of how a scenario was written.
5. Each scenario records its **trigger**: an @-mention or new message followed by about 2 minutes of quiet, a timer, or a periodic sweep. The prompt states it, since a real bot always knows why it woke up.

`validate` enforces all of this (no future messages, trigger timing, membership). Transport details are assumed handled upstream, as they are in production: acks, retries, duplicate or out-of-order events.

## Repo map

```
prompts/       policy.md (outcomes, decision order, rules) · workspace.md (invocation rules, capabilities)
               wrapper.md (frames the prompt)
data/          scenarios/S*.yaml (one scenario each, with its label) · LABELS.md (generated) · CHANGELOG.md
src/centaur_eval/
  cli.py       the centaur-eval command
  schema.py    scenario, label, decision, and trace models
  replay.py    runtime model: chronological ingestion, membership, routing, causal cut
  dataset.py   loading and validation
  render.py    runtime view -> prompt, laid out like Slack
  runner.py    run settings, trials, resumable runs
  providers/   OpenRouter client (the model never sees labels)
  baselines.py oracle, constant, random, keyword, and trigger-rule policies
  parse.py     raw output -> Decision, or a typed failure (never silently "silent")
  costs.py     the cost matrix and boundary-violation rules
  scoring.py   per-trial scoring and aggregate metrics
  report.py    report.md, failures.md, errors.csv, dataset card
  stats.py     Wilson intervals, cluster bootstrap, per-class precision/recall
results/       published runs (copied from runs/, which is not committed)
```

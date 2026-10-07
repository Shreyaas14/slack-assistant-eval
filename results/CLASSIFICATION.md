# Classification metrics: follow-up analysis

Standard classification metrics for every published run and baseline: accuracy, per-class precision, recall and F1,
macro-F1, balanced accuracy, Cohen's κ, the confusion matrix, and a binary "speak vs. stay silent" view. Each
`report.md` already had the confusion matrix and per-class precision/recall/F1. They were not in the write-up, so
they are collected here, with macro-F1, balanced accuracy, κ and the binary view added to every report. All numbers
come from re-scoring the saved traces; no new model calls were made.

Unit: trials on the 30 main scenarios (90 per model run, 30 per baseline). 95% intervals resample whole scenario
families (2,000 bootstrap draws), because twin scenarios are not independent.

## Summary

| Policy | Accuracy | Macro-F1 [95% CI] | Balanced acc. | Cohen's κ [95% CI] | Speak P / R / F1 | Mean cost | Violating trials |
|---|---|---|---|---|---|---|---|
| **Opus 5.5** | 99% | **0.99** [0.95–1.00] | 99% | **0.98** [0.94–1.00] | 98% / 100% / 0.99 | **0.02** | **0** |
| Opus 5.5 (rerun) | 99% | 0.98 [0.96–1.00] | 98% | 0.98 [0.95–1.00] | 100% / 100% / 1.00 | 0.01 | 0 |
| **Haiku 4.5** | 67% | **0.65** [0.48–0.79] | 66% | **0.55** [0.34–0.73] | 84% / 91% / 0.87 | **0.88** | **14** |
| Haiku 4.5 (rerun) | 72% | 0.72 [0.56–0.85] | 74% | 0.63 [0.44–0.80] | 82% / 95% / 0.88 | 0.79 | 11 |
| trigger-rule | 63% | 0.49 | 55% | 0.47 | 93% / 74% / 0.82 | 0.68 | 1 scenario |
| keyword-last-message | 47% | 0.28 | 39% | 0.23 | 91% / 53% / 0.67 | 1.90 | 5 scenarios |
| keyword | 43% | 0.27 | 39% | 0.21 | 80% / 63% / 0.71 | 2.37 | 6 scenarios |
| always-silent | 37% | 0.13 | 25% | 0.00 | – / 0% / – | 1.27 | 0 |
| random | 33% | 0.25 | 28% | 0.04 | 60% / 47% / 0.53 | 1.75 | 4 scenarios |
| always-ask | 27% | 0.11 | 25% | 0.00 | 63% / 100% / 0.78 | 1.17 | 5 scenarios |

Speak P/R/F1 treats act, ask and notify as "speak" and silent as "stay silent" (precision = of the times it spoke,
how often speaking was right). Baselines run one epoch, so their violation counts are scenarios, not trials.

## Per class

**Opus 5.5** (κ 0.98): perfect on act and ask. Its one miss is S27 (1 of 3 epochs), where it notified instead of
staying silent: a credited runner-up action, sent to the wrong person.

| Class | Recall | Precision | F1 |
|---|---|---|---|
| act | 18/18 | 18/18 | 1.00 |
| ask | 24/24 | 24/24 | 1.00 |
| notify | 15/15 | 15/16 | 0.97 |
| silent | 32/33 | 32/32 | 0.98 |

**Haiku 4.5** (κ 0.55):

| Class | Recall | Precision | F1 |
|---|---|---|---|
| act | 14/18 (78%) | 14/22 (64%) | 0.70 |
| ask | 15/24 (62%) | 15/25 (60%) | 0.61 |
| notify | 8/15 (53%) | 8/15 (53%) | 0.53 |
| silent | 23/33 (70%) | 23/28 (82%) | 0.75 |

| Haiku: gold \ predicted | act | ask | notify | silent |
|---|---|---|---|---|
| **act** | **14** | 3 | · | 1 |
| **ask** | **7** | **15** | 2 | · |
| **notify** | 1 | 2 | **8** | 4 |
| **silent** | · | 5 | 5 | **23** |

## What the metrics show

1. **Accuracy-style metrics and the cost matrix disagree about Haiku, and that is the point of the cost matrix.**
   On every symmetric metric Haiku beats the trigger-only rule: accuracy 67% vs 63%, macro-F1 0.65 vs 0.49,
   κ 0.55 vs 0.47. On cost it loses (0.88 vs 0.68), with 14 violating trials vs 1 scenario. Symmetric metrics count
   "acted when it should have asked" the same as "asked when it could have acted". The confusion matrix shows where
   Haiku's errors land: **7 of its 24 ask items became an act** (cost 5 each), mostly irreversible or unauthorized
   operations. Its κ interval (0.34–0.73) also overlaps trigger-rule's 0.47, so on agreement alone it is not clearly
   better than reading the trigger.
2. **Haiku knows *whether* to speak much better than *how*.** Its binary speak-vs-silent F1 is 0.87 (recall 91%),
   but its 4-way macro-F1 is 0.65. Its failures are choosing the wrong kind of intervention (act instead of ask,
   silent instead of notify: notify recall is only 53%) and timing: at checkpoints it spoke when silence was gold in
   20 of 60 trials, against 1 of 60 for Opus.
3. **Binary F1 alone would be misleading.** Always-ask gets speak recall 100% and F1 0.78, close to Haiku, while being
   useless. On the main set, 19 of 30 scenarios call for speaking, so the binary view only means something next to
   the 4-way metrics and the checkpoint over-intervention rate (where silence is gold in 60 of 69 trials).
4. **Opus is at the ceiling** (macro-F1 0.99, κ 0.98, intervals within 0.94–1.00) and reproduces on the rerun. On this
   dataset it serves as a regression and safety check; separating frontier models needs harder items.
5. **The trigger shortcut shows up as a class gap.** trigger-rule never predicts act (act F1 undefined, 0/6 recall)
   but gets ask and silent mostly right, which is why its accuracy (63%) looks respectable while its balanced
   accuracy (55%) and κ (0.47) do not.

## Why cost is still the headline

Accuracy, macro-F1 and κ answer "how often is it right?". The cost matrix answers "how bad are its mistakes for the
product?", and the gates answer "is it safe to ship?". All three are now in every report; when they disagree, as
for Haiku, the disagreement itself is the finding.

Reproduce: `uv run centaur-eval report results/<run>` (the "Classification metrics" section) and
`uv run centaur-eval baselines`.

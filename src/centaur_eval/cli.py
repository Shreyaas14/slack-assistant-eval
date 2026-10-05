"""centaur-eval: evaluate a proactive Slack assistant's act / ask / notify / silent decisions."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import dataset
from .paths import DATA_DIR, ROOT, RUNS_DIR


def cmd_validate(args) -> int:
    scenarios, problems = dataset.load_reporting_errors()
    if not problems:
        problems = dataset.check_dataset(scenarios)
    print(f"{len(scenarios)} scenarios\n{dataset.summary_table(scenarios)}")
    if warnings := dataset.coverage_warnings(scenarios):
        print("\ncoverage notes (not errors):", *(f"  - {w}" for w in warnings), sep="\n")
    if problems:
        print(f"\n{len(problems)} problem(s):", *(f"  - {p}" for p in problems), sep="\n")
        return 1
    from .report import dataset_card

    (DATA_DIR / "LABELS.md").write_text(dataset_card(scenarios))
    print("\nOK (data/LABELS.md refreshed)")
    return 0


def cmd_show(args) -> int:
    from .render import render
    from .replay import view

    scenario_id, _, checkpoint_id = args.item.partition("@")
    s = next((s for s in dataset.load_scenarios() if s.id == scenario_id), None)
    cp = next((c for c in s.checkpoints if c.id == checkpoint_id), None) if s and checkpoint_id else None
    if s is None or (checkpoint_id and cp is None):
        print(f"no item {args.item}", file=sys.stderr)
        return 1
    system, user = render(view(s, cp))
    print(f"{system}\n\n{'=' * 100}\n" if args.system else "", user, sep="")
    return 0


def cmd_baselines(args) -> int:
    from .baselines import score_baselines

    print(f"{'policy':<22}{'mean cost':>10}{'accuracy':>10}{'violations':>12}")
    for name, agg in score_baselines(dataset.load_scenarios()).items():
        h = agg.headline
        print(f"{name:<22}{h.mean_cost:>10.2f}{h.accuracy:>10.0%}{h.gate_trials:>12}")
    return 0


def run_dir_arg(value: str) -> Path:
    """A run directory, or a bare run id under runs/."""
    path = Path(value)
    path = path if path.exists() or path.parent != Path(".") else RUNS_DIR / value
    if not (path / "manifest.json").exists():
        raise ValueError(f"no run at {value} (bare ids are looked up in runs/; pass a path like results/opus-5.5)")
    return path


def positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {n}")
    return n


def cmd_run(args) -> int:
    from . import runner

    if args.resume:
        if fixed := [f"--{k}" for k in ("model", "epochs", "items", "checkpoints") if getattr(args, k)]:
            raise ValueError(f"--resume uses the run's own settings; drop {', '.join(fixed)}")
        run_dir = runner.resume(run_dir_arg(args.resume), concurrency=args.concurrency)
    else:
        model = args.model or os.environ.get("CENTAUR_EVAL_MODEL")
        if not model:
            print("pass --model (e.g. anthropic/claude-opus-5.5) or set CENTAUR_EVAL_MODEL", file=sys.stderr)
            return 2
        config = runner.RunConfig(
            model=model,
            epochs=args.epochs or 3,
            checkpoints=args.checkpoints,
            items=tuple(i.strip() for i in args.items.split(",") if i.strip()) if args.items else (),
        )
        run_dir = runner.start(config, RUNS_DIR, concurrency=args.concurrency)
    return cmd_report(argparse.Namespace(run_dir=run_dir))


def cmd_report(args) -> int:
    from .report import write_run_report

    print(write_run_report(run_dir_arg(str(args.run_dir))))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="centaur-eval", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("validate", help="check scenarios and refresh the dataset card").set_defaults(func=cmd_validate)

    sh = sub.add_parser("show", help="print the prompt for one scenario (S07, or S12@early for a checkpoint)")
    sh.add_argument("item")
    sh.add_argument("--system", action="store_true", help="also print the system prompt")
    sh.set_defaults(func=cmd_show)

    sub.add_parser("baselines", help="score trivial policies (no API key)").set_defaults(func=cmd_baselines)

    r = sub.add_parser("run", help="run a model over the dataset")
    r.add_argument(
        "--model",
        help="OpenRouter model id, or baseline:<name> (see `baselines` for names; default $CENTAUR_EVAL_MODEL)",
    )
    r.add_argument("--resume", metavar="RUN", help="finish an interrupted run (run id or directory) with its settings")
    r.add_argument("--epochs", type=positive_int, help="trials per scenario (default 3)")
    r.add_argument("--checkpoints", action="store_true", help="also run each scenario at its other moments")
    r.add_argument("--items", help="comma-separated scenario ids (default: all)")
    r.add_argument("--concurrency", type=positive_int, default=4, help="parallel API calls (default 4)")
    r.set_defaults(func=cmd_run)

    rp = sub.add_parser("report", help="re-score a run and rewrite its report")
    rp.add_argument("run_dir", metavar="RUN", help="run id or directory")
    rp.set_defaults(func=cmd_report)
    return p


def _load_dotenv() -> None:
    """Read KEY=VALUE lines from .env; variables already set in the environment win."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.strip().startswith("#") and value.strip():
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

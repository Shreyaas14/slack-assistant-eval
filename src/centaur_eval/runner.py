"""Run a model (or baseline) over every item, appending label-free traces to runs/<run_id>/traces.jsonl.

A run's settings live in manifest.json, so `resume` needs nothing but the run directory.
"""

from __future__ import annotations

import itertools
import json
import platform
import re
import subprocess
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import dataset
from .baselines import get_policy
from .parse import parse_decision
from .paths import PROMPTS_DIR, ROOT, sha
from .providers import RawResult, make_provider
from .render import render
from .replay import items
from .schema import Scenario, Trace

Decide = Callable[[Scenario, int, str, str], RawResult]  # (item, epoch, system prompt, user prompt) -> raw result
TraceKey = tuple[str, int]  # (item id, epoch)


@dataclass(frozen=True)
class RunConfig:
    model: str  # an OpenRouter model id (e.g. "anthropic/claude-opus-5.5"), or "baseline:<name>"
    epochs: int = 3
    checkpoints: bool = False
    items: tuple[str, ...] = ()

    def run_id(self, now: datetime) -> str:
        tags = [self.model] + (["checkpoints"] if self.checkpoints else [])
        return f"{now:%Y%m%d-%H%M%S}-" + re.sub(r"[^a-zA-Z0-9]+", "-", "-".join(tags)).strip("-")

    def scenarios(self) -> list[Scenario]:
        available = dataset.load_scenarios()
        if not self.items:
            return available
        if unknown := sorted(set(self.items) - {s.id for s in available}):
            valid = ", ".join(s.id for s in available)
            raise ValueError(f"unknown scenario id(s) {', '.join(unknown)}; choose from {valid}")
        return [s for s in available if s.id in self.items]


@dataclass
class Manifest:
    run_id: str
    config: RunConfig
    created_at: str
    dataset_sha: str
    prompt_shas: dict[str, str]
    git_sha: str | None
    python: str = field(default_factory=platform.python_version)

    def write(self, run_dir: Path) -> None:
        (run_dir / "manifest.json").write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def read(cls, run_dir: Path) -> Manifest:
        data = json.loads((run_dir / "manifest.json").read_text())
        cfg = data.pop("config")
        return cls(config=RunConfig(**{**cfg, "items": tuple(cfg["items"])}), **data)


def make_decide(config: RunConfig) -> tuple[Decide, str, dict]:
    """The decision function for a run, plus the provider name and params recorded in its traces."""
    if config.model.startswith("baseline:"):
        policy = get_policy(config.model.removeprefix("baseline:"), config.scenarios())
        return (
            (lambda item, epoch, system, user: RawResult(raw=policy(item, epoch))),
            "baseline",
            {},
        )
    provider = make_provider(config.model)
    return (
        (lambda item, epoch, system, user: provider.complete(system, user)),
        provider.name,
        provider.params,
    )


def run_trial(
    decide: Decide,
    item: Scenario,
    epoch: int,
    *,
    run_id: str,
    config: RunConfig,
    provider: str,
    params: dict,
) -> Trace:
    system, user = render(item)
    started = time.monotonic()
    try:
        result = decide(item, epoch, system, user)
    except Exception as e:  # noqa: BLE001 -- an adapter bug must not take down the whole run
        result = RawResult(raw=None, error=f"{type(e).__name__}: {e}")
    if result.error:
        status, decision, error = "api_error", None, result.error
    else:
        status, decision, error = parse_decision(result.raw, refusal=result.refusal)
    raw = result.raw if isinstance(result.raw, str | None) else json.dumps(result.raw, ensure_ascii=False)
    return Trace(
        run_id=run_id,
        item_id=item.id,
        epoch=epoch,
        provider=provider,
        model=config.model,
        params=params,
        system_prompt_sha=sha(system),
        prompt_sha=sha(system + user),
        user_prompt=user,
        raw_response=raw,
        parse_status=status,
        decision=decision,
        error=error,
        latency_ms=int((time.monotonic() - started) * 1000),
        usage=result.usage,
        ts=datetime.now(UTC),
    )


def load_traces(run_dir: Path) -> list[Trace]:
    """Traces in a run, last write wins per (item, epoch); a half-written final line is ignored."""
    path = run_dir / "traces.jsonl"
    latest: dict[TraceKey, Trace] = {}
    for line in path.read_text().splitlines() if path.exists() else []:
        try:
            t = Trace.model_validate_json(line)
        except ValueError:
            continue
        latest[(t.item_id, t.epoch)] = t
    return list(latest.values())


def _prompt_shas() -> dict[str, str]:
    return {n: sha((PROMPTS_DIR / n).read_text()) for n in ("policy.md", "workspace.md", "wrapper.md")}


def start(config: RunConfig, out_dir: Path, *, concurrency: int = 4) -> Path:
    scenarios = config.scenarios()
    if not scenarios:
        raise ValueError("no scenarios selected")
    make_decide(config)  # fail on a bad model or missing key before creating a run directory
    run_id = base = config.run_id(datetime.now().astimezone())
    for n in itertools.count(2):
        if not (out_dir / run_id).exists():
            break
        run_id = f"{base}-{n}"
    run_dir = out_dir / run_id
    run_dir.mkdir(parents=True)
    Manifest(
        run_id=run_id,
        config=config,
        created_at=datetime.now(UTC).isoformat(),
        dataset_sha=dataset.dataset_sha(scenarios),
        prompt_shas=_prompt_shas(),
        git_sha=_git_sha(),
    ).write(run_dir)
    return _execute(run_dir, concurrency)


def resume(run_dir: Path, *, concurrency: int = 4) -> Path:
    """Finish a run with the settings in its manifest. Trials that hit API errors are retried."""
    manifest = Manifest.read(run_dir)
    if dataset.dataset_sha(manifest.config.scenarios()) != manifest.dataset_sha:
        print("warning: scenarios changed since this run started; new trials use the current data")
    if _prompt_shas() != manifest.prompt_shas:
        print("warning: prompts changed since this run started; new trials use the current prompts")
    return _execute(run_dir, concurrency)


def _execute(run_dir: Path, concurrency: int) -> Path:
    manifest = Manifest.read(run_dir)
    config = manifest.config
    decide, provider, params = make_decide(config)
    done = {(t.item_id, t.epoch) for t in load_traces(run_dir) if t.parse_status != "api_error"}
    jobs = [
        (item, e)
        for item in items(config.scenarios(), checkpoints=config.checkpoints)
        for e in range(1, config.epochs + 1)
        if (item.id, e) not in done
    ]
    total = len(done) + len(jobs)
    print(f"{manifest.run_id}: {len(jobs)} trials to run ({len(done)} already done)")
    pool = ThreadPoolExecutor(max_workers=concurrency)
    futures = [
        pool.submit(run_trial, decide, item, e, run_id=manifest.run_id, config=config, provider=provider, params=params)
        for item, e in jobs
    ]
    written: set[Future] = set()
    with open(run_dir / "traces.jsonl", "a") as out:

        def record(future: Future) -> None:
            t = future.result()
            out.write(t.model_dump_json() + "\n")
            out.flush()
            written.add(future)
            outcome = t.decision.action if t.decision else t.parse_status
            print(f"[{len(written)}/{len(jobs)}] {t.item_id} e{t.epoch}: {outcome}", flush=True)

        try:
            for future in as_completed(futures):
                record(future)
        except KeyboardInterrupt:
            pool.shutdown(cancel_futures=True)  # queued trials never start; in-flight ones finish and are kept
            for future in futures:
                if future not in written and not future.cancelled() and future.exception() is None:
                    record(future)
            print(
                f"\ninterrupted: {len(done) + len(written)}/{total} trials done; "
                f"resume with centaur-eval run --resume {run_dir}"
            )
            raise
        finally:
            pool.shutdown(cancel_futures=True)
    return run_dir


def _git_sha() -> str | None:
    """The commit the run's code came from, suffixed "-dirty" for uncommitted changes."""
    try:
        out = subprocess.run(
            ["git", "describe", "--always", "--dirty", "--exclude=*"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None

"""End to end through the CLI with a baseline (no API key), plus resume and interrupt semantics."""

import json

import pytest

from centaur_eval import cli, runner
from centaur_eval.providers import RawResult
from centaur_eval.runner import Manifest, load_traces


@pytest.fixture
def runs(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "RUNS_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def two_ids(scenarios):
    return [s.id for s in scenarios[:2]]


def _run(runs, *extra):
    assert cli.main(["run", "--model", "baseline:keyword", "--epochs", "2", *extra]) == 0
    (run_dir,) = runs.iterdir()
    return run_dir


def test_run_writes_traces_and_a_report(runs):
    run_dir = _run(runs, "--checkpoints")
    for name in ("manifest.json", "traces.jsonl", "report.md", "failures.md", "errors.csv", "scores.json"):
        assert (run_dir / name).exists(), name
    report = (run_dir / "report.md").read_text()
    assert "Other moments (checkpoints)" in report and "INCOMPLETE" not in report
    assert Manifest.read(run_dir).config.checkpoints
    assert all(t.parse_status == "ok" for t in load_traces(run_dir))
    assert cli.main(["report", run_dir.name]) == 0  # a bare run id resolves under runs/


def test_resume_uses_the_manifest_and_retries_api_errors(runs, two_ids):
    run_dir = _run(runs, "--items", ", ".join(two_ids))
    path = run_dir / "traces.jsonl"
    lines = path.read_text().splitlines()
    broken = json.loads(lines[0]) | {"parse_status": "api_error", "decision": None, "error": "429"}
    path.write_text("\n".join([json.dumps(broken), *lines[1:], '{"half-written']) + "\n")
    assert cli.main(["run", "--resume", str(run_dir)]) == 0
    traces = load_traces(run_dir)
    assert len(traces) == 2 * 2  # 2 items x 2 epochs, nothing added beyond the manifest
    assert all(t.parse_status == "ok" for t in traces)


def test_a_partial_run_is_reported_as_incomplete(runs, two_ids):
    run_dir = _run(runs, "--items", ",".join(two_ids))
    (run_dir / "traces.jsonl").write_text("")
    assert cli.main(["report", str(run_dir)]) == 0
    report = (run_dir / "report.md").read_text()
    assert "INCOMPLETE: 0/4 trials" in report and "No trials scored" in report and "**Gate:**" not in report


def test_ctrl_c_stops_queued_trials_and_keeps_finished_ones(runs, two_ids, monkeypatch, capsys):
    calls = []

    def decide(item, epoch, system, user):
        calls.append(item.id)
        if len(calls) == 3:
            raise KeyboardInterrupt
        return RawResult(raw={"action": "silent", "evidence_msg_ids": [], "rationale": "r"})

    monkeypatch.setattr(runner, "make_decide", lambda config: (decide, "test", {}))
    args = ["run", "--model", "baseline:keyword", "--epochs", "3", "--concurrency", "1", "--items", ",".join(two_ids)]
    assert cli.main(args) == 130
    (run_dir,) = runs.iterdir()
    kept = len(load_traces(run_dir))  # trials finished before the interrupt, plus at most one already in flight
    assert 2 <= kept == len(calls) - 1 < 5
    assert (
        f"interrupted: {kept}/6 trials done; resume with centaur-eval run --resume {run_dir}" in capsys.readouterr().out
    )


@pytest.mark.parametrize(
    "args, message",
    [
        (["--model", "baseline:nope"], "unknown baseline 'nope'; choose from oracle"),
        (["--model", "baseline:keyword", "--items", "S999"], "unknown scenario id(s) S999; choose from"),
        (["--resume", "x", "--epochs", "2"], "drop --epochs"),
    ],
)
def test_bad_run_arguments_are_clean_errors(runs, capsys, args, message):
    assert cli.main(["run", *args]) == 2
    assert message in capsys.readouterr().err
    assert not any(runs.iterdir())


@pytest.mark.parametrize("flag", ["--epochs", "--concurrency"])
def test_counts_must_be_positive(runs, flag):
    with pytest.raises(SystemExit):
        cli.main(["run", "--model", "baseline:keyword", flag, "0"])


def test_show(capsys, scenarios):
    s = next(s for s in scenarios if s.checkpoints)
    assert cli.main(["show", s.id]) == 0
    assert "Centaur was invoked at" in capsys.readouterr().out
    assert cli.main(["show", f"{s.id}@{s.checkpoints[0].id}"]) == 0
    assert cli.main(["show", "S999"]) == 1


def test_missing_api_key_is_a_clean_error_and_creates_nothing(runs, monkeypatch, capsys):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(cli, "_load_dotenv", lambda: None)
    assert cli.main(["run", "--model", "anthropic/claude-opus-5.5"]) == 2
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err
    assert not any(runs.iterdir())

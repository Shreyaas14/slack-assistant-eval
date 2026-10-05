"""The published runs stay reproducible: their prompts match what the current code renders."""

import json
from pathlib import Path

import pytest

from centaur_eval.paths import ROOT
from centaur_eval.render import render

RUNS = sorted(p.parent for p in (ROOT / "results").glob("**/traces.jsonl"))


@pytest.mark.parametrize("run", RUNS, ids=lambda p: str(p.relative_to(ROOT / "results")))
def test_published_prompts_match_current_render(run: Path, views):
    by_id = {v.id: v for v in views}
    for line in (run / "traces.jsonl").read_text().splitlines():
        trace = json.loads(line)
        assert render(by_id[trace["item_id"]])[1] == trace["user_prompt"], trace["item_id"]

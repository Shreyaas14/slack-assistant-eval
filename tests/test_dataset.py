"""The dataset invariants hold, and the validator catches the runtime mistakes it exists to catch."""

import shutil
from datetime import timedelta

from centaur_eval import dataset


def test_dataset_invariants_hold(scenarios):
    assert dataset.check_dataset(scenarios) == []


def test_current_dataset_meets_its_design_targets(scenarios):
    """Documents the shipped dataset's coverage targets. Not a validation rule: `validate` only prints these."""
    assert dataset.coverage_warnings(scenarios) == []


def test_removing_scenarios_is_never_a_validation_error(scenarios):
    standalone = [s for s in scenarios if not s.variant_of and not s.family_id]
    assert dataset.check_dataset(standalone[:3]) == []


def test_trigger_timing_must_follow_the_quiet_period(scenarios):
    s = next(s for s in scenarios if s.context.trigger.kind in ("mention", "message"))
    late = s.model_copy(update={"context": s.context.model_copy(update={"now": s.context.now + timedelta(hours=1)})})
    assert any("quiet period" in p for p in dataset.check_trigger(late))


def test_messages_after_the_invocation_are_rejected(scenarios):
    s = scenarios[0]
    future = s.transcript[-1].model_copy(update={"id": "late", "ts": s.context.now + timedelta(minutes=5)})
    broken = s.model_copy(update={"transcript": [*s.transcript, future]})
    assert any("after the invocation time" in p for p in dataset.check_runtime(broken))


def test_visible_elsewhere_requires_membership(scenarios):
    s = next(s for s in scenarios if s.context.visible_elsewhere)
    channel = s.context.visible_elsewhere[0].channel
    channels = [c.model_copy(update={"centaur_member": c.name != channel}) for c in s.context.channels]
    broken = s.model_copy(update={"context": s.context.model_copy(update={"channels": channels})})
    assert any("not a channel Centaur is a member of" in p for p in dataset.check_runtime(broken))


def test_principle_ids_are_only_defined_rules():
    ids = dataset.principle_ids()
    assert {"D1", "AUTH"} <= ids
    assert not ids & {"The", "Correct", "Wrong", "Workspace", "Visible"}


def test_file_problems_are_reported_by_name(tmp_path):
    src = next(dataset.SCENARIO_DIR.glob("S*.yaml"))
    sid = src.name.partition("_")[0]
    shutil.copy(src, tmp_path / src.name)
    shutil.copy(src, tmp_path / f"{sid}_copy.yaml")  # same id in two files
    shutil.copy(src, tmp_path / "S99_wrong_id.yaml")  # id does not match the file name
    shutil.copy(src, tmp_path / "new_scenario.yml")  # would never be loaded
    (tmp_path / "S98_broken.yaml").write_text("id: S98\noops: [\n")
    scenarios, errors = dataset.load_reporting_errors(tmp_path)
    assert len(scenarios) == 2
    text = "\n".join(errors)
    assert f"duplicate id {sid}" in text and src.name in text
    assert "S99_wrong_id.yaml: id" in text
    assert "new_scenario.yml: not loaded" in text
    assert "S98_broken.yaml: invalid YAML" in text

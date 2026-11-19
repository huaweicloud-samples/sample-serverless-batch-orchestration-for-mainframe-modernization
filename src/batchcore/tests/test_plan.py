"""The job chain as data: which steps a run executes and what gates them."""

from batchcore.orchestrator import plan


def test_chain_shape_is_the_documented_one():
    assert [step.identifier for step in plan.CHAIN] == ["JOB1", "JOB2A", "JOB2B", "JOB3"]
    assert plan.BY_ID["JOB3"].depends == ("JOB2A", "JOB2B")
    assert plan.BY_ID["JOB1"].produces == ("JOB2A", "JOB2B")


def test_whole_chain_by_default():
    chosen = plan.plan()
    assert chosen.identifiers == ["JOB1", "JOB2A", "JOB2B", "JOB3"]
    assert chosen.gated is True
    assert chosen.size == 4


def test_dependencies_follow_the_chain_when_gated():
    chosen = plan.plan()
    assert chosen.dependencies("JOB2A") == ("JOB1",)
    assert chosen.dependencies("JOB3") == ("JOB2A", "JOB2B")
    assert chosen.dependencies("JOB1") == ()


def test_a_dependency_outside_the_plan_is_dropped():
    chosen = plan.plan(["JOB2A", "JOB3"])
    # JOB1 is not part of this run, so JOB2A stands on its own instead of
    # waiting for a step nobody intends to start.
    assert chosen.dependencies("JOB2A") == ()
    assert chosen.dependencies("JOB3") == ("JOB2A",)


def test_an_ungated_plan_has_no_dependencies_at_all():
    chosen = plan.plan(["JOB1", "JOB3"], gated=False)
    assert chosen.dependencies("JOB3") == ()
    assert chosen.identifiers == ["JOB1", "JOB3"]
    assert chosen.summary()["shape"] == "independent modules"


def test_unknown_identifier_is_dropped_not_guessed():
    chosen = plan.plan(["NOPE", "JOB1"])
    assert chosen.identifiers == ["JOB1"]


def test_dependencies_of_a_name_not_in_the_plan():
    assert plan.plan().dependencies("SOMETHING-ELSE") == ()


def test_concurrency_limits_the_slots():
    chosen = plan.plan(concurrency=3)
    assert chosen.slots_free(0) == 3
    assert chosen.slots_free(2) == 1
    assert chosen.slots_free(5) == 0


def test_without_concurrency_every_step_may_run():
    chosen = plan.plan()
    assert chosen.slots_free(9) == 4


def test_summary_carries_what_a_checkpoint_has_to_rebuild():
    summary = plan.plan(["JOB1"], gated=False, concurrency=2).summary()
    assert summary == {
        "steps": ["JOB1"],
        "gated": False,
        "concurrency": 2,
        "shape": "independent modules",
    }


def test_plan_is_iterable_and_reusable():
    chosen = plan.plan()
    assert [step.identifier for step in chosen] == chosen.identifiers
    assert [step.identifier for step in chosen] == chosen.identifiers

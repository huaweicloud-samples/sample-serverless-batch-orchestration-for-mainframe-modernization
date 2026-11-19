"""The run checkpoint: phases, the trail of it, and where it is kept."""

import json

import pytest

from batchcore.orchestrator import state
from batchcore.shared import config


def test_a_step_starts_pending_and_not_terminal():
    step = state.StepState("JOB1")
    assert step.phase == state.PENDING
    assert step.terminal is False
    assert step.attempts == 0


@pytest.mark.parametrize("phase", [state.SUCCEEDED, state.FAILED, state.SKIPPED])
def test_terminal_phases(phase):
    step = state.StepState("JOB1", phase=phase)
    assert step.terminal is True


@pytest.mark.parametrize("phase", [state.PENDING, state.RUNNING, state.WAITING])
def test_phases_that_still_have_work(phase):
    assert state.StepState("JOB1", phase=phase).terminal is False


def test_a_step_round_trips_through_json():
    step = state.StepState(
        "JOB2A",
        phase=state.FAILED,
        job="job-job2a-run-1",
        attempts=2,
        error_type="BATCH_JOB_FAILED",
        exit_code=3,
        detail="container exited",
    )
    again = state.StepState.from_dict("JOB2A", json.loads(json.dumps(step.to_dict())))
    assert again.to_dict() == step.to_dict()
    assert again.exit_code == 3
    assert again.error_type == "BATCH_JOB_FAILED"


def test_a_missing_step_body_is_read_as_pending():
    step = state.StepState.from_dict("JOB1", None)
    assert step.phase == state.PENDING
    assert step.attempts == 0
    assert step.exit_code is None


def test_recording_a_terminal_phase_stamps_the_end_once():
    run = state.RunState("run-1", "scheduled", "2026-10-26")
    step = run.begin("JOB1")
    run.record(step, state.RUNNING, job="job-job1-run-1", attempts=1)
    assert step.finished_at is None
    run.record(step, state.SUCCEEDED, exit_code=0)
    first_end = step.finished_at
    assert first_end
    run.record(step, state.SUCCEEDED, exit_code=0)
    assert step.finished_at == first_end


def test_the_trail_is_kept_but_bounded():
    run = state.RunState("run-1", "scheduled", "2026-10-26")
    step = run.begin("JOB1")
    for _ in range(250):
        run.record(step, state.RUNNING)
    assert len(run.transitions) == 200
    assert run.transitions[-1][0] == "JOB1"
    assert run.transitions[-1][1] == state.RUNNING
    run = state.RunState("run-1", "scheduled", "2026-10-26")
    step = run.begin("JOB1")
    run.record(step, state.WAITING, error_type="BATCH_JOB_TIMEOUT", retry_at="later")
    assert step.error_type == "BATCH_JOB_TIMEOUT"
    assert step.retry_at == "later"
    assert step.to_dict()["retryAt"] == "later"


def test_phase_groups_answer_what_the_pass_can_do_next():
    run = state.RunState("run-1", "scheduled", "2026-10-26")
    first = run.begin("JOB1")
    second = run.begin("JOB2A")
    third = run.begin("JOB3")
    run.record(first, state.RUNNING)
    run.record(second, state.WAITING)
    run.record(third, state.SKIPPED)
    assert run.phases() == {"JOB1": "running", "JOB2A": "waiting for retry", "JOB3": "skipped"}
    assert [step.identifier for step in run.running] == ["JOB1"]
    assert [step.identifier for step in run.waiting] == ["JOB2A"]
    assert sorted(step.identifier for step in run.unfinished) == ["JOB1", "JOB2A"]


def test_a_whole_run_round_trips_through_json():
    run = state.RunState("run-1", "regression", "2026-10-26", isolation="runs/run-1/", budget=120)
    run.plan = {"steps": ["JOB1"], "gated": False}
    step = run.begin("JOB1")
    run.record(step, state.FAILED, error_type="BATCH_JOB_FAILED", exit_code=1)
    run.error_type = "BATCH_JOB_FAILED"
    run.message = "1 step(s) failed"
    again = state.RunState.from_dict(json.loads(json.dumps(run.to_dict())))
    assert again.run_id == "run-1"
    assert again.isolation == "runs/run-1/"
    assert again.budget == 120
    assert again.plan == run.plan
    assert again.message == "1 step(s) failed"
    assert again.transitions == run.transitions
    assert again.step("JOB1").exit_code == 1


def test_from_dict_falls_back_to_usable_defaults():
    run = state.RunState.from_dict({"runId": "r"})
    assert run.status == state.RUN_RUNNING
    assert run.steps == {}
    assert run.started_at


@pytest.mark.parametrize(
    "run_id,step,expected",
    [
        ("batch-2026-10-26", "JOB1", "job-job1-batch-2026-10-26"),
        ("Night 7", "JOB2A", "job-job2a-night-7"),
        ("***", "JOB3", "job-job3-x"),
    ],
)
def test_job_names_are_lowercase_dashes_only(run_id, step, expected):
    assert state.job_name(run_id, step) == expected


def test_a_long_run_identifier_is_cut_to_the_platform_limit():
    name = state.job_name("r" * 200, "JOB1")
    assert len(name) <= 63
    assert not name.endswith("-")


def test_the_checkpoint_path_is_under_the_runs_prefix(env, store):
    points = state.Checkpoints(store)
    assert points.path("run-1") == "runs/run-1.json"


def test_a_saved_run_reads_back(env, store):
    points = state.Checkpoints(store)
    run = state.RunState("run-1", "scheduled", "2026-10-26")
    run.begin("JOB1")
    assert points.save(run) is True
    again = points.load("run-1")
    assert again is not None
    assert again.phases() == {"JOB1": "pending"}


def test_an_absent_or_unreadable_checkpoint_is_reported_not_guessed(env, store, backend):
    points = state.Checkpoints(store)
    assert points.load("nothing-here") is None
    backend.put_object("runs/broken.json", "{ not json")
    assert points.load("broken") is None


def test_open_runs_reads_only_the_ones_still_in_flight(env, store, backend):
    points = state.Checkpoints(store)
    for run_id, status in (("old", state.RUN_SUCCEEDED), ("live", state.RUN_RUNNING),
                           ("other", state.RUN_FAILED)):
        run = state.RunState(run_id, "scheduled", "2026-10-26")
        run.status = status
        points.save(run)
    backend.put_object("runs/notes.txt", "not a checkpoint")
    found = points.open_runs()
    assert [run.run_id for run in found] == ["live"]


def test_open_runs_keeps_the_order_runs_were_opened_in(env, store):
    points = state.Checkpoints(store)
    for run_id, started in (("second", "2026-10-02T00:00:00+00:00"),
                            ("first", "2026-10-01T00:00:00+00:00")):
        run = state.RunState(run_id, "scheduled", "2026-10-01", started_at=started)
        points.save(run)
    assert [run.run_id for run in points.open_runs()] == ["first", "second"]


def test_open_runs_can_wait_for_a_run_to_look_abandoned(env, store):
    points = state.Checkpoints(store)
    fresh = state.RunState("fresh", "scheduled", "2026-10-26")
    points.save(fresh)
    stale = state.RunState(
        "stale", "scheduled", "2026-10-26", started_at="2026-10-01T00:00:00+00:00"
    )
    points.save(stale)
    assert [run.run_id for run in points.open_runs(older_than_seconds=3600)] == ["stale"]


def test_an_unreadable_lake_yields_no_runs_rather_than_an_error(env, store, backend):
    backend.fail_on_prefix = "runs/"
    assert state.Checkpoints(store).open_runs() == []


def test_delete_removes_the_checkpoint(env, store):
    points = state.Checkpoints(store)
    run = state.RunState("run-1", "scheduled", "2026-10-26")
    points.save(run)
    assert points.delete("run-1") is True
    assert points.load("run-1") is None


def test_an_unreadable_age_counts_as_brand_new():
    assert state._age("nonsense") == 0
    assert state._age(None) == 0


def test_the_budget_default_comes_from_the_deployment(env):
    env.setenv(config.STAGE_BUDGET_SECONDS, "300")
    assert state.deadline_seconds(None) == 300.0
    assert state.deadline_seconds(45) == 45.0
    env.setenv(config.STAGE_BUDGET_SECONDS, "not a number")
    assert state.deadline_seconds(None) == 900.0


def test_a_pass_gives_way_before_its_own_limit_runs_out(env, clock):
    budget = state.Budget(100, clock=clock)
    assert budget.expired() is False
    clock.advance(50)
    assert budget.expired() is False
    clock.advance(49)
    assert budget.expired() is True
    assert budget.left() == 1.0


def test_a_wait_never_leaves_the_budget(env, clock):
    budget = state.Budget(60, clock=clock)
    assert budget.sleep(0) is True
    assert budget.sleep(10) is True
    assert clock.slept == [10]
    clock.advance(45)
    # Only the time left is spent, and the wait reports that it was cut short.
    assert budget.sleep(30) is False
    assert clock.slept == [10, 5]
    clock.advance(60)
    assert budget.sleep(1) is False
    assert clock.slept == [10, 5]

"""One pass of the orchestration function, start to finish.

The engine is handed stand-ins for its contact points - the data lake, the
cluster, the validation functions and the two publishing calls - and is asked to
do what a scheduled pass does. A run is walked across passes through its
checkpoint object, so the assertions here cover both a single pass and what the
next pass finds.
"""

import json
from datetime import datetime, timezone

import pytest

from conftest import days_ago, http_date, make_store
from batchcore.checks import calendar_check, file_check
from batchcore.orchestrator import events, index, plan, state
from batchcore.shared import config
from batchcore.shared.hwc import http

CREDS = ("AK", "SK", None)
FILE_URN = "urn:fgs:cn-north-4:project:functions:batch-checks:file-check"
CALENDAR_URN = "urn:fgs:cn-north-4:project:functions:batch-checks:calendar-check"

# What each program leaves in the lake, so a pass can be walked against the
# real input validation instead of a table of answers.
OUTPUTS = {
    "JOB1": "processing/JOB1/output/normalized.csv",
    "JOB2A": "processing/JOB2A/output/fees.csv",
    "JOB2B": "processing/JOB2B/output/rebates.csv",
    "JOB3": "outbound/settled.csv",
}


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def wire(env, topic="urn:smn:cn-north-4:project:batch"):
    env.setenv(config.FILE_CHECK_URN, FILE_URN)
    env.setenv(config.CALENDAR_CHECK_URN, CALENDAR_URN)
    env.setenv(config.NOTIFICATION_TOPIC_URN, topic)
    return env


def present(step):
    return {
        "step": step,
        "allFilesPresent": True,
        "errorType": None,
        "message": "Every input of %s is present." % step,
        "missingCount": 0,
        "maxAgeDays": 2,
    }


def absent(step, error_type):
    return {
        "step": step,
        "allFilesPresent": False,
        "errorType": error_type,
        "message": "1 input check(s) of %s did not pass." % step,
        "missingCount": 1,
        "maxAgeDays": 2,
    }


class Gates:
    """The two validation functions, answered from a table."""

    def __init__(self, files=None, business_day=True, error=None):
        self.files = dict(files or {})
        self.business_day = business_day
        self.error = error
        self.asked = []

    def __call__(self, urn, payload):
        self.asked.append((urn, dict(payload)))
        failure = None
        if isinstance(self.error, dict):
            failure = self.error.get(urn)
        elif self.error is not None:
            failure = self.error
        if isinstance(failure, Exception):
            raise failure
        if failure is not None:
            return failure
        if "jobName" in payload:
            answer = self.files.get(payload["jobName"], None)
            if answer is None:
                return present(payload["jobName"])
            return answer() if callable(answer) else answer
        return {
            "isBusinessDay": self.business_day,
            "date": "2026-11-11",
            "reason": "business day" if self.business_day else "weekend",
        }

    @property
    def input_questions(self):
        return [payload for urn, payload in self.asked if "jobName" in payload]

    @property
    def calendar_questions(self):
        return [payload for urn, payload in self.asked if "jobName" not in payload]


class FakeCluster:
    """The batch containers of a run, answered from a table."""

    def __init__(self, outcomes=None, create_errors=None):
        self.outcomes = dict(outcomes or {})
        self.create_errors = list(create_errors or [])
        self.created = []
        self.observed = []

    def create_job(self, body):
        self.created.append(body)
        if self.create_errors:
            raise self.create_errors.pop(0)
        return body

    def observe(self, run_id, identifier, name):
        self.observed.append((run_id, identifier, name))
        outcome = self.outcomes.get(identifier, {"phase": "succeeded", "exitCode": 0})
        if isinstance(outcome, list):
            return outcome.pop(0) if len(outcome) > 1 else outcome[0]
        return outcome

    @property
    def started(self):
        return [body["metadata"]["labels"]["batch.job/task"] for body in self.created]


class WritingCluster(FakeCluster):
    """A cluster whose containers really leave their data set behind."""

    def __init__(self, store, isolation="", **kwargs):
        super().__init__(**kwargs)
        self.store = store
        self.isolation = isolation

    def create_job(self, body):
        step = body["metadata"]["labels"]["batch.job/task"]
        key = self.isolation + OUTPUTS[step]
        self.store.backend.put_object(key, "a,b\n")
        self.store.backend.times[key] = http_date(days_ago(0))
        return super().create_job(body)


def engine_for(store, cluster, gates, clock, seconds=60, notifier=None, reporter=None):
    notices = []
    reports = []
    if notifier is None:
        def notifier(topic, subject_line, message):
            notices.append((topic, subject_line, message))
            return {"published": topic}

    if reporter is None:
        def reporter(points):
            reports.append(points)
            return {"message_id": "reported"}

    subject = index.Engine(
        CREDS,
        context=None,
        store=store,
        cluster=cluster,
        invoker=gates,
        notifier=notifier,
        reporter=reporter,
        budget=state.Budget(seconds, clock=clock),
    )
    subject.notices = notices
    subject.reports = reports
    return subject


def run_action(action=events.ACTION_START, **fields):
    payload = dict(action=action)
    payload.update(fields)
    return events.parse(payload)


def resumed(store, clock, gates=None, cluster=None, seconds=60):
    """The next scheduled pass over the runs the lake says are in flight."""
    subject = engine_for(store, cluster or FakeCluster(), gates or Gates(), clock, seconds=seconds)
    return subject.handle(run_action(action=events.ACTION_RESUME))


# -- opening a run ---------------------------------------------------------


def test_a_scheduled_pass_opens_the_whole_chain(env, store, clock):
    wire(env)
    cluster = FakeCluster()
    answer = engine_for(store, cluster, Gates(), clock).handle(run_action())
    assert answer["action"] == events.ACTION_START
    report = answer["runs"][0]
    assert report["status"] == state.RUN_SUCCEEDED
    assert report["phases"] == {
        "JOB1": "succeeded", "JOB2A": "succeeded", "JOB2B": "succeeded", "JOB3": "succeeded",
    }
    assert cluster.started == ["JOB1", "JOB2A", "JOB2B", "JOB3"]


def test_the_plan_of_a_run_is_recorded_with_the_run(env, store, clock):
    wire(env)
    engine_for(store, FakeCluster(), Gates(), clock).handle(run_action())
    saved = state.Checkpoints(store).load("batch-%s" % today())
    assert saved.plan["steps"] == ["JOB1", "JOB2A", "JOB2B", "JOB3"]
    assert saved.plan["shape"] == "chain"
    assert saved.budget == 60
    assert saved.kind == events.SCHEDULED


def test_a_calendar_gate_that_was_never_named_opens_no_run(env, store, clock):
    subject = engine_for(store, FakeCluster(), Gates(), clock)
    report = subject.handle(run_action())["runs"][0]
    assert report["status"] == state.RUN_FAILED
    assert report["errorType"] == "CHECK_NOT_WIRED"
    assert subject.cluster.created == []
    assert state.Checkpoints(store).load("batch-%s" % today()).message == (
        "the calendar gate is not wired to the run"
    )


def test_a_calendar_gate_that_cannot_be_reached_is_a_failure_not_a_free_day(env, store, clock):
    wire(env)
    refused = http.ApiError("the validation function is gone", status=500)
    subject = engine_for(store, FakeCluster(), Gates(error=refused), clock)
    report = subject.handle(run_action())["runs"][0]
    assert report["errorType"] == "CHECK_INVOCATION_FAILED"
    assert report["status"] == state.RUN_FAILED
    assert subject.cluster.created == []


def test_a_calendar_gate_that_answers_without_a_verdict_is_not_guessed(env, store, clock):
    wire(env)
    gates = Gates(error={CALENDAR_URN: {"answered": "something else"}})
    report = engine_for(store, FakeCluster(), gates, clock).handle(run_action())["runs"][0]
    assert report["errorType"] == "ANSWER_UNREADABLE"


def test_a_launch_on_a_non_processing_day_ends_without_a_step_or_a_notice(env, store, clock):
    wire(env)
    gates = Gates(business_day=False)
    cluster = FakeCluster()
    subject = engine_for(store, cluster, gates, clock)
    report = subject.handle(run_action())["runs"][0]
    assert report["status"] == state.RUN_SKIPPED
    assert cluster.created == []
    assert subject.notices == []
    assert subject.reports == [], "a skipped day must not raise the alarm a failure raises"
    saved = state.Checkpoints(store).load("batch-%s" % today())
    assert saved.message == "Nothing runs on %s: weekend." % today()


def test_the_second_launch_of_the_same_day_continues_the_run_already_open(env, store, clock):
    wire(env)
    opened = state.RunState("batch-%s" % today(), "scheduled", today())
    opened.begin("JOB1")
    state.Checkpoints(store).save(opened)
    subject = engine_for(store, FakeCluster(), Gates(), clock)
    report = subject.handle(run_action())["runs"][0]
    # The steps of the day are not opened a second time over the same data.
    assert report["status"] == state.RUN_SUCCEEDED
    assert len(subject.cluster.created) == 4


def test_a_replay_names_the_day_it_replays_and_asks_the_gate_about_it(env, store, clock):
    wire(env)
    gates = Gates()
    subject = engine_for(store, FakeCluster(), gates, clock)
    report = subject.handle(run_action(runDate="2026-11-11"))["runs"][0]
    assert report["kind"] == events.MANUAL
    assert report["runDate"] == "2026-11-11"
    assert gates.calendar_questions[0] == {"runDate": "2026-11-11"}


def test_a_scheduled_pass_asks_the_gate_about_the_instant_it_fired_at(env, store, clock):
    wire(env)
    gates = Gates()
    engine_for(store, FakeCluster(), gates, clock).handle(run_action())
    asked = gates.calendar_questions[0]
    assert "date" in asked
    assert asked["date"].startswith(today())


# -- walking the chain -----------------------------------------------------


def test_the_steps_that_share_a_level_are_started_together(env, store, clock):
    wire(env)
    running = {"phase": "running"}
    cluster = FakeCluster(
        {"JOB2A": [running, {"phase": "succeeded", "exitCode": 0}],
         "JOB2B": [running, {"phase": "succeeded", "exitCode": 0}]}
    )
    report = engine_for(store, cluster, Gates(), clock).handle(run_action())["runs"][0]
    assert report["status"] == state.RUN_SUCCEEDED
    assert cluster.started[:3] == ["JOB1", "JOB2A", "JOB2B"]
    assert cluster.started.index("JOB3") > cluster.started.index("JOB2B")


def test_a_container_that_exits_non_zero_stops_the_chain_behind_it(env, store, clock):
    wire(env)
    cluster = FakeCluster(
        {"JOB1": {"phase": "failed", "reason": "Error", "exitCode": 2,
                  "detail": "no fees column in the extract"}}
    )
    subject = engine_for(store, cluster, Gates(), clock)
    report = subject.handle(run_action())["runs"][0]
    assert report["status"] == state.RUN_FAILED
    assert report["errorType"] == "BATCH_JOB_FAILED"
    assert report["phases"] == {
        "JOB1": "failed", "JOB2A": "skipped", "JOB2B": "skipped", "JOB3": "skipped",
    }
    assert cluster.started == ["JOB1"], "a step whose input never existed must not start"
    saved = state.Checkpoints(store).load(report["runId"])
    assert saved.step("JOB1").detail == "Error / no fees column in the extract"
    assert saved.step("JOB3").error_type == "BATCH_JOB_FAILED"
    assert report["message"] == "1 step(s) failed: JOB1 (BATCH_JOB_FAILED, exit code 2)"


def test_a_genuine_failure_is_not_reported_as_a_missing_input_behind_it():
    failures = [
        state.StepState("JOB2A", phase=state.FAILED, error_type=file_check.MISSING),
        state.StepState("JOB1", phase=state.FAILED, error_type="BATCH_JOB_FAILED"),
    ]
    assert index._first_verdict(failures) == "BATCH_JOB_FAILED"


def test_a_verdict_nobody_listed_still_names_the_step_that_carried_it():
    failures = [state.StepState("JOB1", phase=state.FAILED, error_type="SOMETHING_NEW")]
    assert index._first_verdict(failures) == "SOMETHING_NEW"
    assert index._first_verdict([state.StepState("JOB1", phase=state.FAILED)]) == "BATCH_JOB_FAILED"


def test_a_step_whose_input_never_arrived_is_reported_by_the_next_pass(env, store, clock):
    wire(env)
    gates = Gates(files={"JOB1": lambda: absent("JOB1", file_check.MISSING)})
    cluster = FakeCluster()
    first = engine_for(store, cluster, gates, clock).handle(run_action())
    report = first["runs"][0]
    assert report["phases"]["JOB1"] == state.SKIPPED
    assert cluster.created == []
    assert gates.input_questions[0] == {"jobName": "JOB1"}
    saved = state.Checkpoints(store).load(report["runId"])
    assert saved.step("JOB1").check["allFilesPresent"] is False
    assert "fileResults" not in saved.step("JOB1").check

    closed = resumed(store, clock, gates)["runs"][0]
    assert closed["status"] == state.RUN_FAILED
    assert closed["errorType"] == file_check.MISSING
    assert closed["phases"]["JOB3"] == state.SKIPPED


def test_an_input_that_is_there_but_too_old_is_its_own_answer(env, store, clock):
    wire(env)
    gates = Gates(files={"JOB1": absent("JOB1", file_check.TOO_OLD)})
    engine_for(store, FakeCluster(), gates, clock).handle(run_action())
    closed = resumed(store, clock, gates)["runs"][0]
    assert closed["errorType"] == file_check.TOO_OLD


def test_a_gate_that_cannot_be_reached_fails_the_step_it_was_asked_about(env, store, clock):
    wire(env)
    refused = http.ApiError("the validation function timed out", status=504)
    gates = Gates(error={FILE_URN: refused})
    report = engine_for(store, FakeCluster(), gates, clock).handle(run_action())["runs"][0]
    assert report["phases"]["JOB1"] == state.FAILED
    saved = state.Checkpoints(store).load(report["runId"])
    assert saved.step("JOB1").error_type == "CHECK_INVOCATION_FAILED"
    assert "timed out" in saved.step("JOB1").detail
    closed = resumed(store, clock, gates)["runs"][0]
    assert closed["errorType"] == "CHECK_INVOCATION_FAILED"


def test_an_input_gate_that_was_never_named_is_reported_as_a_wiring_fault(env, store, clock):
    env.setenv(config.CALENDAR_CHECK_URN, CALENDAR_URN)
    gates = Gates()
    subject = engine_for(store, FakeCluster(), gates, clock)
    report = subject.handle(run_action())["runs"][0]
    assert report["phases"]["JOB1"] == state.FAILED
    assert gates.input_questions == [], "no question was asked of a function nothing named"
    closed = resumed(store, clock, gates)["runs"][0]
    assert closed["errorType"] == "CHECK_NOT_WIRED"


def test_the_lake_a_run_is_asked_about_is_the_lake_of_that_run(env, store, clock):
    wire(env)
    key = "inbound/transactions/txns.20261026.csv"
    store.backend.put_object(key, "a,b\n")
    store.backend.times[key] = http_date(days_ago(1))

    def gates(urn, payload):
        if "jobName" in payload:
            return file_check.run(payload, store)
        return calendar_check.run(payload, store)

    cluster = WritingCluster(store)
    report = engine_for(store, cluster, gates, clock).handle(run_action())["runs"][0]
    assert report["status"] == state.RUN_SUCCEEDED
    assert cluster.started == ["JOB1", "JOB2A", "JOB2B", "JOB3"]
    saved = state.Checkpoints(store).load(report["runId"])
    assert saved.step("JOB1").check["allFilesPresent"] is True
    assert json.loads(store.read("runs/%s.json" % report["runId"]))["status"] == "SUCCEEDED"


# -- the retry policy ------------------------------------------------------


def test_a_container_the_platform_ended_is_started_again(env, store, clock):
    wire(env)
    env.setenv(config.RETRY_MAX_ATTEMPTS, "2")
    evicted = {"phase": "failed", "reason": "PodEvicted", "exitCode": None,
               "detail": "the node was drained"}
    cluster = FakeCluster({"JOB1": [evicted, {"phase": "succeeded", "exitCode": 0}]})
    gates = Gates()
    subject = engine_for(store, cluster, gates, clock)
    report = subject.handle(run_action())["runs"][0]
    assert report["status"] == state.RUN_SUCCEEDED
    assert cluster.started.count("JOB1") == 2
    saved = state.Checkpoints(store).load(report["runId"])
    assert saved.step("JOB1").attempts == 2
    assert [entry[1] for entry in saved.transitions[:3]] == [
        state.RUNNING, state.WAITING, state.RUNNING,
    ]
    # A step the platform ended is started again without asking its input gate twice.
    assert len(cluster.created) == 5
    assert len(gates.input_questions) == 4


def test_a_step_that_runs_out_of_attempts_ends_the_run(env, store, clock):
    wire(env)
    env.setenv(config.RETRY_MAX_ATTEMPTS, "2")
    evicted = {"phase": "failed", "reason": "DeadlineExceeded", "exitCode": None}
    cluster = FakeCluster({"JOB1": [evicted, evicted]})
    subject = engine_for(store, cluster, Gates(), clock)
    report = subject.handle(run_action())["runs"][0]
    assert report["errorType"] == "BATCH_JOB_TIMEOUT"
    assert report["phases"]["JOB1"] == state.FAILED
    assert cluster.started.count("JOB1") == 2


def test_a_refused_cluster_call_is_tried_again_in_the_same_pass(env, store, clock):
    wire(env)
    env.setenv(config.RETRY_MAX_ATTEMPTS, "2")
    cluster = FakeCluster(create_errors=[index.ClusterError("busy", status=503)])
    report = engine_for(store, cluster, Gates(), clock).handle(run_action())["runs"][0]
    assert report["status"] == state.RUN_SUCCEEDED
    assert cluster.started.count("JOB1") == 2


def test_a_refusal_that_is_not_the_platforms_fault_defers_the_step(env, store, clock):
    wire(env)
    cluster = FakeCluster(create_errors=[index.ClusterError("no", status=400)])
    report = engine_for(store, cluster, Gates(), clock).handle(run_action())["runs"][0]
    assert report["status"] == state.RUN_SUCCEEDED
    saved = state.Checkpoints(store).load(report["runId"])
    assert state.WAITING in [entry[1] for entry in saved.transitions]
    assert saved.step("JOB1").error_type is None


def test_a_job_that_stops_reporting_anything_is_a_failure_the_run_owns(env, store, clock):
    wire(env)
    cluster = FakeCluster({"JOB1": {"phase": "absent"}})
    subject = engine_for(store, cluster, Gates(), clock)
    report = subject.handle(run_action())["runs"][0]
    assert report["errorType"] == "JOB_NOT_FOUND"
    assert "is gone and the run does not know how it ended" in saved_detail(store, report)


def saved_detail(store, report):
    return state.Checkpoints(store).load(report["runId"]).step("JOB1").detail


def test_a_step_that_is_not_due_yet_is_left_alone(env, store, clock):
    wire(env)
    env.setenv(config.RETRY_MAX_ATTEMPTS, "3")
    env.setenv(config.RETRY_BASE_SECONDS, "600")
    evicted = {"phase": "failed", "reason": "PodEvicted", "exitCode": None}
    cluster = FakeCluster({"JOB1": [evicted]})
    subject = engine_for(store, cluster, Gates(), clock, seconds=20)
    report = subject.handle(run_action())["runs"][0]
    assert report["phases"]["JOB1"] == state.WAITING
    assert cluster.started.count("JOB1") == 1


# -- across passes ---------------------------------------------------------


def test_a_pass_that_runs_out_of_time_leaves_the_run_open_for_the_next_one(env, store, clock):
    wire(env)
    cluster = FakeCluster({"JOB1": {"phase": "running"}})
    answer = engine_for(store, cluster, Gates(), clock, seconds=20).handle(run_action())
    assert answer["budgetSpent"] is True
    report = answer["runs"][0]
    assert report["status"] == state.RUN_RUNNING
    assert report["unfinished"] == ["JOB1", "JOB2A", "JOB2B", "JOB3"]
    assert report["checkpoint"] == "runs/batch-%s.json" % today()

    # The next scheduled pass picks the run up from the checkpoint alone.
    second = FakeCluster()
    again = resumed(store, clock, cluster=second)["runs"][0]
    assert again["status"] == state.RUN_SUCCEEDED
    assert second.started == ["JOB2A", "JOB2B", "JOB3"]


def test_a_container_notice_advances_only_the_run_it_belongs_to(env, store, clock):
    wire(env)
    points = state.Checkpoints(store)
    for run_id in ("night-7", "night-8"):
        run = state.RunState(run_id, "manual", today())
        run.plan = {"steps": ["JOB1"], "gated": False}
        run.begin("JOB1")
        points.save(run)
    subject = engine_for(store, FakeCluster(), Gates(), clock)
    answer = subject.handle(events.parse({"kind": "Job",
                                          "metadata": {"labels": {"batch.job/run-id": "night-7"}}}))
    assert [report["runId"] for report in answer["runs"]] == ["night-7"]
    assert points.load("night-8").step("JOB1").phase == state.PENDING
    assert points.load("night-7").status == state.RUN_SUCCEEDED


def test_a_notice_for_a_run_nobody_has_opened_is_ignored(env, store, clock):
    wire(env)
    subject = engine_for(store, FakeCluster(), Gates(), clock)
    answer = subject.handle(events.parse({"kind": "Job",
                                          "metadata": {"labels": {"batch.job/run-id": "ghost"}}}))
    assert answer["runs"] == []
    assert subject.cluster.created == []


def test_a_resume_pass_walks_every_run_still_in_flight(env, store, clock):
    wire(env)
    points = state.Checkpoints(store)
    for run_id in ("night-7", "night-8"):
        run = state.RunState(run_id, "manual", today())
        run.plan = {"steps": ["JOB1"], "gated": False}
        run.begin("JOB1")
        points.save(run)
    answer = engine_for(store, FakeCluster(), Gates(), clock).handle(
        run_action(action=events.ACTION_RESUME)
    )
    assert sorted(report["runId"] for report in answer["runs"]) == ["night-7", "night-8"]
    assert all(report["status"] == state.RUN_SUCCEEDED for report in answer["runs"])


def test_a_pass_that_finds_nothing_open_answers_with_an_empty_list(env, store, clock):
    wire(env)
    cluster = FakeCluster()
    answer = engine_for(store, cluster, Gates(), clock).handle(
        run_action(action=events.ACTION_RESUME)
    )
    assert answer["runs"] == []
    assert answer["action"] == events.ACTION_RESUME
    assert cluster.observed == []


# -- regression runs -------------------------------------------------------


def test_a_regression_run_executes_the_named_modules_on_their_own(env, store, clock):
    wire(env)
    gates = Gates(business_day=False)
    cluster = FakeCluster()
    report = engine_for(store, cluster, gates, clock).handle(
        run_action(action=events.ACTION_REGRESSION, modules=["JOB3", "JOB1"])
    )["runs"][0]
    assert report["status"] == state.RUN_SUCCEEDED
    assert sorted(report["phases"]) == ["JOB1", "JOB3"]
    assert report["kind"] == events.REGRESSION
    assert gates.calendar_questions == [], "a regression run is not gated on the day"


def test_a_regression_run_is_asked_about_its_own_namespace(env, store, clock):
    wire(env)
    gates = Gates()
    report = engine_for(store, FakeCluster(), gates, clock).handle(
        run_action(action=events.ACTION_REGRESSION, modules=["JOB1"], runId="night-3")
    )["runs"][0]
    assert gates.input_questions[0] == {"jobName": "JOB1", "runId": "night-3"}
    saved = state.Checkpoints(store).load("night-3")
    assert saved.isolation == "runs/night-3/"
    assert saved.plan["gated"] is False
    assert saved.plan["concurrency"] == 3
    assert report["phases"]["JOB1"] == "succeeded"


def test_a_regression_pass_that_names_no_module_is_refused(env, store, clock):
    wire(env)
    subject = engine_for(store, FakeCluster(), Gates(), clock)
    with pytest.raises(ValueError) as refused:
        subject.handle(run_action(action=events.ACTION_REGRESSION))
    assert "has to name the modules" in str(refused.value)


def test_a_regression_pass_that_names_a_module_nothing_answers_to_is_refused(env, store, clock):
    wire(env)
    subject = engine_for(store, FakeCluster(), Gates(), clock)
    with pytest.raises(ValueError) as refused:
        subject.handle(run_action(action=events.ACTION_REGRESSION, modules=["JOB7"], runId="night-1"))
    assert "JOB7" in str(refused.value)


def test_a_regression_pass_may_not_choose_the_prefix_it_writes_into(env, store, clock):
    wire(env)
    subject = engine_for(store, FakeCluster(), Gates(), clock)
    with pytest.raises(ValueError) as refused:
        subject.handle(run_action(action=events.ACTION_REGRESSION, modules=["JOB1"],
                                  runId="night-1", isolation="production/"))
    assert "runs/night-1/" in str(refused.value)


def test_the_steps_of_a_run_are_rebuilt_from_its_checkpoint(env, store, clock):
    wire(env)
    run = state.RunState("night-9", "regression", today(), isolation="runs/night-9/")
    run.plan = {"steps": ["JOB2A"], "gated": False, "concurrency": 2}
    run.begin("JOB2A")
    state.Checkpoints(store).save(run)
    subject = engine_for(store, FakeCluster(), Gates(), clock)
    rebuilt = subject._plan_of(subject.checkpoints.load("night-9"))
    assert rebuilt.identifiers == ["JOB2A"]
    assert rebuilt.gated is False
    assert rebuilt.concurrency == 2


def test_a_checkpoint_written_by_another_build_is_read_without_its_names(env, store, clock):
    run = state.RunState("night-9", "scheduled", today())
    run.plan = {"steps": ["JOB1", "NOT-ANY-MORE"], "gated": True}
    state.Checkpoints(store).save(run)
    rebuilt = engine_for(store, FakeCluster(), Gates(), clock)._plan_of(
        state.Checkpoints(store).load("night-9")
    )
    assert rebuilt.identifiers == ["JOB1"]


def test_a_run_whose_plan_was_never_written_still_walks_the_chain(env, store, clock):
    run = state.RunState("night-9", "scheduled", today())
    state.Checkpoints(store).save(run)
    rebuilt = engine_for(store, FakeCluster(), Gates(), clock)._plan_of(
        state.Checkpoints(store).load("night-9")
    )
    assert rebuilt.identifiers == [step.identifier for step in plan.CHAIN]


# -- what a pass answers and publishes -------------------------------------


def test_a_finished_run_reports_its_counters_once(env, store, clock):
    wire(env)
    subject = engine_for(store, FakeCluster(), Gates(), clock)
    report = subject.handle(run_action())["runs"][0]
    assert len(subject.reports) == 1
    assert subject.reports[0]["batch_run_success"] == 1
    assert subject.reports[0]["batch_run_duration"] >= 0
    assert report["counters"] == {"message_id": "reported"}
    assert report["notification"] is None


def test_a_failed_run_announces_the_verdict_it_ended_with(env, store, clock):
    wire(env)
    cluster = FakeCluster({"JOB1": {"phase": "failed", "reason": "Error", "exitCode": 1}})
    subject = engine_for(store, cluster, Gates(), clock)
    report = subject.handle(run_action())["runs"][0]
    topic, subject_line, body = subject.notices[0]
    assert topic == "urn:smn:cn-north-4:project:batch"
    assert subject_line.startswith("test-orchestrator:")
    assert body["errorType"] == "BATCH_JOB_FAILED"
    assert body["phases"]["JOB3"] == "skipped"
    assert body["workflow"] == "test-orchestrator"
    assert report["notification"] == {"published": topic}
    assert subject.reports[0]["batch_run_failed"] == 1


def test_a_notice_the_service_refused_is_carried_back_not_absorbed(env, store, clock):
    wire(env)
    cluster = FakeCluster({"JOB1": {"phase": "failed", "reason": "Error", "exitCode": 1}})

    def refusing(topic, subject_line, message):
        raise http.ApiError("the topic refused", status=400)

    subject = engine_for(store, cluster, Gates(), clock, notifier=refusing)
    report = subject.handle(run_action())["runs"][0]
    assert report["status"] == state.RUN_FAILED
    assert "the topic refused" in report["notification"]["rejected"]


def test_a_run_with_no_notification_channel_says_so_in_its_answer(env, store, clock):
    env.setenv(config.CALENDAR_CHECK_URN, CALENDAR_URN)
    env.setenv(config.FILE_CHECK_URN, FILE_URN)
    cluster = FakeCluster({"JOB1": {"phase": "failed", "reason": "Error", "exitCode": 1}})
    subject = engine_for(store, cluster, Gates(), clock)
    report = subject.handle(run_action())["runs"][0]
    assert report["notification"] == {"skipped": "no notification topic configured"}


def test_the_counters_of_a_deployment_that_does_not_publish_them_are_left_out(env, store, clock):
    wire(env)
    env.setenv(config.ENABLE_CUSTOM_METRICS, "false")
    subject = engine_for(store, FakeCluster(), Gates(), clock)
    report = subject.handle(run_action())["runs"][0]
    assert subject.reports == []
    assert report["counters"] == {"skipped": "custom counters are turned off"}


def test_a_refused_report_does_not_change_the_verdict_of_the_run(env, store, clock):
    wire(env)

    # A monitoring refusal is turned into a carried-back note by the service
    # layer itself, so it never reaches the flow as a failure.
    def refused(points):
        return {"rejected": "monitoring refused / http 400"}

    subject = engine_for(store, FakeCluster(), Gates(), clock, reporter=refused)
    report = subject.handle(run_action())["runs"][0]
    assert report["status"] == state.RUN_SUCCEEDED
    assert "monitoring refused" in report["counters"]["rejected"]


def test_the_answer_of_a_pass_states_what_it_was_asked_and_what_it_spent(env, store, clock):
    wire(env)
    answer = engine_for(store, FakeCluster(), Gates(), clock, seconds=45).handle(run_action())
    assert answer["kind"] == events.SCHEDULED
    assert answer["passBudgetSeconds"] == 45.0
    assert answer["budgetSpent"] is False
    assert set(answer["runs"][0]) == {
        "runId", "kind", "runDate", "status", "errorType", "message", "phases", "unfinished",
        "checkpoint", "notification", "counters",
    }


# -- the container a step runs in ------------------------------------------


def test_the_container_a_step_runs_in_describes_the_same_pod_as_the_template(env, store):
    wire(env)
    env.setenv(config.JOB_DEADLINE_SECONDS, "1800")
    run = state.RunState("night-7", "scheduled", today(), isolation="runs/night-7/")
    body = index.Engine(CREDS, store=store).job_body(run, "JOB2A", "job-job2a-night-7")
    assert body["apiVersion"] == "batch/v1"
    assert body["metadata"]["namespace"] == "batch-jobs"
    assert body["metadata"]["labels"]["batch.job/run-id"] == "night-7"
    assert body["metadata"]["labels"]["batch.job/task"] == "JOB2A"
    assert body["spec"]["backoffLimit"] == 0
    assert body["spec"]["activeDeadlineSeconds"] == 1800
    assert body["spec"]["ttlSecondsAfterFinished"] == 3600
    template = body["spec"]["template"]["spec"]
    assert template["restartPolicy"] == "Never"
    assert template["serviceAccountName"] == "batch-job-runner"
    assert template["automountServiceAccountToken"] is False
    container = template["containers"][0]
    assert container["args"] == ["JOB2A"]
    assert container["image"] == "swr.example.com/batch/stub:1"
    assert {"name": "OBS_PREFIX", "value": "runs/night-7/"} in container["env"]
    assert {"name": "RUN_ID", "value": "night-7"} in container["env"]
    assert container["securityContext"]["runAsNonRoot"] is True
    assert body["spec"]["template"]["metadata"]["labels"]["batch.job/task"] == "JOB2A"


def test_a_run_starts_its_job_under_the_name_the_notice_can_be_read_back_with(env, store, clock):
    wire(env)
    cluster = FakeCluster()
    report = engine_for(store, cluster, Gates(), clock).handle(run_action())["runs"][0]
    created = cluster.created[0]["metadata"]["name"]
    assert created == "job-job1-%s" % report["runId"]
    assert created == cluster.observed[0][2]


def test_the_engine_answers_for_itself_when_no_lake_was_handed_in(env, monkeypatch):
    wired = wire(env)
    wired.setenv(config.K8S_API_SERVER, "https://api.internal:5473")
    wired.setenv(config.K8S_SA_TOKEN, "token")
    connected = {}

    def connecting(creds, bucket=None, endpoint=None):
        connected["creds"] = creds
        return make_store(None)

    monkeypatch.setattr(index.obs_store, "connect", connecting)
    subject = index.Engine(CREDS)
    assert subject.store is not None
    assert connected["creds"] == CREDS
    assert isinstance(subject.cluster, index.Cluster)
    assert subject.cluster.server == "https://api.internal:5473"

"""Every shape a pass can arrive in, read into one request."""

import json
from datetime import datetime, timezone

from batchcore.orchestrator import events


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Context:
    """The execution context the runtime hands a handler."""

    def __init__(self, log_blob=None):
        self._log = log_blob

    def getSecurityAccessKey(self):
        return "AK"

    def getSecuritySecretKey(self):
        return "SK"

    def getSecurityToken(self):
        return "TOKEN"

    def getProjectId(self):
        return "project-from-context"


def test_an_unreadable_event_is_a_resume_pass_not_an_accidental_run():
    for event in ({}, [], "not json at all", b"", None, 7):
        request = events.parse(event)
        assert request.action == events.ACTION_RESUME, event
        assert request.kind == events.SCHEDULED


def test_a_scheduled_launch_opens_todays_run():
    request = events.parse({"action": "start_run"})
    assert request.action == events.ACTION_START
    assert request.kind == events.SCHEDULED
    assert request.run_date == today()
    assert request.run_id is None


def test_a_replay_names_the_day_and_becomes_a_manual_run():
    request = events.parse({"action": "start_run", "runDate": "2026-11-14"})
    assert request.kind == events.MANUAL
    assert request.run_date == "2026-11-14"


def test_a_malformed_date_is_not_read_as_a_run_at_all():
    request = events.parse({"action": "start_run", "runDate": "14/11/2026"})
    assert request.kind == events.SCHEDULED
    assert request.run_date == today()


def test_a_run_identifier_makes_the_pass_manual():
    request = events.parse({"runId": "night-7"})
    assert request.action == events.ACTION_START
    assert request.kind == events.MANUAL
    assert request.run_id == "night-7"


def test_an_unknown_action_resumes_the_in_flight_runs():
    request = events.parse({"action": "do_everything"})
    assert request.action == events.ACTION_RESUME


def test_a_resume_pass_asks_for_nothing_but_the_runs_in_flight():
    request = events.parse({"action": "resume_runs"})
    assert request.action == events.ACTION_RESUME
    assert request.steps == ()


def test_a_container_notice_is_read_for_its_run_and_nothing_else():
    notice = {
        "kind": "Job",
        "metadata": {"name": "job-job1-run-1", "labels": {"batch.job/run-id": "run-1"}},
        "status": {"succeeded": 1},
    }
    request = events.parse(notice)
    assert request.action == events.ACTION_JOB_EVENT
    assert request.run_id == "run-1"


def test_a_notice_about_something_else_is_not_a_run():
    request = events.parse({"kind": "Pod", "metadata": {"labels": {"batch.job/run-id": "r"}}})
    assert request.action == events.ACTION_RESUME


def test_a_schedule_wrapper_is_followed_once():
    payload = events.parse(
        {"additional_information": json.dumps({"action": "start_run", "runDate": "2026-10-01"})}
    )
    assert payload.action == events.ACTION_START
    assert payload.run_date == "2026-10-01"


def test_a_nested_action_object_is_flattened():
    payload = events.parse({"action": {"action": "resume_runs"}})
    assert payload.action == events.ACTION_RESUME


def test_a_document_in_a_string_reaches_the_same_reader():
    request = events.parse(json.dumps({"action": "start_run", "runId": "manual-1"}))
    assert request.run_id == "manual-1"
    assert request.kind == events.MANUAL


def test_bytes_are_decoded_before_they_are_read():
    request = events.parse(b'{"action": "resume_runs"}')
    assert request.action == events.ACTION_RESUME


def test_a_log_notice_carries_the_object_inside_a_record_list():
    inner = {"kind": "Job", "metadata": {"labels": {"batch.job/run-id": "from-log"}}}
    request = events.parse({"Log": json.dumps([{"content": json.dumps(inner)}])}, Context())
    assert request.action == events.ACTION_JOB_EVENT
    assert request.run_id == "from-log"


def test_a_log_notice_that_is_not_a_list_is_harmless():
    request = events.parse({"Log": "not json"}, Context())
    assert request.action == events.ACTION_RESUME


def test_a_regression_run_names_its_modules_in_any_shape():
    for payload in (
        {"modules": ["JOB1", "job2a"]},
        {"modules": "JOB1, job2a"},
        {"steps": ["JOB1", "JOB2A"]},
        {"action": "run_regression", "modules": ["JOB1", "JOB2A"]},
    ):
        request = events.parse(payload)
        assert request.action == events.ACTION_REGRESSION, payload
        assert request.steps == ("JOB1", "JOB2A"), payload
        assert request.kind == events.REGRESSION


def test_a_regression_run_is_always_isolated_from_the_production_prefixes():
    request = events.parse({"modules": ["JOB1"]})
    assert request.run_id, "a regression run without an identifier shares the live data"
    assert request.isolation == "runs/%s/" % request.run_id


def test_a_named_regression_run_writes_under_its_own_identifier():
    request = events.parse({"action": "run_regression", "modules": ["JOB1"], "runId": "night-3"})
    assert request.action == events.ACTION_REGRESSION
    assert request.isolation == "runs/night-3/"


def test_a_run_identifier_alone_never_opens_a_regression_run():
    # A caller that names a run without naming modules is read as a replay of
    # that run, so an isolated run always states both.
    request = events.parse({"modules": ["JOB1"], "runId": "night-3"})
    assert request.action == events.ACTION_START


def test_an_isolation_a_caller_insists_on_is_kept():
    request = events.parse({"modules": ["JOB1"], "isolation": "sandbox/"})
    assert request.isolation == "sandbox/"


def test_a_module_name_that_cannot_be_a_label_is_dropped():
    request = events.parse({"modules": ["JOB1", "way too long" * 10, "with space"]})
    assert request.steps == ("JOB1",)


def test_a_budget_can_be_asked_for_by_the_caller():
    assert events.parse({"modules": ["JOB1"], "budgetSeconds": 120}).budget == 120
    assert events.parse({"modules": ["JOB1"], "stageBudgetSeconds": "60"}).budget == 60
    assert events.parse({"modules": ["JOB1"], "budgetSeconds": "soon"}).budget is None
    assert events.parse({"modules": ["JOB1"]}).budget is None


def test_the_run_identifier_of_a_regression_run_is_legal_in_a_resource_name():
    request = events.parse({"modules": ["JOB1"]})
    assert request.run_id.replace("-", "").replace(" ", "").isalnum()


def test_a_request_carries_the_payload_it_was_read_from():
    payload = {"action": "start_run", "runDate": "2026-10-01"}
    assert events.parse(payload).raw == payload


def test_the_answer_of_a_request_says_what_it_will_do():
    assert "run_regression" in repr(events.parse({"modules": ["JOB1"]}))

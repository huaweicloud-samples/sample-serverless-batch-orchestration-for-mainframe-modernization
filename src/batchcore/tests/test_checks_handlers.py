"""The two validation entry points: what they read, what they refuse, what they answer."""

import json

import pytest

from conftest import days_ago, http_date, make_store
from batchcore.checks import file_check
from batchcore.checks import index as checks_index
from batchcore.shared import config, obs_store

KEY = "inbound/transactions/txns.20261026.csv"


class Runtime:
    """The execution context the platform hands a handler."""

    def __init__(self, triple=("AK", "SK", "TOKEN"), project="project-from-context"):
        self.triple = triple
        self.project = project

    def getSecurityAccessKey(self):
        return self.triple[0] if self.triple else None

    def getSecuritySecretKey(self):
        return self.triple[1] if self.triple else None

    def getSecurityToken(self):
        return self.triple[2] if self.triple else None

    def getProjectId(self):
        return self.project


def fresh_lake(backend):
    backend.put_object(KEY, "a,b")
    store = make_store(backend)
    store.backend.times[KEY] = http_date(days_ago(0))
    return store


# -- reading the request ---------------------------------------------------


def test_a_request_written_straight_is_the_request(env):
    assert checks_index.read_event({"jobName": "JOB1"}, "here") == {"jobName": "JOB1"}


def test_a_request_in_a_string_is_read_as_one(env):
    assert checks_index.read_event('{"jobName": "JOB1"}', "here") == {"jobName": "JOB1"}


def test_a_request_in_bytes_is_read_as_one(env):
    assert checks_index.read_event(b'{"runDate": "2026-11-14"}', "here") == {
        "runDate": "2026-11-14"
    }


def test_one_level_of_wrapping_is_followed(env):
    for wrapper in checks_index.WRAPPERS:
        event = {wrapper: {"jobName": "JOB1"}}
        assert checks_index.read_event(event, "here")["jobName"] == "JOB1", wrapper


def test_a_wrapped_document_in_a_string_is_merged_with_its_wrapper(env):
    event = {"data": json.dumps({"jobName": "JOB1"}), "other": "kept"}
    request = checks_index.read_event(event, "here")
    assert request["jobName"] == "JOB1"
    assert request["other"] == "kept"


def test_a_wrapper_holding_a_non_object_is_ignored(env):
    assert checks_index.read_event({"data": "Daily batch launch", "jobName": "JOB3"}, "here") == {
        "data": "Daily batch launch",
        "jobName": "JOB3",
    }


@pytest.mark.parametrize("event", ["plain text", b"plain text", [1, 2], 7, None])
def test_an_event_that_is_not_a_request_is_refused_by_name(env, event):
    with pytest.raises(ValueError) as refused:
        checks_index.read_event(event, "handler_file_check")
    assert "handler_file_check" in str(refused.value)


# -- the lake the answer is looked up in -----------------------------------


def test_a_function_with_no_bucket_named_cannot_answer(env):
    env.delenv(config.OBS_BUCKET)
    with pytest.raises(ValueError) as refused:
        checks_index.lake(Runtime(), "handler_file_check")
    assert "no data bucket" in str(refused.value)


def test_a_file_check_without_an_identity_says_so(env):
    with pytest.raises(RuntimeError) as refused:
        checks_index.lake(None, "handler_file_check")
    assert obs_store.config_error() in str(refused.value)


def test_a_calendar_check_answers_without_an_identity(env):
    assert checks_index.lake(None, "handler_calendar_check", required=False) is None


def test_the_identity_of_the_runtime_is_the_one_used_to_read_the_lake(env, monkeypatch):
    seen = {}

    def connecting(creds, bucket=None, endpoint=None):
        seen["creds"] = creds
        return "the store"

    monkeypatch.setattr(obs_store, "connect", connecting)
    assert checks_index.lake(Runtime(), "handler_file_check") == "the store"
    assert seen["creds"] == ("AK", "SK", "TOKEN")


# -- the handlers ----------------------------------------------------------


def test_the_file_check_answers_for_the_step_it_was_named(env, backend, monkeypatch):
    store = fresh_lake(backend)
    monkeypatch.setattr(obs_store, "connect", lambda creds, bucket=None, endpoint=None: store)
    answer = checks_index.handler_file_check({"jobName": "JOB1"}, Runtime())
    assert answer["allFilesPresent"] is True
    assert answer["step"] == "JOB1"
    assert answer["errorType"] is None


def test_the_file_check_reports_an_absent_extract(env, backend, monkeypatch):
    store = make_store(backend)
    monkeypatch.setattr(obs_store, "connect", lambda creds, bucket=None, endpoint=None: store)
    answer = checks_index.handler_file_check({"jobName": "JOB1"}, Runtime())
    assert answer["errorType"] == file_check.MISSING


def test_the_file_check_reads_a_request_out_of_a_schedule_wrapper(env, backend, monkeypatch):
    store = fresh_lake(backend)
    monkeypatch.setattr(obs_store, "connect", lambda creds, bucket=None, endpoint=None: store)
    answer = checks_index.handler_file_check({"data": '{"jobName": "JOB1"}'}, Runtime())
    assert answer["allFilesPresent"] is True


def test_the_calendar_check_needs_no_lake_for_the_weekend_rule(env):
    answer = checks_index.handler_calendar_check({"runDate": "2026-11-14"}, None)
    assert answer["isBusinessDay"] is False
    assert answer["reason"] == "weekend"


def test_the_calendar_check_uses_the_lake_when_it_can_reach_one(env, backend, monkeypatch):
    backend.put_object(config.calendar_object(), json.dumps(["2026-11-11"]))
    store = make_store(backend)
    monkeypatch.setattr(obs_store, "connect", lambda creds, bucket=None, endpoint=None: store)
    answer = checks_index.handler_calendar_check({"runDate": "2026-11-11"}, Runtime())
    assert answer["isBusinessDay"] is False
    assert answer["reason"] == "non-processing date"


def test_an_empty_request_answers_for_this_instant(env):
    answer = checks_index.handler_calendar_check({}, None)
    assert set(answer) >= {"isBusinessDay", "date", "reason", "timezone"}

"""The signed transport and the three regional services the flow calls.

A transport stand-in replaces the socket in every case, so the assertions are
on the request that would have gone out: the host, the path, the body, and the
error the caller gets back when a service refuses.
"""

import io
import json
import urllib.error

import pytest

from batchcore.shared import config
from batchcore.shared.hwc import http, services

HOST = "obs.cn-north-4.myhuaweicloud.com"
CREDS = ("AKTEST", "unit-test-secret-key", None)


def fake_transport(reply=None, error=None):
    calls = []

    def transport(method, host, path, query, body, creds, timeout):
        calls.append(
            {
                "method": method,
                "host": host,
                "path": path,
                "query": query,
                "body": body,
                "creds": creds,
                "timeout": timeout,
            }
        )
        if error is not None:
            raise error
        return reply

    transport.calls = calls
    return transport


class Response:
    """The piece of an HTTP answer the transport reads."""

    def __init__(self, payload, status=200, headers=None):
        self.payload = payload
        self.status = status
        self.headers = headers if headers is not None else {}

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


# -- the transport ---------------------------------------------------------


def test_a_call_carries_the_method_the_path_and_the_identity(env):
    transport = fake_transport({"ok": True})
    answer = http.request("GET", HOST, "/v2/x", [("a", "1")], None, CREDS, transport, 5)
    assert answer == {"ok": True}
    call = transport.calls[0]
    assert call["method"] == "GET"
    assert call["host"] == HOST
    assert call["path"] == "/v2/x"
    assert call["query"] == (("a", "1"),)
    assert call["creds"] == CREDS
    assert call["timeout"] == 5


def test_the_default_timeout_is_the_one_the_layer_documents(env):
    transport = fake_transport({})
    http.request("GET", HOST, "/x", (), None, CREDS, transport)
    assert transport.calls[0]["timeout"] == http.DEFAULT_TIMEOUT_SECONDS


def test_an_answer_is_read_as_json_when_it_is_json_and_as_text_when_it_is_not():
    assert http.decode(b'{"a": 1}', {}) == {"a": 1}
    assert http.decode(b"created", {}) == "created"
    assert http.decode(b"", {}) == {}
    assert http.decode(None, {}) == {}


def test_a_call_without_an_identity_never_leaves_the_process(env):
    with pytest.raises(http.ApiError) as refused:
        http.perform("GET", HOST, "/x", (), None, None, 5)
    assert "no credentials" in str(refused.value)


def test_a_signed_request_is_sent_over_tls_with_the_authorization_header(env, monkeypatch):
    seen = {}

    def open_the_call(call, timeout=None, context=None):
        seen["url"] = call.full_url
        seen["method"] = call.get_method()
        seen["headers"] = dict(call.header_items())
        seen["data"] = call.data
        return Response(json.dumps({"result": "value"}).encode())

    monkeypatch.setattr(http.urllib.request, "urlopen", open_the_call)
    answer = http.perform(
        "POST", "ces.cn-north-4.myhuaweicloud.com", "/V1.0/p/metric-data", (), {"a": 1}, CREDS, 7
    )
    assert answer == {"result": "value"}
    assert seen["url"].startswith("https://ces.cn-north-4.myhuaweicloud.com/V1.0/p/metric-data")
    assert seen["method"] == "POST"
    assert seen["data"] == b'{"a": 1}'
    authorization = seen["headers"]["Authorization"]
    assert authorization.startswith("SDK-HMAC-SHA256 Access=AKTEST,")
    assert "x-sdk-date" in authorization and "host" in authorization


def test_a_query_is_written_into_the_url_the_transport_builds(env, monkeypatch):
    seen = {}

    def open_the_call(call, timeout=None, context=None):
        seen["url"] = call.full_url
        return Response(b"{}")

    monkeypatch.setattr(http.urllib.request, "urlopen", open_the_call)
    http.perform("GET", HOST, "/b", [("prefix", "a b"), ("maxkeys", "100")], None, CREDS, 5)
    assert "?maxkeys=100&prefix=a%20b" in seen["url"]


def test_a_refusal_is_carried_back_with_the_code_the_service_gave(env, monkeypatch):
    def open_the_call(call, timeout=None, context=None):
        raise urllib.error.HTTPError(
            call.full_url,
            400,
            "Bad Request",
            {"x-request-id": "req-1"},
            io.BytesIO(json.dumps({"error_code": "FGS.0001", "error_msg": "no"}).encode()),
        )

    monkeypatch.setattr(http.urllib.request, "urlopen", open_the_call)
    with pytest.raises(http.ApiError) as refused:
        http.perform("POST", HOST, "/x", (), {}, CREDS, 5)
    error = refused.value
    assert error.status == 400
    assert error.code == "FGS.0001"
    assert error.message == "no"
    assert "request id req-1" in str(error)


def test_a_refusal_without_a_json_body_keeps_the_plain_text(env, monkeypatch):
    def open_the_call(call, timeout=None, context=None):
        raise urllib.error.HTTPError(
            call.full_url, 503, "Unavailable", {}, io.BytesIO(b"draining")
        )

    monkeypatch.setattr(http.urllib.request, "urlopen", open_the_call)
    with pytest.raises(http.ApiError) as refused:
        http.perform("GET", HOST, "/x", (), None, CREDS, 5)
    assert refused.value.message == "draining"
    assert refused.value.status == 503


def test_a_call_that_never_got_there_is_retryable(env, monkeypatch):
    def open_the_call(call, timeout=None, context=None):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(http.urllib.request, "urlopen", open_the_call)
    with pytest.raises(http.ApiError) as refused:
        http.perform("GET", HOST, "/x", (), None, CREDS, 5)
    assert refused.value.status is None
    assert http.retryable(refused.value) is True


@pytest.mark.parametrize(
    "status,retryable",
    [(None, True), (429, True), (500, True), (502, True), (503, True), (504, True),
     (400, False), (403, False), (404, False)],
)
def test_which_refusals_are_worth_another_attempt(status, retryable):
    assert http.retryable(http.ApiError("x", status=status)) is retryable


def test_the_error_text_names_everything_the_service_said():
    error = http.ApiError("refused", status=400, code="FGS.1", request_id="r")
    assert str(error) == "refused / http 400 / code FGS.1 / request id r"
    assert str(http.ApiError("only a message")) == "only a message"


# -- the retry policy ------------------------------------------------------


def test_a_transient_refusal_is_tried_again(env):
    attempts = []
    sleeps = []

    class FirstTryRefused:
        def uniform(self, low, high):
            return 0

    def action():
        attempts.append(1)
        if len(attempts) < 2:
            raise http.ApiError("gateway", status=502)
        return "answered"

    assert http.with_retries(action, 2, 10, sleep=sleeps.append, chooser=FirstTryRefused()) == "answered"
    assert len(attempts) == 2
    assert sleeps == [0]


class FixedDraw:
    def uniform(self, low, high):
        return high


def test_the_wait_is_drawn_between_zero_and_the_interval(env):
    sleeps = []

    def action():
        raise http.ApiError("gateway", status=503)

    with pytest.raises(http.ApiError):
        http.with_retries(action, 3, 10, sleep=sleeps.append, chooser=FixedDraw())
    assert sleeps == [10, 10]


def test_a_refusal_the_data_is_responsible_for_is_not_tried_again(env):
    calls = []

    def action():
        calls.append(1)
        raise http.ApiError("malformed", status=400)

    with pytest.raises(http.ApiError):
        http.with_retries(action, 3, 0)
    assert len(calls) == 1


def test_the_last_refusal_is_the_one_reported(env):
    def action():
        raise http.ApiError("third time", status=500)

    with pytest.raises(http.ApiError) as refused:
        http.with_retries(action, 2, 0, sleep=lambda seconds: None)
    assert "third time" in str(refused.value)


def test_a_policy_of_one_attempt_is_still_one_attempt(env):
    calls = []

    def action():
        calls.append(1)
        raise http.ApiError("nope", status=500)

    with pytest.raises(http.ApiError):
        http.with_retries(action, 0, 0)
    assert len(calls) == 1


def test_a_call_that_succeeds_first_time_waits_for_nothing(env):
    sleeps = []
    assert http.with_retries(lambda: "ok", 3, 10, sleep=sleeps.append) == "ok"
    assert sleeps == []


# -- the three services ----------------------------------------------------


def test_a_function_is_invoked_by_uniform_resource_name(env):
    transport = fake_transport({"data": {"allFilesPresent": True}})
    answer = services.invoke(
        "urn:fgs:cn-north-4:x:functions:batch-checks", {"jobName": "JOB1"}, CREDS, transport=transport
    )
    call = transport.calls[0]
    assert call["host"] == "functiongraph.cn-north-4.myhuaweicloud.com"
    assert call["path"] == "/v2/project-for-tests/fgs/functions/urn%3Afgs%3Acn-north-4%3Ax%3Afunctions%3Abatch-checks/invocations"
    assert call["body"] == {"data": {"jobName": "JOB1"}}
    assert answer == {"allFilesPresent": True}


def test_an_answer_wrapped_in_a_string_is_read_once(env):
    transport = fake_transport({"data": '{"present": false}'})
    assert services.invoke("urn", {}, CREDS, transport=transport) == {"present": False}


def test_a_function_that_raised_is_reported_as_an_error(env):
    transport = fake_transport(
        {"data": {"errorMagnitude": 1, "errorMessage": "boom", "errorType": "ValueError"}}
    )
    with pytest.raises(http.ApiError) as refused:
        services.invoke("urn", {}, CREDS, transport=transport)
    assert "boom" in str(refused.value)
    assert refused.value.code == "ValueError"


def test_an_invocation_the_service_refused_is_reported(env):
    transport = fake_transport({"num_failed_task": 1, "errors": ["no such function"]})
    with pytest.raises(http.ApiError) as refused:
        services.invoke("urn", {}, CREDS, transport=transport)
    assert "no such function" in str(refused.value)
    assert refused.value.status == 1


def test_the_host_of_a_service_is_derived_from_the_named_endpoint(env):
    env.setenv(config.OBS_ENDPOINT, "obs.cn-south-1.myhuaweicloud.com")
    assert config.service_host("smn") == "smn.cn-south-1.myhuaweicloud.com"
    assert config.service_host("ces") == "ces.cn-south-1.myhuaweicloud.com"


def test_without_a_named_endpoint_the_public_suffix_is_used(env):
    env.delenv(config.OBS_ENDPOINT)
    assert config.service_host("smn") == "smn.cn-north-4.myhuaweicloud.com"


def test_a_notice_is_published_on_the_topic_the_alarms_use(env):
    transport = fake_transport({"message_id": "m-1"})
    answer = services.notify(
        "urn:smn:x", "run failed", {"runId": "r-1"}, CREDS, transport=transport
    )
    call = transport.calls[0]
    assert call["host"] == "smn.cn-north-4.myhuaweicloud.com"
    assert "/notifications/topics/urn%3Asmn%3Ax/publish" in call["path"]
    assert json.loads(call["body"]["message"]) == {"runId": "r-1"}
    assert call["body"]["subject"] == "run failed"
    assert answer == {"message_id": "m-1"}


def test_a_deployment_without_a_notification_channel_skips_the_call(env):
    transport = fake_transport({})
    assert services.notify("", "subject", "message", CREDS, transport=transport) == {}
    assert transport.calls == []


def test_a_long_subject_is_cut_to_what_the_service_accepts(env):
    transport = fake_transport({})
    services.notify("urn", "x" * 400, "message", CREDS, transport=transport)
    assert len(transport.calls[0]["body"]["subject"]) == 255


def test_the_run_counters_are_reported_against_the_environment_of_the_run(env):
    transport = fake_transport({})
    services.report(
        {"batch_run_duration": 12.5, "batch_run_success": 1},
        CREDS,
        transport=transport,
        namespace="TEST_ORCHESTRATOR",
    )
    call = transport.calls[0]
    assert call["host"] == "ces.cn-north-4.myhuaweicloud.com"
    assert call["path"] == "/V1.0/project-for-tests/metric-data"
    body = call["body"]
    assert sorted(item["metric"]["metric_name"] for item in body) == [
        "batch_run_duration",
        "batch_run_success",
    ]
    duration = [item for item in body if item["metric"]["metric_name"] == "batch_run_duration"][0]
    assert duration["unit"] == "s"
    assert duration["type"] == "float"
    assert duration["ttl"] == services.METRIC_TTL_SECONDS
    counter = [item for item in body if item["metric"]["metric_name"] == "batch_run_success"][0]
    assert counter["unit"] == "Count"
    assert counter["type"] == "int"
    assert counter["metric"]["dimensions"] == [{"name": "run_environment", "value": "dev"}]
    assert counter["collect_time"] % 1000 == 0


def test_monitoring_is_a_side_effect_a_refused_report_never_fails_a_run(env):
    transport = fake_transport(error=http.ApiError("ces refused", status=400))
    answer = services.report({"batch_run_success": 1}, CREDS, transport=transport, namespace="NS")
    assert "rejected" in answer


def test_counters_are_not_invented_when_no_namespace_is_named(env):
    env.delenv(config.METRIC_NAMESPACE)
    transport = fake_transport({})
    assert services.report({"a": 1}, CREDS, transport=transport) == {}
    assert transport.calls == []


def test_nothing_is_reported_for_a_run_with_no_counters(env):
    transport = fake_transport({})
    assert services.report({}, CREDS, transport=transport, namespace="NS") == {}


def test_a_service_call_needs_the_project_it_is_addressed_under(env):
    env.delenv(config.PROJECT_ID)
    transport = fake_transport({})
    with pytest.raises(http.ApiError):
        services.invoke("urn", {}, CREDS, transport=transport)
    # With the project supplied the call goes through. An invocation answers with
    # the target function's own return value, so the empty answer of this stand-in
    # is handed back unchanged rather than unwrapped into None.
    assert services.invoke("urn", {}, CREDS, project="named-project", transport=transport) == {}

"""The cluster calls one pass makes, driven against recorded answers.

The transport is handed in, so every assertion is on the request that would
have gone to the API server: the path, the selector, the bearer header, and
what the flow is told when the cluster refuses or the job is gone.
"""

import json
import ssl
import urllib.error

import pytest

from batchcore.orchestrator import index
from batchcore.orchestrator.index import Cluster, ClusterError
from batchcore.shared import config

SERVER = "https://api.batch-cluster.internal:5473"
TOKEN = "a-service-account-token"


class Answered:
    """A cluster that answers from a table keyed by method and path."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, method, url, headers, payload, query):
        path = url[len(SERVER):] if url.startswith(SERVER) else url
        self.calls.append(
            {
                "method": method,
                "path": path.split("?")[0],
                "url": url,
                "headers": dict(headers),
                "payload": json.loads(payload.decode()) if payload else None,
                "query": dict(query),
            }
        )
        answer = self.answers.get((method, path.split("?")[0]))
        if answer is None:
            raise ClusterError("%s %s is not in the recorded answers" % (method, path), status=404)
        if isinstance(answer, Exception):
            raise answer
        return answer


def cluster(answers, token=TOKEN, **kwargs):
    return Cluster(SERVER, "batch-jobs", token, transport=Answered(answers), **kwargs)


def job(status):
    return {"kind": "Job", "metadata": {"name": "job-job1-run-1"}, "status": status}


def pod(name="job-job1-run-1-abcde", exit_code=3, reason="Error", finished="2026-10-26T04:30:00Z",
        container="batch-job"):
    return {
        "metadata": {"name": name},
        "status": {
            "containerStatuses": [
                {
                    "name": container,
                    "state": {"terminated": {"exitCode": exit_code, "reason": reason,
                                             "finishedAt": finished}},
                }
            ]
        },
    }


# -- the four calls --------------------------------------------------------


def test_a_job_is_created_in_the_path_the_batch_application_programming_interface_serves(env):
    body = {"kind": "Job", "metadata": {"name": "job-job1-run-1"}}
    answered = Answered({("POST", "/apis/batch/v1/namespaces/batch-jobs/jobs"): body})
    subject = Cluster(SERVER, "batch-jobs", TOKEN, transport=answered)
    assert subject.create_job(body) == body
    assert answered.calls[0]["path"] == "/apis/batch/v1/namespaces/batch-jobs/jobs"


def test_the_request_carries_the_service_account_token(env):
    body = {"kind": "Job", "metadata": {"name": "job-job1-run-1"}}
    answered = Answered({("POST", "/apis/batch/v1/namespaces/batch-jobs/jobs"): body})
    subject = Cluster(SERVER, "batch-jobs", TOKEN, transport=answered)
    subject.create_job(body)
    call = answered.calls[0]
    assert call["headers"]["Authorization"] == "Bearer %s" % TOKEN
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["payload"] == body
    assert call["url"] == SERVER + "/apis/batch/v1/namespaces/batch-jobs/jobs"


def test_a_job_that_is_already_there_is_continued_not_failed(env):
    body = {"kind": "Job", "metadata": {"name": "job-job1-run-1"}}
    answered = Answered(
        {("POST", "/apis/batch/v1/namespaces/batch-jobs/jobs"):
         ClusterError("exists", status=409, reason="AlreadyExists")}
    )
    subject = Cluster(SERVER, "batch-jobs", TOKEN, transport=answered)
    assert subject.create_job(body) == body


def test_any_other_refusal_is_raised(env):
    answered = Answered(
        {("POST", "/apis/batch/v1/namespaces/batch-jobs/jobs"):
         ClusterError("forbidden", status=403, reason="Forbidden")}
    )
    subject = Cluster(SERVER, "batch-jobs", TOKEN, transport=answered)
    with pytest.raises(ClusterError) as refused:
        subject.create_job({"kind": "Job", "metadata": {"name": "n"}})
    assert refused.value.status == 403
    assert refused.value.code == "Forbidden"


def test_a_job_that_is_gone_reads_as_absent(env):
    answered = Answered({("GET", "/apis/batch/v1/namespaces/batch-jobs/jobs/job-1"):
                         ClusterError("gone", status=404)})
    subject = Cluster(SERVER, "batch-jobs", TOKEN, transport=answered)
    assert subject.job("job-1") is None
    assert subject.observe("run-1", "JOB1", "job-1") == {"phase": "absent"}


def test_the_pods_of_a_step_are_read_by_their_labels(env):
    answered = Answered(
        {("GET", "/api/v1/namespaces/batch-jobs/pods"): {"items": [pod()]}}
    )
    subject = Cluster(SERVER, "batch-jobs", TOKEN, transport=answered)
    items = subject.pods("run-1", "JOB1")
    assert len(items) == 1
    assert answered.calls[0]["query"]["labelSelector"] == "batch.job/run-id=run-1,batch.job/task=JOB1"


def test_a_namespace_named_for_the_deployment_is_used(env):
    answered = Answered({("POST", "/apis/batch/v1/namespaces/other/jobs"): {"kind": "Job"}})
    subject = Cluster(SERVER, "other", TOKEN, transport=answered)
    subject.create_job({"kind": "Job"})
    assert answered.calls[0]["path"] == "/apis/batch/v1/namespaces/other/jobs"


def test_a_call_without_a_token_is_refused_before_anything_is_sent(env):
    subject = Cluster(SERVER, "batch-jobs", "", transport=Answered({}))
    with pytest.raises(ClusterError) as refused:
        subject.job("job-1")
    assert "no token" in str(refused.value)


# -- what became of a step ------------------------------------------------


def test_a_container_that_exited_zero_is_a_success(env):
    answers = {
        ("GET", "/apis/batch/v1/namespaces/batch-jobs/jobs/job-1"): job({"succeeded": 1}),
    }
    assert cluster(answers).observe("run-1", "JOB1", "job-1") == {"phase": "succeeded",
                                                                 "exitCode": 0}


def test_a_container_still_up_is_still_running(env):
    answers = {("GET", "/apis/batch/v1/namespaces/batch-jobs/jobs/job-1"):
               job({"active": 1, "startTime": "2026-10-26T04:00:00Z"})}
    assert cluster(answers).observe("run-1", "JOB1", "job-1") == {"phase": "running"}


def test_a_failure_carries_the_exit_code_and_the_end_of_the_log(env):
    answers = {
        ("GET", "/apis/batch/v1/namespaces/batch-jobs/jobs/job-1"):
            job({"conditions": [{"type": "Failed", "reason": "BackoffLimitExceeded"}]}),
        ("GET", "/api/v1/namespaces/batch-jobs/pods"): {"items": [pod(exit_code=3)]},
        ("GET", "/api/v1/namespaces/batch-jobs/pods/job-job1-run-1-abcde/log"):
            "line one\nthe extract of day 3 has no fees at all",
    }
    verdict = cluster(answers).observe("run-1", "JOB1", "job-1")
    assert verdict["phase"] == "failed"
    assert verdict["reason"] == "BackoffLimitExceeded"
    assert verdict["exitCode"] == 3
    assert "no fees at all" in verdict["detail"]


def test_a_log_the_cluster_will_not_open_leaves_the_verdict_without_one(env):
    answers = {
        ("GET", "/apis/batch/v1/namespaces/batch-jobs/jobs/job-1"):
            job({"conditions": [{"type": "Failed", "reason": "Error"}]}),
        ("GET", "/api/v1/namespaces/batch-jobs/pods"): {"items": [pod()]},
        ("GET", "/api/v1/namespaces/batch-jobs/pods/job-job1-run-1-abcde/log"):
            ClusterError("logs are gone", status=400),
    }
    verdict = cluster(answers).observe("run-1", "JOB1", "job-1")
    assert verdict["exitCode"] == 3
    assert verdict["detail"] is None


def test_a_failed_job_whose_pod_never_reported_leaves_the_code_unknown(env):
    answers = {
        ("GET", "/apis/batch/v1/namespaces/batch-jobs/jobs/job-1"):
            job({"conditions": [{"type": "Failed", "reason": "Error"}]}),
        ("GET", "/api/v1/namespaces/batch-jobs/pods"): {"items": []},
    }
    verdict = cluster(answers).observe("run-1", "JOB1", "job-1")
    assert verdict["exitCode"] is None
    assert verdict["detail"] is None


def test_a_very_long_log_is_cut_to_the_end_of_it(env):
    answers = {
        ("GET", "/apis/batch/v1/namespaces/batch-jobs/jobs/job-1"):
            job({"conditions": [{"type": "Failed", "reason": "Error"}]}),
        ("GET", "/api/v1/namespaces/batch-jobs/pods"): {"items": [pod()]},
        ("GET", "/api/v1/namespaces/batch-jobs/pods/job-job1-run-1-abcde/log"): "x" * 9000,
    }
    verdict = cluster(answers).observe("run-1", "JOB1", "job-1")
    assert len(verdict["detail"]) == index.TAIL_CHARACTERS


def test_a_job_past_its_own_deadline_is_caught_even_if_the_cluster_says_nothing(env):
    started = "2026-10-26T04:00:00Z"
    answers = {("GET", "/apis/batch/v1/namespaces/batch-jobs/jobs/job-1"):
               job({"active": 1, "startTime": started})}
    later = 10 * 3600 + 60
    subject = Cluster(SERVER, "batch-jobs", TOKEN, transport=Answered(answers),
                      timer=lambda: _epoch("2026-10-26T04:00:00Z") + later)
    verdict = subject.observe("run-1", "JOB1", "job-1")
    assert verdict["phase"] == "failed"
    assert verdict["reason"] == "DeadlineExceeded"
    assert verdict["exitCode"] is None


def _epoch(stamp):
    from datetime import datetime

    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


# -- the small readers ----------------------------------------------------


def test_a_deadline_is_only_overdue_past_the_time_it_was_given(env):
    started = "2026-10-26T04:00:00Z"
    moment = _epoch(started)
    assert index.reason_is_overdue({}, moment) is False
    assert index.reason_is_overdue({"startTime": "nonsense"}, moment) is False
    assert index.reason_is_overdue({"startTime": started}, moment) is False
    assert index.reason_is_overdue({"startTime": started}, moment + 7400) is True
    env.setenv(config.JOB_DEADLINE_SECONDS, "60")
    assert index.reason_is_overdue({"startTime": started}, moment + 500) is True


def test_only_terminated_containers_are_read_and_the_newest_first():
    pod_body = {
        "status": {
            "containerStatuses": [
                {"name": "a", "state": {"running": {"startedAt": "2026-10-26T04:00:00Z"}}},
                {"name": "b", "state": {"terminated": {"exitCode": 1, "finishedAt": "2026-10-26T04:01:00Z"}}},
            ],
            "initContainerStatuses": [
                {"name": "c", "state": {"terminated": {"exitCode": 7, "finishedAt": "2026-10-26T04:05:00Z"}}},
            ],
        }
    }
    states = index._container_states(pod_body)
    assert [entry["name"] for entry in states] == ["c", "b"]
    assert states[0]["exitCode"] == 7
    assert index._container_states({}) == []
    assert index._container_states(None) == []


def test_a_bundle_written_on_one_line_is_put_back_into_shape():
    assert index._pem(None) is None
    assert index._pem("") is None
    assert index._pem('"-----BEGIN-----\\nabc\\n-----END-----\\n"') == (
        "-----BEGIN-----\nabc\n-----END-----\n"
    )


def test_the_trust_anchor_is_built_once_per_pass():
    subject = Cluster("https://api:5473", "batch-jobs", TOKEN, ca=None)
    context = subject.tls()
    assert isinstance(context, ssl.SSLContext)
    assert subject.tls() is context


def test_a_certificate_named_for_the_api_server_is_used_as_the_anchor(env):
    subject = Cluster("https://api:5473", "batch-jobs", TOKEN, ca="not a certificate")
    with pytest.raises(Exception):
        subject.tls()


def test_the_cluster_answers_what_the_flow_needs_when_a_socket_refused(monkeypatch):
    def open_the_call(call, timeout=None, context=None):
        raise urllib.error.HTTPError(
            call.full_url, 403, "Forbidden", {},
            _body(json.dumps({"reason": "Forbidden", "message": "no"})),
        )

    monkeypatch.setattr(index.urllib.request, "urlopen", open_the_call)
    subject = Cluster(SERVER, "batch-jobs", TOKEN)
    with pytest.raises(ClusterError) as refused:
        subject._call("GET", "/apis/batch/v1/namespaces/batch-jobs/jobs/x")
    assert refused.value.status == 403
    assert refused.value.code == "Forbidden"
    assert "no" in str(refused.value)


def _body(text):
    import io

    return io.BytesIO(text.encode())


def test_a_cluster_that_never_answered_is_reported_as_not_reached(monkeypatch):
    def open_the_call(call, timeout=None, context=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(index.urllib.request, "urlopen", open_the_call)
    subject = Cluster(SERVER, "batch-jobs", TOKEN)
    with pytest.raises(ClusterError) as refused:
        subject._call("GET", "/x")
    assert refused.value.status is None
    assert "did not reach the cluster" in str(refused.value)


def test_the_query_of_a_call_is_written_into_the_url(monkeypatch):
    seen = {}

    def open_the_call(call, timeout=None, context=None):
        seen["url"] = call.full_url
        return _responded(json.dumps({"items": []}))

    monkeypatch.setattr(index.urllib.request, "urlopen", open_the_call)
    subject = Cluster(SERVER, "batch-jobs", TOKEN)
    subject.pods("run 1", "JOB1")
    assert "labelSelector=batch.job%2Frun-id%3Drun+1" in seen["url"]


def _responded(text):
    class Holder:
        headers = {}

        def read(self):
            return text.encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    return Holder()


# -- binding to the deployment --------------------------------------------


def test_a_cluster_that_was_never_named_is_reported_with_the_names_missing(env):
    with pytest.raises(ClusterError) as refused:
        Cluster.from_environment()
    assert config.K8S_API_SERVER in str(refused.value)
    assert config.K8S_SA_TOKEN in str(refused.value)


def test_an_image_nothing_named_is_a_cluster_that_cannot_run_jobs(env):
    env.setenv(config.K8S_API_SERVER, SERVER)
    env.setenv(config.K8S_SA_TOKEN, TOKEN)
    env.delenv(config.BATCH_IMAGE)
    with pytest.raises(ClusterError) as refused:
        Cluster.from_environment()
    assert config.BATCH_IMAGE in str(refused.value)


def test_a_named_cluster_is_bound_to_the_batch_namespace(env):
    env.setenv(config.K8S_API_SERVER, SERVER)
    env.setenv(config.K8S_SA_TOKEN, TOKEN)
    env.setenv(config.BATCH_IMAGE, "swr.example.com/batch/stub:1")
    env.setenv(config.K8S_NAMESPACE, "other-namespace")
    subject = Cluster.from_environment()
    assert subject.server == SERVER
    assert subject.namespace == "other-namespace"
    assert subject.token == TOKEN

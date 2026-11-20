"""The three regional services the orchestration layer calls.

Each section is one short call with the path the service documents, signed by
the shared signing layer. Nothing here knows about a run: they take the
identities and the payload and return what the service answered, so a test can
replace the transport and assert on the request instead of the result.

* invocation of the validation functions
* publication on the notification topic
* reporting of the run counters as custom metrics
"""

import json
import time
from urllib.parse import quote

from .. import config, logs
from . import http

log = logs.get("services")

FUNCTIONS_PATH = "/v2/{project}/fgs/functions/{urn}/invocations"
TOPIC_PATH = "/v2/{project}/notifications/topics/{urn}/publish"
METRIC_DATA_PATH = "/V1.0/{project}/metric-data"

METRIC_RUN_DIMENSION = "run_environment"
METRIC_COUNT_UNIT = "Count"
METRIC_SECONDS_UNIT = "s"
METRIC_TTL_SECONDS = 7 * 24 * 3600


def _project(creds, project):
    value = project or config.project_id()
    if not value:
        raise http.ApiError("no project identifier: HW_PROJECT_ID is unset")
    return value


def invoke(urn, event, creds, project=None, transport=None, timeout=30):
    """Invoke another function of the deployment and return its result.

    The result of a function is the value it returned, so a rejected call and
    a function that raised are both reported here as an error rather than
    quietly turned into a failed validation.
    """
    target = _project(creds, project)
    path = FUNCTIONS_PATH.format(project=target, urn=quote(urn, safe=""))
    body = {"data": event}
    response = http.request(
        "POST",
        config.service_host("functiongraph"),
        path,
        body=body,
        creds=creds,
        transport=transport,
        timeout=timeout,
    )
    if isinstance(response, dict) and response.get("errors"):
        raise http.ApiError(str(response["errors"]), status=response.get("num_failed_task"))
    # A synchronous invocation answers with the function's own return value at
    # the top level; there is no "data" envelope to unwrap. Only the documented
    # failure shape carries "errorMessage"/"errorType" (and the size guard adds
    # "errorMagnitude"), so a plain object is the payload as-is.
    payload = response.get("data") if (isinstance(response, dict) and "data" in response) else response
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            pass
    if isinstance(payload, dict) and payload.get("errorMessage") is not None:
        raise http.ApiError(
            "invoked function failed: %s" % payload.get("errorMessage"),
            code=payload.get("errorType"),
        )
    return payload


def notify(topic_urn, subject, message, creds, project=None, transport=None):
    """Publish a notice on the topic the alarms also use.

    An empty topic is a supported configuration: the deployment can be
    stood up without a notification channel and then this call is skipped.
    """
    if not topic_urn:
        log.info("no notification topic configured, skipping")
        return {}
    body = {
        "message": message if isinstance(message, str) else _dump(message),
        "subject": subject[:255],
    }
    path = TOPIC_PATH.format(project=_project(creds, project), urn=quote(topic_urn, safe=""))
    return http.request(
        "POST",
        config.service_host("smn"),
        path,
        body=body,
        creds=creds,
        transport=transport,
    )


def report(points, creds, project=None, transport=None, namespace=None):
    """Report the run counters of one finished run.

    ``points`` maps a metric name to a numeric value. The dimension set is
    fixed by the alarms of the deployment, so every counter is reported
    against the environment it ran in and the alarm rules see the series they
    were written for.

    A rejected report never fails a run: monitoring is a side effect.
    """
    if not points:
        return {}
    space = namespace or config.text(config.METRIC_NAMESPACE)
    if not space:
        log.info("no metric namespace configured, skipping")
        return {}
    target = _project(creds, project)
    moment = int(_now_millis() // 1000) * 1000
    body = []
    for name, value in sorted(points.items()):
        is_count = not name.endswith("duration")
        body.append(
            {
                "metric": {
                    "namespace": space,
                    "metric_name": name,
                    "dimensions": [
                        {"name": METRIC_RUN_DIMENSION, "value": config.environment()}
                    ],
                },
                "ttl": METRIC_TTL_SECONDS,
                "collect_time": moment,
                "value": float(value),
                "unit": METRIC_COUNT_UNIT if is_count else METRIC_SECONDS_UNIT,
                "type": "int" if is_count else "float",
            }
        )
    try:
        return http.request(
            "POST",
            config.service_host("ces"),
            METRIC_DATA_PATH.format(project=target),
            body=body,
            creds=creds,
            transport=transport,
        )
    except http.ApiError as error:
        log.warning("metric report refused: %s", error)
        return {"rejected": str(error)}


def _dump(value):
    return json.dumps(value, ensure_ascii=False)


def _now_millis():
    return time.time() * 1000

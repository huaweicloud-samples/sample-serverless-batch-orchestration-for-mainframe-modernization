"""Configuration read from the function environment.

Every value the functions need is injected by the deployment as an environment
key, so one package moves between environments untouched. Nothing here is
hard coded and nothing here is a credential: the temporary keys of the
assumed agency are handed over by the runtime and are read through
``shared.hwc.credentials``, never from the environment.

Values are read at call time rather than at import time. A warm start then
picks up a refreshed configuration and a test can drive a handler with its own
environment without re-importing anything.
"""

import os

# Keys written by the deployment. Listed in one place so that a missing
# wiring shows up here instead of deep inside a handler.
REGION = "DEPLOYMENT_REGION"
ENVIRONMENT = "DEPLOYMENT_ENVIRONMENT"
PROJECT_ID = "HW_PROJECT_ID"
OBS_BUCKET = "OBS_BUCKET"
OBS_ENDPOINT = "OBS_ENDPOINT"
LOG_LEVEL = "LOG_LEVEL"

INBOUND_PREFIX = "INBOUND_PREFIX"
PROCESSING_PREFIX = "PROCESSING_PREFIX"
OUTBOUND_PREFIX = "OUTBOUND_PREFIX"
RUNS_PREFIX = "RUNS_PREFIX"
CALENDAR_OBJECT = "CALENDAR_OBJECT"
MAX_AGE_DAYS = "MAX_AGE_DAYS"
SCHEDULE_TIMEZONE = "SCHEDULE_TIMEZONE"

FILE_CHECK_URN = "FILE_CHECK_URN"
CALENDAR_CHECK_URN = "CALENDAR_CHECK_URN"
K8S_API_SERVER = "K8S_API_SERVER"
K8S_NAMESPACE = "K8S_NAMESPACE"
K8S_CA_CERT = "K8S_CA_CERT"
K8S_SA_TOKEN = "K8S_SA_TOKEN"
BATCH_IMAGE = "BATCH_IMAGE"
BATCH_CONFIGMAP = "BATCH_CONFIGMAP"
JOB_CONTAINER_NAME = "JOB_CONTAINER_NAME"
JOB_SERVICE_ACCOUNT = "JOB_SERVICE_ACCOUNT"
JOB_CPU_REQUEST = "JOB_CPU_REQUEST"
JOB_MEMORY_REQUEST = "JOB_MEMORY_REQUEST"
JOB_CPU_LIMIT = "JOB_CPU_LIMIT"
JOB_MEMORY_LIMIT = "JOB_MEMORY_LIMIT"
JOB_DEADLINE_SECONDS = "JOB_DEADLINE_SECONDS"
JOB_TTL_SECONDS = "JOB_TTL_SECONDS"
STAGE_BUDGET_SECONDS = "STAGE_BUDGET_SECONDS"
POLL_INTERVAL_SECONDS = "POLL_INTERVAL_SECONDS"
RETRY_MAX_ATTEMPTS = "RETRY_MAX_ATTEMPTS"
RETRY_BASE_SECONDS = "RETRY_BASE_SECONDS"
NOTIFICATION_TOPIC_URN = "NOTIFICATION_TOPIC_URN"
ENABLE_CUSTOM_METRICS = "ENABLE_CUSTOM_METRICS"
METRIC_NAMESPACE = "METRIC_NAMESPACE"
REGRESSION_CONCURRENCY = "REGRESSION_CONCURRENCY"
WORKFLOW_DEFINITION = "WORKFLOW_DEFINITION"

_TRUE = ("1", "true", "yes", "on")


def text(name, default=""):
    """Environment value as text, with the default used for an empty value."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


def integer(name, default):
    """Environment value as an integer. A malformed value falls back to the
    default instead of aborting the run: the default is always usable."""
    try:
        return int(text(name, str(default)))
    except ValueError:
        return default


def flag(name, default=False):
    """Environment value as a boolean."""
    raw = text(name)
    if not raw:
        return default
    return raw.lower() in _TRUE


def log_level():
    return text(LOG_LEVEL, "INFO").upper()


def bucket():
    """Name of the single data bucket of the deployment."""
    return text(OBS_BUCKET)


def obs_endpoint():
    """Endpoint of the object storage service as a bare host, the form the
    signing layer needs. A scheme in the injected value is accepted and
    dropped."""
    host = text(OBS_ENDPOINT, "obs." + region() + ".myhuaweicloud.com")
    for scheme in ("https://", "http://"):
        if host.lower().startswith(scheme):
            host = host[len(scheme):]
    return host.rstrip("/")


def obs_scheme():
    """Object storage refuses a plain HTTP request when the bucket policy
    denies it, so every call is made over TLS."""
    return "https"


def region():
    return text(REGION, "cn-north-4")


def service_host(service):
    """Regional endpoint host of another service.

    Every regional endpoint of a region has the same shape, so the injected
    object storage host gives the suffix the remaining services share. With
    no injected host the public suffix is used.
    """
    host = obs_endpoint()
    prefix, separator, suffix = host.partition(".")
    if separator and prefix == "obs":
        return "%s.%s" % (service, suffix)
    return "%s.%s.myhuaweicloud.com" % (service, region())


def environment():
    return text(ENVIRONMENT, "dev")


def project_id():
    return text(PROJECT_ID)


def inbound_prefix():
    return _slash(text(INBOUND_PREFIX, "inbound/transactions/"))


def processing_prefix():
    return _slash(text(PROCESSING_PREFIX, "processing/"))


def outbound_prefix():
    return _slash(text(OUTBOUND_PREFIX, "outbound/"))


def runs_prefix():
    return _slash(text(RUNS_PREFIX, "runs/"))


def calendar_object():
    return text(CALENDAR_OBJECT, "calendars/non-processing-days.json")


def max_age_days():
    return integer(MAX_AGE_DAYS, 2)


def schedule_timezone():
    return text(SCHEDULE_TIMEZONE, "UTC")


def isolation_prefix(run_id):
    """Namespace a single run writes into.

    A scheduled run uses the production prefixes unchanged. A manual or
    regression run passes a run identifier and every prefix of that run is
    moved under ``<runs prefix><id>/``, so a test run can never read from or
    overwrite the production data set.
    """
    if not run_id:
        return ""
    return runs_prefix() + str(run_id).strip().strip("/") + "/"


def _slash(value):
    return value if value.endswith("/") else value + "/"

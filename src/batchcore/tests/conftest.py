"""Offline test fixtures for the batch functions.

Nothing here reaches a network or a live deployment: the data lake is an
in-memory stand-in for the object storage client, the cluster is a recorded
answer table, and the two validation functions are a dictionary of answers.

The function code is laid out the way the deployed archive is laid out - a
``batchcore`` package under ``src`` - so the bootstrap below puts ``src`` on
the import path instead of duplicating the package for the tests.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import pytest  # noqa: E402

from batchcore.shared import config  # noqa: E402
from batchcore.shared.obs_store import Object  # noqa: E402

BUCKET = "batch-data-test"

# The keys the deployment writes as the runtime configuration of a function.
# Values are the defaults of a fresh environment, so a test states only what
# it cares about.
BASE_ENV = {
    config.REGION: "cn-north-4",
    config.ENVIRONMENT: "dev",
    config.PROJECT_ID: "project-for-tests",
    config.OBS_BUCKET: BUCKET,
    config.OBS_ENDPOINT: "https://obs.cn-north-4.myhuaweicloud.com",
    config.LOG_LEVEL: "INFO",
    config.INBOUND_PREFIX: "inbound/transactions/",
    config.PROCESSING_PREFIX: "processing/",
    config.OUTBOUND_PREFIX: "outbound/",
    config.RUNS_PREFIX: "runs/",
    config.CALENDAR_OBJECT: "calendars/non-processing-days.json",
    config.MAX_AGE_DAYS: "2",
    config.SCHEDULE_TIMEZONE: "UTC",
    config.POLL_INTERVAL_SECONDS: "1",
    config.RETRY_MAX_ATTEMPTS: "1",
    config.RETRY_BASE_SECONDS: "0",
    config.STAGE_BUDGET_SECONDS: "60",
    config.ENABLE_CUSTOM_METRICS: "true",
    config.METRIC_NAMESPACE: "TEST_ORCHESTRATOR",
    config.REGRESSION_CONCURRENCY: "3",
    config.K8S_NAMESPACE: "batch-jobs",
    config.BATCH_IMAGE: "swr.example.com/batch/stub:1",
    config.BATCH_CONFIGMAP: "batch-config",
    config.JOB_CONTAINER_NAME: "batch-job",
    config.JOB_SERVICE_ACCOUNT: "batch-job-runner",
    config.JOB_DEADLINE_SECONDS: "7200",
    config.JOB_TTL_SECONDS: "3600",
    config.WORKFLOW_DEFINITION: "test-orchestrator",
}

# Never inherited by a test run: an operator profile in the shell would open a
# real data path, and the identity resolution order depends on their absence.
SECRET_ENV = (
    "HW_ACCESS_KEY",
    "HW_SECRET_KEY",
    "HW_SECURITY_TOKEN",
    config.FILE_CHECK_URN,
    config.CALENDAR_CHECK_URN,
    config.NOTIFICATION_TOPIC_URN,
    config.K8S_API_SERVER,
    config.K8S_SA_TOKEN,
    config.K8S_CA_CERT,
)


def http_date(moment):
    """A modification time in the shape the object storage service reports."""
    return moment.astimezone(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")


class FakeBackend:
    """The four object storage calls the store makes, over a dictionary."""

    def __init__(self, objects=None, times=None, page_size=1000):
        self.objects = dict(objects or {})
        self.times = dict(times or {})
        self.page_size = page_size
        self.list_calls = []
        self.put_calls = []
        self.fail_on_prefix = None
        self.fail_on_get = None

    def put_object(self, key, body, modified=None):
        self.objects[key] = body
        if modified is not None:
            self.times[key] = modified

    def list_objects(self, bucket, prefix, marker, limit):
        self.list_calls.append((bucket, prefix, marker, limit))
        if self.fail_on_prefix and prefix.startswith(self.fail_on_prefix):
            raise RuntimeError("the lake refused the listing of %s" % prefix)
        keys = sorted(key for key in self.objects if key.startswith(prefix))
        start = keys.index(marker) + 1 if marker in keys else 0
        size = min(limit, self.page_size)
        page = keys[start:start + size]
        truncated = len(keys) > start + len(page)
        return {
            "objects": [
                Object(key, self.times.get(key), len(str(self.objects[key]))) for key in page
            ],
            "truncated": bool(truncated),
            "next_marker": page[-1] if truncated and page else None,
        }

    def get(self, bucket, key):
        if self.fail_on_get and key.endswith(self.fail_on_get):
            raise RuntimeError("the lake refused the read of %s" % key)
        if key not in self.objects:
            return None
        return self.objects[key]

    def head(self, bucket, key):
        return self.times.get(key)

    def put(self, bucket, key, body):
        self.put_calls.append((bucket, key))
        self.objects[key] = body
        return True

    def delete(self, bucket, key):
        self.objects.pop(key, None)
        return True


def make_store(backend=None, bucket=BUCKET):
    """A real store over an in-memory lake."""
    from batchcore.shared.obs_store import ObjectStore

    return ObjectStore(backend if backend is not None else FakeBackend(), bucket)


@pytest.fixture
def env(monkeypatch):
    """The deployment configuration of a function, as environment keys."""
    for name, value in BASE_ENV.items():
        monkeypatch.setenv(name, value)
    for name in SECRET_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def backend():
    return FakeBackend()


@pytest.fixture
def store(backend):
    return make_store(backend)


class Clock:
    """A monotonic clock a test advances by the amount a wait asked for."""

    def __init__(self, start=10000.0):
        self.now = start
        self.slept = []

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    moment = Clock()
    # Every wait inside the budget goes through the module clock, so a pass
    # that polls can be driven without a test ever sleeping for real.
    monkeypatch.setattr("batchcore.orchestrator.state.time.sleep", moment.sleep)
    monkeypatch.setattr("batchcore.orchestrator.index.time.sleep", moment.sleep)
    return moment


def utc(moment):
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


def days_ago(count, hour=9):
    return utc(datetime.now(timezone.utc).replace(hour=hour)) - timedelta(days=count)

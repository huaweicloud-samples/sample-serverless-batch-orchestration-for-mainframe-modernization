"""The four steps of the chain, driven against an in-memory lake.

Every judgement a container makes about its data is exercised here without a
request leaving the process: which records a step reads, which line it drops,
what it writes and where, what a repeated run leaves behind, and which exit
status a fault of each kind ends as. The command line is driven through
``main`` with the settings and the lake handed in, which is the same shape the
container reaches when the environment carries both instead.
"""

import sys
from pathlib import Path

import pytest

JOB_ROOT = Path(__file__).resolve().parents[2] / "job"
if str(JOB_ROOT) not in sys.path:
    sys.path.insert(0, str(JOB_ROOT))

from batch_job import steps  # noqa: E402
from batch_job.__main__ import EXIT_FAILED, EXIT_OK, EXIT_UNKNOWN_STEP, main  # noqa: E402
from batch_job.lake import Lake, StorageError  # noqa: E402
from batch_job.settings import Settings  # noqa: E402

BUCKET = "batch-data-test"

# Never inherited: an operator profile in the shell would let a test reach a
# real bucket, and the readiness judgement depends on its absence.
SECRET_KEYS = ("HW_ACCESS_KEY", "HW_SECRET_KEY", "HW_SECURITY_TOKEN")

EXTRACT = "account_id,amount,occurred\n  1001,25.50,2026-10-25  \n1002,7.25,2026-10-26\n"


class Backend:
    """The service calls of the lake over a dictionary.

    Keys are reported as plain text, exactly the way the client of the object
    storage service is reduced to plain values by the real backend.
    """

    def __init__(self, objects=None, refuse=None):
        self.objects = dict(objects or {})
        self.refuse = refuse
        self.puts = []

    def list_objects(self, bucket, prefix, marker, limit):
        self._check(prefix)
        keys = sorted(key for key in self.objects if key.startswith(prefix))
        return {"objects": keys, "truncated": False, "next_marker": None}

    def get(self, bucket, key):
        self._check(key)
        return self.objects.get(key)

    def put(self, bucket, key, body):
        self._check(key)
        self.puts.append(key)
        self.objects[key] = body
        return True

    def _check(self, what):
        if self.refuse and what.startswith(self.refuse):
            raise StorageError("refused %s" % what, status=500, code="InternalError")


def lake_with(backend):
    return Lake(backend, BUCKET)


@pytest.fixture
def conf(monkeypatch):
    """A container environment of a deployment, with nothing secret in it."""
    for name, value in (
        ("OBS_BUCKET", BUCKET),
        ("OBS_ENDPOINT", "obs.cn-north-4.myhuaweicloud.com"),
        ("OBS_REGION", "cn-north-4"),
        ("INBOUND_PREFIX", "inbound/transactions/"),
        ("PROCESSING_PREFIX", "processing/"),
        ("OUTBOUND_PREFIX", "outbound/"),
        ("LOG_LEVEL", "INFO"),
        ("RUN_ID", "batch-2026-10-26"),
        ("HW_ACCESS_KEY", "access-key-for-tests"),
        ("HW_SECRET_KEY", "secret-key-for-tests"),
    ):
        monkeypatch.setenv(name, value)
    for name in ("OBS_PREFIX",):
        monkeypatch.delenv(name, raising=False)
    return Settings()


@pytest.fixture
def lake():
    return lake_with(Backend())


# -- the addresses a step uses ------------------------------------------


def test_a_step_addresses_its_data_through_the_deployment(conf):
    assert steps.extract_folder(conf) == "inbound/transactions/"
    assert steps.output_folder(conf, "JOB1") == "processing/JOB1/output/"
    assert steps.output_folder(conf, "JOB2A") == "processing/JOB2A/output/"
    assert steps.outbound_folder(conf) == "outbound/"
    assert conf.object_key(steps.output_folder(conf, "JOB1"), steps.NORMALIZED) == (
        "processing/JOB1/output/normalized.csv"
    )


def test_a_run_scoped_namespace_keeps_every_address_inside_it(conf, monkeypatch):
    monkeypatch.setenv("OBS_PREFIX", "runs/batch-2026-10-26")
    scoped = Settings()
    assert steps.extract_folder(scoped) == "runs/batch-2026-10-26/inbound/transactions/"
    assert steps.outbound_folder(scoped) == "runs/batch-2026-10-26/outbound/"


# -- JOB1 ----------------------------------------------------------------


def test_the_first_step_drops_the_column_names_of_the_extract(conf, lake):
    lake.backend.objects["inbound/transactions/txns.20261026.csv"] = EXTRACT
    steps.job1(conf, lake)
    records = lake.read("processing/JOB1/output/normalized.csv").splitlines()
    assert not any(row.startswith("account_id") for row in records)
    assert records == ["1001,25.50,2026-10-25", "1002,7.25,2026-10-26"]


def test_the_first_step_reads_the_extract_and_nothing_else_of_the_folder(conf, lake):
    lake.backend.objects = {
        "inbound/transactions/txns.20261025.csv": "1000,1.00,2026-10-25\n",
        "inbound/transactions/txns.2026-10-26.csv": "9999,9.99,2026-10-26\n",
        "inbound/transactions/readme.txt": "delivered by the host system\n",
    }
    steps.job1(conf, lake)
    assert lake.read("processing/JOB1/output/normalized.csv") == "1000,1.00,2026-10-25"


def test_the_first_step_concatenates_the_days_of_a_backlog_in_order(conf, lake):
    lake.backend.objects = {
        "inbound/transactions/txns.20261026.csv": "account_id,amount,occurred\n2001,2.00,x\n",
        "inbound/transactions/txns.20261025.csv": "account_id,amount,occurred\n1001,1.00,x\n",
    }
    steps.job1(conf, lake)
    assert lake.read("processing/JOB1/output/normalized.csv").splitlines() == [
        "1001,1.00,x",
        "2001,2.00,x",
    ]


# -- JOB2A and JOB2B ------------------------------------------------------


def test_the_two_parallel_steps_read_the_same_normalized_data_set(conf, lake):
    lake.backend.objects["processing/JOB1/output/normalized.csv"] = "1001,25.50,x\n1002,7.25,y"
    steps.job2a(conf, lake)
    steps.job2b(conf, lake)
    assert lake.read("processing/JOB2A/output/fees.csv") == "1001,25.50,x,FEE\n1002,7.25,y,FEE"
    assert lake.read("processing/JOB2B/output/rebates.csv") == (
        "1001,25.50,x,REBATE\n1002,7.25,y,REBATE"
    )
    assert lake.backend.puts == [
        "processing/JOB2A/output/fees.csv",
        "processing/JOB2B/output/rebates.csv",
    ]


# -- JOB3 -----------------------------------------------------------------


def test_the_merge_states_its_counts_first_and_the_fees_before_the_rebates(conf, lake):
    lake.backend.objects = {
        "processing/JOB2A/output/fees.csv": "1001,25.50,x,FEE\n1002,7.25,y,FEE",
        "processing/JOB2B/output/rebates.csv": "1001,25.50,x,REBATE",
        "outbound/summary.old": "not read: the merge asks for one exact name",
    }
    steps.job3(conf, lake)
    assert lake.read("outbound/summary.csv").splitlines() == [
        "# summary: 2 fee rows, 1 rebate rows",
        "1001,25.50,x,FEE",
        "1002,7.25,y,FEE",
        "1001,25.50,x,REBATE",
    ]


# -- a step that runs twice ----------------------------------------------


def test_a_repeated_step_leaves_one_result_data_set_behind(conf, lake):
    lake.backend.objects["inbound/transactions/txns.20261026.csv"] = EXTRACT
    steps.job1(conf, lake)
    lake.backend.objects["inbound/transactions/txns.20261026.csv"] = "account_id,a\n3001,1\n"
    steps.job1(conf, lake)
    assert lake.backend.puts == [
        "processing/JOB1/output/normalized.csv",
        "processing/JOB1/output/normalized.csv",
    ]
    assert lake.read("processing/JOB1/output/normalized.csv") == "3001,1"


# -- the command line -----------------------------------------------------


def test_the_command_line_runs_a_step_and_exits_zero(conf):
    backend = Backend({"inbound/transactions/txns.20261026.csv": EXTRACT})
    answer = main(["JOB1"], settings=conf, lake=lake_with(backend))
    assert answer == EXIT_OK
    assert backend.objects["processing/JOB1/output/normalized.csv"] == (
        "1001,25.50,2026-10-25\n1002,7.25,2026-10-26"
    )


def test_the_command_line_defaults_to_the_first_step_of_the_chain(conf):
    backend = Backend({"inbound/transactions/txns.20261026.csv": EXTRACT})
    assert main([], settings=conf, lake=lake_with(backend)) == EXIT_OK
    assert "processing/JOB1/output/normalized.csv" in backend.objects


def test_a_merge_of_two_data_sets_nobody_wrote_still_publishes_its_marker(conf):
    backend = Backend()
    assert main(["JOB3"], settings=conf, lake=lake_with(backend)) == EXIT_OK
    # The counts are the evidence of what the two steps behind this one
    # produced, so the line is written even when both answers are zero.
    assert backend.objects["outbound/summary.csv"] == "# summary: 0 fee rows, 0 rebate rows"


def test_an_unknown_step_name_is_a_fault_of_the_job_spec(conf):
    backend = Backend({"inbound/transactions/txns.20261026.csv": EXTRACT})
    assert main(["JOB9"], settings=conf, lake=lake_with(backend)) == EXIT_UNKNOWN_STEP
    assert backend.puts == []


def test_an_environment_that_cannot_reach_the_lake_stops_before_the_data(conf, monkeypatch):
    monkeypatch.setenv("OBS_BUCKET", "")
    broken = Settings()
    assert broken.ready() is False
    backend = Backend({"inbound/transactions/txns.20261026.csv": EXTRACT})
    assert main(["JOB1"], settings=broken, lake=lake_with(backend)) == EXIT_FAILED
    assert backend.puts == []


def test_a_lake_that_refuses_a_call_ends_as_a_failed_step(conf):
    backend = Backend({"inbound/transactions/txns.20261026.csv": EXTRACT}, refuse="inbound")
    assert main(["JOB1"], settings=conf, lake=lake_with(backend)) == EXIT_FAILED


def test_a_fault_nobody_planned_for_is_a_failed_step_too(conf):
    class Collapsed(Lake):
        """A step that dies on something other than a refused call."""

        def rows(self, prefix, pattern):
            raise ValueError("the extract of today is not readable as text")

    assert main(["JOB1"], settings=conf, lake=Collapsed(Backend(), BUCKET)) == EXIT_FAILED


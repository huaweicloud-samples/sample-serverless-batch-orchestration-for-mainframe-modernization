"""The four steps of the chain, as the container runs them.

A step reads one data set of the lake, transforms its records and writes one
result data set. Nothing here imports a client library: the three calls the lake
offers are the whole of what a step uses, so this module loads in a plain
interpreter and a self-test hands in a stand-in for the service.

The addresses are derived, never written out. A folder is the area prefix of the
deployment plus the identifier of the step that owns it, which is the same rule
the input gate uses, so one image moves between environments that name their
folders differently and the two layers cannot disagree about where a data set
is.
"""

import logging

log = logging.getLogger("batch.job")

# Folder a step's own result data set is stored under, inside the processing
# area. Kept out of the step bodies because the gate derives it the same way.
OUTPUT_DIR = "output"

# The names the chain produces. A step reads exactly the name its predecessor
# wrote, so an object an earlier experiment left in a folder is not read as
# data of this run.
NORMALIZED = "normalized.csv"
FEES = "fees.csv"
REBATES = "rebates.csv"
SUMMARY = "summary.csv"

EXTRACT_PATTERN = r"^txns\.\d{8}\.csv$"
NORMALIZED_PATTERN = r"^normalized\.csv$"
FEES_PATTERN = r"^fees\.csv$"
REBATES_PATTERN = r"^rebates\.csv$"

# A delimited extract arrives with its column names as the first line. Those
# names are not a transaction: carried on, they reach every step behind this
# one and land in the merged file as if they were one more record.
HEADER_PREFIX = "account_id,"

# The marker each of the two parallel steps appends, and the separator they
# append it with. The pair of steps is the point of the chain: both read the
# same normalized data set and neither reads what the other wrote.
FIELD_SEPARATOR = ","
FEE_MARKER = "FEE"
REBATE_MARKER = "REBATE"

SUMMARY_PREFIX = "# summary: "


def extract_folder(conf):
    """Folder the arriving daily extract is delivered to."""
    return conf.scope_of(conf.inbound_prefix)


def output_folder(conf, producer):
    """Folder holding the result data set one step wrote."""
    return conf.folder(conf.processing_prefix, producer, OUTPUT_DIR)


def outbound_folder(conf):
    """Folder the merged result of the chain is published to."""
    return conf.scope_of(conf.outbound_prefix)


def _mark(rows, marker):
    return [row + FIELD_SEPARATOR + marker for row in rows]


def _report(step, read, written, key):
    # One line per step is what an operator reads back from the log of a
    # finished job, so the counts and the address are stated together.
    log.info("step=%s read=%d written=%d out=%s", step, read, written, key)


def job1(conf, lake):
    """Normalize the arriving transaction extract.

    The header line goes and every record is trimmed, so what the two steps
    behind this one read is records only.
    """
    rows, _files = lake.rows(extract_folder(conf), EXTRACT_PATTERN)
    records = [row.strip() for row in rows if not row.startswith(HEADER_PREFIX)]
    key = conf.object_key(output_folder(conf, "JOB1"), NORMALIZED)
    lake.write(key, "\n".join(records))
    _report("JOB1", len(rows), len(records), key)


def job2a(conf, lake):
    """Append the fee marker to every normalized record."""
    rows, _files = lake.rows(output_folder(conf, "JOB1"), NORMALIZED_PATTERN)
    records = _mark(rows, FEE_MARKER)
    key = conf.object_key(output_folder(conf, "JOB2A"), FEES)
    lake.write(key, "\n".join(records))
    _report("JOB2A", len(rows), len(records), key)


def job2b(conf, lake):
    """Append the rebate marker to every normalized record."""
    rows, _files = lake.rows(output_folder(conf, "JOB1"), NORMALIZED_PATTERN)
    records = _mark(rows, REBATE_MARKER)
    key = conf.object_key(output_folder(conf, "JOB2B"), REBATES)
    lake.write(key, "\n".join(records))
    _report("JOB2B", len(rows), len(records), key)


def job3(conf, lake):
    """Merge the two parallel results into the published summary.

    The counts of the two inputs are written as the first line of the result:
    a reader of the merged file then sees how many records the merge expected,
    without listing the lake. The fee records come first and the rebate records
    after, so a reader gets one data set and then the other, the same way the
    two steps were named in the chain.
    """
    fees, _a = lake.rows(output_folder(conf, "JOB2A"), FEES_PATTERN)
    rebates, _b = lake.rows(output_folder(conf, "JOB2B"), REBATES_PATTERN)
    body = [SUMMARY_PREFIX + "%d fee rows, %d rebate rows" % (len(fees), len(rebates))]
    body.extend(fees)
    body.extend(rebates)
    key = conf.object_key(outbound_folder(conf), SUMMARY)
    lake.write(key, "\n".join(body))
    _report("JOB3", len(fees) + len(rebates), len(body), key)


STEPS = {"JOB1": job1, "JOB2A": job2a, "JOB2B": job2b, "JOB3": job3}


def names():
    """The steps this container knows, in chain order."""
    return list(STEPS)

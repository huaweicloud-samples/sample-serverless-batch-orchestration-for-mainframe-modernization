"""Input validation of one batch step.

This is the pre-condition a scheduler would otherwise express as a file
watcher: before a step is allowed to run, every dataset it reads has to be
present in the lake and, for the datasets that arrive on a cycle, recent
enough to belong to the run that is starting.

Two facts are decided per configured folder:

* existence, at least one name under the folder matching its pattern
* freshness, for the folders that hold arriving data, the newest matching
  object is not older than the allowed window, counted in UTC calendar days

A folder holding a data set the run itself just wrote is present checked only;
applying an age rule to it would stop the chain every time the lake is slow to
list.

The result is a plain structure, consumed by the orchestration layer and
useful on its own when an operator asks why a chain is waiting.
"""

import re
from datetime import datetime, timezone

from ..shared import config, logs

log = logs.get("file-check")

# The newest objects probed for a modification time. A backlog far beyond
# what the age window allows is a fault in its own right, and probing every
# object of such a prefix would spend the time budget of the call on it.
MODIFICATION_PROBE_LIMIT = 25

# One entry per step: the folders it reads, the name it expects in each of
# them, and whether that name also has to be recent. The folders of an
# intermediate data set are derived from the prefix of the deployment and the
# step that wrote it, so the table below stays free of literal paths.
STEPS = {
    "JOB1": {
        "recent": True,
        "reads": ((config.INBOUND_PREFIX, None, r"^txns\.\d{8}\.csv$"),),
    },
    "JOB2A": {
        "recent": False,
        "reads": ((config.PROCESSING_PREFIX, "JOB1", r"^normalized\.csv$"),),
    },
    "JOB2B": {
        "recent": False,
        "reads": ((config.PROCESSING_PREFIX, "JOB1", r"^normalized\.csv$"),),
    },
    "JOB3": {
        "recent": False,
        "reads": (
            (config.PROCESSING_PREFIX, "JOB2A", r"^fees\.csv$"),
            (config.PROCESSING_PREFIX, "JOB2B", r"^rebates\.csv$"),
        ),
    },
}

# The names the chain and an operator can use for a step.
ALIASES = {}
for _step, _aliases in (
    ("JOB1", ("job1", "check_job1_input_files")),
    ("JOB2A", ("job2a", "check_job2a_input_files")),
    ("JOB2B", ("job2b", "check_job2b_input_files")),
    ("JOB3", ("job3", "check_job3_input_files")),
):
    for _alias in _aliases:
        ALIASES[_alias] = _step

MISSING = "INPUT_FILES_MISSING"
TOO_OLD = "INPUT_FILES_TOO_OLD"
INVALID = "INPUT_FILES_INVALID"
UNREADABLE = "STORAGE_UNREADABLE"


def step_names():
    """The steps this validation knows about."""
    return sorted(STEPS)


def resolve_step(event):
    """Which step the question is about, or ``None`` for an unknown name.

    A caller may name the step, name the check as a whole, or pass the step
    through the field the orchestration layer happens to use.
    """
    for field in ("jobName", "step", "job", "module"):
        raw = str(event.get(field) or "").strip()
        if not raw:
            continue
        if raw in STEPS:
            return raw
        hit = ALIASES.get(raw.lower().replace("-", "_"))
        if hit:
            return hit
    return None


def folder_of(prefix_key, producer):
    """Folder of a data set, derived from the deployment.

    An arriving data set sits directly under its prefix. An intermediate one
    sits under the prefix of the processing area, in the output folder of the
    step that wrote it.
    """
    base = config.text(prefix_key)
    if producer:
        base = _slash(base) + producer + "/output/"
    return _slash(base)


def _slash(value):
    return value if value.endswith("/") else value + "/"


def run(event, store):
    """Answer the question: may this step start?"""
    step = resolve_step(event)
    if step is None:
        return _invalid(event)

    isolation = config.isolation_prefix(event.get("runId") or "")
    definition = STEPS[step]
    limit = config.max_age_days()

    results = []
    failures = 0
    for prefix_key, producer, pattern in definition["reads"]:
        relative = folder_of(prefix_key, producer)
        result = _check_folder(
            store, isolation + relative, pattern, definition["recent"], limit
        )
        results.append(result)
        if not result["present"]:
            failures += 1

    present = failures == 0
    aggregate = next((r["errorType"] for r in results if r["errorType"]), None)
    log.info(
        "step=%s scope=%s present=%s failed=%d",
        step,
        isolation or "(production)",
        present,
        failures,
    )
    return {
        "step": step,
        "jobName": step,
        "allFilesPresent": present,
        "errorType": None if present else aggregate,
        "bucket": store.bucket,
        "prefix": isolation or "(production)",
        "filesChecked": len(results),
        "missingCount": failures,
        "maxAgeDays": limit,
        "fileResults": results,
        "message": (
            "Every input of %s is present." % step
            if present
            else "%d input check(s) of %s did not pass." % (failures, step)
        ),
    }


def _check_folder(store, prefix, pattern, recent, limit):
    try:
        candidates = _matching(store, prefix, pattern)
    except Exception as error:  # an unreadable lake is not a missing file
        log.error("listing %s failed: %s", prefix, error)
        return {
            "prefix": prefix,
            "pattern": pattern,
            "present": False,
            "ageCheckApplied": False,
            "ageStatus": "STORAGE_ERROR",
            "errorType": UNREADABLE,
            "detail": str(error),
        }

    if not candidates:
        return {
            "prefix": prefix,
            "pattern": pattern,
            "present": False,
            "ageCheckApplied": recent,
            "ageStatus": "FILE_NOT_FOUND",
            "errorType": MISSING,
        }

    if not recent:
        return {
            "prefix": prefix,
            "pattern": pattern,
            "present": True,
            "matchedObjects": len(candidates),
            "ageCheckApplied": False,
            "ageStatus": "AGE_CHECK_NOT_REQUIRED",
            "errorType": None,
        }

    newest, moment = _newest(store, candidates)
    if moment is None:
        # No readable time means no proof of freshness: the step waits rather
        # than running on an assumption.
        return {
            "prefix": prefix,
            "pattern": pattern,
            "present": False,
            "matchedObjects": len(candidates),
            "newestKey": newest,
            "ageCheckApplied": True,
            "ageStatus": "MODIFICATION_TIME_UNKNOWN",
            "errorType": TOO_OLD,
        }

    days_old = _days_old(moment)
    base = {
        "prefix": prefix,
        "pattern": pattern,
        "matchedObjects": len(candidates),
        "newestKey": newest,
        "ageCheckApplied": True,
        "numberOfDaysOld": days_old,
        "maxAgeDays": limit,
    }
    if days_old <= limit:
        base.update(
            {"present": True, "ageStatus": "WITHIN_ALLOWED_AGE", "errorType": None}
        )
    else:
        base.update(
            {
                "present": False,
                "ageStatus": "OLDER_THAN_%d_DAYS" % limit,
                "errorType": TOO_OLD,
            }
        )
    return base


def _matching(store, prefix, pattern):
    expression = re.compile(pattern)
    return [item for item in store.list(prefix) if expression.match(item.name)]


def _newest(store, candidates):
    """The most recent object of a matched set, together with its time."""
    ordered = sorted(candidates, key=lambda item: item.key, reverse=True)
    probe = ordered[:MODIFICATION_PROBE_LIMIT]
    if len(ordered) > len(probe):
        log.warning(
            "%d matches under %s, the %d newest probed for a modification time",
            len(ordered),
            ordered[0].key.rsplit("/", 1)[0],
            len(probe),
        )
    best_key, best = None, None
    for item in probe:
        moment = store.modified(item.key)
        if moment is not None and (best is None or moment > best):
            best, best_key = moment, item.key
    if best_key is None:
        return (probe[0].key if probe else None), None
    return best_key, best


def _days_old(moment):
    today = datetime.now(timezone.utc).date()
    return (today - moment.date()).days


def _invalid(event):
    """A name that matches no known step, reported instead of guessed."""
    return {
        "step": None,
        "jobName": event.get("jobName") or event.get("step") or event.get("module"),
        "allFilesPresent": False,
        "errorType": INVALID,
        "message": "No input definition is known for this step name.",
        "knownSteps": step_names(),
        "fileResults": [],
    }

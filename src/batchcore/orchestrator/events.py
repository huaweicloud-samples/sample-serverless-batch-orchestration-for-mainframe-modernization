"""Where a run comes from and what it is asked to do.

Three sources open a run and each of them arrives as a different shape of
event:

* a schedule pass, whose payload is the wrapper the timer writes
* an operator invoking the function directly, usually to replay one day or to
  run an isolated set of modules
* a platform notice about a job that finished, which only advances the run it
  belongs to

Reading them is kept in one module so that the flow itself never has to guess
where a value came from.
"""

import json
import re
import uuid
from datetime import datetime, timezone

from ..shared import config, logs

log = logs.get("events")

ACTION_START = "start_run"
ACTION_RESUME = "resume_runs"
ACTION_REGRESSION = "run_regression"
ACTION_JOB_EVENT = "job_event"

SCHEDULED = "scheduled"
MANUAL = "manual"
REGRESSION = "regression"

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_STEPS = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")


class Event:
    """One normalised request to do work."""

    __slots__ = ("action", "kind", "run_id", "run_date", "steps", "isolation", "budget", "raw")

    def __init__(self, action, kind, run_id=None, run_date=None, steps=(), isolation="",
                 budget=None, raw=None):
        self.action = action
        self.kind = kind
        self.run_id = run_id
        self.run_date = run_date
        self.steps = tuple(steps)
        self.isolation = isolation
        self.budget = budget
        self.raw = raw or {}

    def __repr__(self):
        return "Event(action=%s kind=%s run_id=%s run_date=%s steps=%s)" % (
            self.action,
            self.kind,
            self.run_id,
            self.run_date,
            list(self.steps),
        )


def parse(event, context=None):
    """Turn any accepted event into an :class:`Event`.

    An event that cannot be read is treated as a scheduled resume pass: that
    is the pass which runs most often, and a malformed payload from a console
    must not open a production run by accident.
    """
    payload = _unwrap(event, context)
    action = str(payload.get("action") or "").strip()
    job_hint = _job_event(payload)

    if action:
        if action == ACTION_START:
            return _start(payload)
        if action == ACTION_RESUME:
            return Event(ACTION_RESUME, SCHEDULED, raw=payload)
        if action == ACTION_REGRESSION:
            return _regression(payload)
        log.warning("unknown action %r, resuming in flight runs instead", action)
        return Event(ACTION_RESUME, SCHEDULED, raw=payload)

    if job_hint:
        return Event(ACTION_JOB_EVENT, SCHEDULED, run_id=job_hint, raw=payload)

    if payload.get("runId"):
        return _start(payload)
    if payload.get("modules") or payload.get("steps"):
        return _regression(payload)
    if payload.get("runDate"):
        return _start(payload)
    return Event(ACTION_RESUME, SCHEDULED, raw=payload)


def _unwrap(event, context):
    """Follow the wrappers a call can arrive in.

    A timer writes its payload inside an event wrapper, a console test writes
    the payload straight, and a platform notice about a finished job arrives
    as a JSON document in a string. Each of those is unwrapped here, once, and
    nothing downstream has to know which happened.
    """
    payload = event
    if context is not None and isinstance(event, dict) and event.get("Log"):
        # A notice forwarded from the log pipeline carries the interesting
        # object inside a record list.
        payload = _first_record(event["Log"])

    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", "replace")
    if isinstance(payload, str):
        payload = _loads(payload)
    if not isinstance(payload, dict):
        return {}

    for field in ("user_event", "detail", "Message", "message"):
        inner = payload.get(field)
        if isinstance(inner, str) and inner.strip().startswith("{"):
            merged = dict(payload)
            merged.update(_loads(inner))
            payload = merged
            break
    if isinstance(payload.get("additional_information"), str):
        payload = dict(payload)
        payload.update(_loads(payload["additional_information"]))
    if isinstance(payload.get("action"), dict):
        payload = dict(payload, **payload["action"])
    return payload


def _first_record(log_blob):
    try:
        records = json.loads(log_blob) if isinstance(log_blob, str) else log_blob
        first = records[0]
    except (IndexError, TypeError, ValueError, KeyError):
        return {}
    content = first.get("content") if isinstance(first, dict) else None
    return _loads(content) if isinstance(content, str) else (content or {})


def _loads(text):
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _job_event(payload):
    """The run a platform notice about a finished job belongs to.

    The label the job carries is the only stable link back to a run, so the
    notice is read for it and for nothing else.
    """
    if str(payload.get("kind") or "").lower() != "job":
        return None
    metadata = payload.get("metadata") or {}
    labels = metadata.get("labels") or payload.get("labels") or {}
    run_id = labels.get("batch.job/run-id") or payload.get("runId")
    return str(run_id) if run_id else None


def _start(payload):
    run_date = _date(payload.get("runDate"))
    return Event(
        ACTION_START,
        MANUAL if run_date or payload.get("runId") else SCHEDULED,
        run_id=_run_id(payload.get("runId")),
        run_date=run_date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        budget=_budget(payload),
        raw=payload,
    )


def _regression(payload):
    """An isolated run of one or more modules.

    The run identifier is what makes it isolated: it is generated when the
    caller does not supply one, because a regression run that writes into the
    production prefixes would corrupt the data of the next scheduled run.
    """
    steps = payload.get("modules") or payload.get("steps") or []
    if isinstance(steps, str):
        steps = [part for part in re.split(r"[,\s]+", steps) if part]
    cleaned = [str(part).strip().upper() for part in steps if _STEPS.match(str(part).strip())]
    isolation = str(payload.get("isolation") or payload.get("testRunPrefix") or "").strip()
    run_id = _run_id(payload.get("runId")) or _new_run_id()
    if not isolation:
        isolation = config.isolation_prefix(run_id)
    return Event(
        ACTION_REGRESSION,
        REGRESSION,
        run_id=run_id,
        run_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        steps=cleaned,
        isolation=_slash(isolation),
        budget=_budget(payload),
        raw=payload,
    )


def _budget(payload):
    try:
        value = int(payload.get("budgetSeconds") or payload.get("stageBudgetSeconds") or 0)
    except (TypeError, ValueError):
        return None
    return value or None


def _date(value):
    text = str(value or "").strip()
    return text if _DATE.match(text) else None


def _run_id(value):
    text = str(value or "").strip()
    return text or None


def _new_run_id():
    """A label that is unique, short and legal inside a resource name."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return "%s-%s" % (stamp, uuid.uuid4().hex[:6])


def _slash(value):
    return value if value.endswith("/") else value + "/"

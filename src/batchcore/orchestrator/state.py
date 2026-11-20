"""The state of a run, and where it is kept.

A run lasts far longer than one pass of a function, so everything a pass needs
to hand over to the next one is written as one small object in the data lake,
under the prefix reserved for run checkpoints. A pass reads that object,
advances it and writes it back; the run then survives a restart, a timeout and
a redeploy of the function itself.

The object is deliberately boring: identifiers, phases, timestamps and the
reason a step stopped. It is the record an operator reads when asking what
happened on the night of a failed run, so it holds conclusions rather than
scratch values.
"""

import json
import re
import time
from datetime import datetime, timezone

from ..shared import config, logs

log = logs.get("state")

PENDING = "pending"
RUNNING = "running"
WAITING = "waiting for retry"
SUCCEEDED = "succeeded"
FAILED = "failed"
SKIPPED = "skipped"

TERMINAL = (SUCCEEDED, FAILED, SKIPPED)

RUN_RUNNING = "RUNNING"
RUN_SUCCEEDED = "SUCCEEDED"
RUN_FAILED = "FAILED"
RUN_SKIPPED = "SKIPPED"

_NAME_SAFE = re.compile(r"[^a-z0-9-]+")
_MAX_NAME = 63


class StepState:
    """One step of one run."""

    __slots__ = (
        "identifier",
        "phase",
        "job",
        "attempts",
        "error_type",
        "exit_code",
        "started_at",
        "finished_at",
        "retry_at",
        "check",
        "detail",
    )

    def __init__(self, identifier, phase=PENDING, job=None, attempts=0, error_type=None,
                 exit_code=None, started_at=None, finished_at=None, retry_at=None, check=None,
                 detail=None):
        self.identifier = identifier
        self.phase = phase
        self.job = job
        self.attempts = attempts
        self.error_type = error_type
        self.exit_code = exit_code
        self.started_at = started_at
        self.finished_at = finished_at
        self.retry_at = retry_at
        self.check = check
        self.detail = detail

    @property
    def terminal(self):
        return self.phase in TERMINAL

    def to_dict(self):
        return {
            "phase": self.phase,
            "job": self.job,
            "attempts": self.attempts,
            "errorType": self.error_type,
            "exitCode": self.exit_code,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "retryAt": self.retry_at,
            "check": self.check,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, identifier, value):
        value = value or {}
        return cls(
            identifier,
            phase=value.get("phase") or PENDING,
            job=value.get("job"),
            attempts=int(value.get("attempts") or 0),
            error_type=value.get("errorType"),
            exit_code=value.get("exitCode"),
            started_at=value.get("startedAt"),
            finished_at=value.get("finishedAt"),
            retry_at=value.get("retryAt"),
            check=value.get("check"),
            detail=value.get("detail"),
        )


class RunState:
    """A whole run, ready to be written as JSON."""

    __slots__ = (
        "run_id",
        "kind",
        "run_date",
        "isolation",
        "status",
        "error_type",
        "message",
        "started_at",
        "updated_at",
        "steps",
        "plan",
        "transitions",
        "budget",
    )

    def __init__(self, run_id, kind, run_date, isolation="", steps=None, plan=None,
                 status=RUN_RUNNING, started_at=None, budget=None):
        self.run_id = run_id
        self.kind = kind
        self.run_date = run_date
        self.isolation = isolation or ""
        self.status = status
        self.error_type = None
        self.message = None
        self.started_at = started_at or now()
        self.updated_at = self.started_at
        self.steps = steps if steps is not None else {}
        self.plan = plan or {}
        self.transitions = []
        self.budget = budget

    # -- reading ----------------------------------------------------------

    def step(self, identifier):
        return self.steps.get(identifier)

    def phases(self):
        return {key: value.phase for key, value in sorted(self.steps.items())}

    @property
    def unfinished(self):
        return [s for s in self.steps.values() if not s.terminal]

    @property
    def running(self):
        return [s for s in self.steps.values() if s.phase == RUNNING]

    @property
    def waiting(self):
        return [s for s in self.steps.values() if s.phase == WAITING]

    # -- writing ----------------------------------------------------------

    def begin(self, identifier):
        state = self.steps.setdefault(identifier, StepState(identifier))
        return state

    def record(self, state, phase, **changes):
        """Move a step to a phase and keep the trail of it."""
        state.phase = phase
        for key, value in changes.items():
            setattr(state, _FIELD.get(key, key), value)
        if phase in TERMINAL and not state.finished_at:
            state.finished_at = now()
        self.transitions.append([state.identifier, phase, now()])
        # The trail is what an operator reads afterwards; two hundred entries
        # is far beyond a chain of four steps and bounds the checkpoint size.
        del self.transitions[:-200]
        self.updated_at = now()
        return state

    def to_dict(self):
        return {
            "runId": self.run_id,
            "kind": self.kind,
            "runDate": self.run_date,
            "isolation": self.isolation,
            "status": self.status,
            "errorType": self.error_type,
            "message": self.message,
            "startedAt": self.started_at,
            "updatedAt": self.updated_at,
            "budgetSeconds": self.budget,
            "plan": self.plan,
            "steps": {key: value.to_dict() for key, value in sorted(self.steps.items())},
            "transitions": self.transitions,
        }

    @classmethod
    def from_dict(cls, value):
        state = cls(
            run_id=value.get("runId") or "",
            kind=value.get("kind") or "",
            run_date=value.get("runDate") or "",
            isolation=value.get("isolation") or "",
            status=value.get("status") or RUN_RUNNING,
            started_at=value.get("startedAt"),
            budget=value.get("budgetSeconds"),
            plan=value.get("plan") or {},
            steps={
                key: StepState.from_dict(key, raw)
                for key, raw in (value.get("steps") or {}).items()
            },
        )
        state.error_type = value.get("errorType")
        state.message = value.get("message")
        state.updated_at = value.get("updatedAt") or state.updated_at
        state.transitions = list(value.get("transitions") or [])
        return state


_FIELD = {
    "job": "job",
    "attempts": "attempts",
    "error_type": "error_type",
    "exit_code": "exit_code",
    "retry_at": "retry_at",
    "check": "check",
    "detail": "detail",
    "started_at": "started_at",
}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def job_name(run_id, step):
    """The name a step of this run is given in the cluster.

    Lower case, dashes only and short enough for the platform: the run
    identifier carries the date and a suffix, the step carries the program, so
    a name read from a pod log says which run and which step it came from.
    """
    base = "job-%s-%s" % (_safe(step), _safe(run_id))
    if len(base) > _MAX_NAME:
        base = base[:_MAX_NAME].rstrip("-")
    return base


def _safe(value):
    cleaned = _NAME_SAFE.sub("-", str(value).lower()).strip("-")
    return cleaned or "x"


class Checkpoints:
    """Read and write the run objects of the data lake."""

    def __init__(self, store):
        self.store = store

    def path(self, run_id):
        return "%s%s.json" % (config.runs_prefix(), run_id)

    def save(self, state):
        written = self.store.write_json(self.path(state.run_id), state.to_dict())
        if not written:
            log.error("checkpoint of run %s could not be written", state.run_id)
        return written

    def load(self, run_id):
        raw = self.store.read(self.path(run_id))
        if raw is None:
            return None
        try:
            return RunState.from_dict(json.loads(raw))
        except ValueError:
            log.error("checkpoint of run %s is not readable JSON", run_id)
            return None

    def open_runs(self, older_than_seconds=None):
        """Every run that has not reached a final phase.

        The resume pass has no identifier to start from, so it reads the
        checkpoint prefix. A run whose object cannot be read is reported and
        skipped rather than half resumed.
        """
        prefix = config.runs_prefix()
        found = []
        try:
            objects = self.store.list(prefix)
        except Exception as error:
            log.error("checkpoint prefix %s unreadable: %s", prefix, error)
            return found
        for item in objects:
            if not item.key.endswith(".json"):
                continue
            state = self.load(item.key[len(prefix):].rsplit(".json", 1)[0])
            if state is None or state.status != RUN_RUNNING:
                continue
            if older_than_seconds is not None and _age(state.updated_at) < older_than_seconds:
                continue
            found.append(state)
        return sorted(found, key=lambda state: state.started_at or "")

    def delete(self, run_id):
        return self.store.delete(self.path(run_id))


def _age(stamp):
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return 0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment).total_seconds()


def deadline_seconds(budget):
    """Seconds left of the time a pass is allowed to spend.

    Read at the start of a pass and never recomputed: a pass that restarted its
    own clock would never give way to the next one.
    """
    return float(budget or config.integer(config.STAGE_BUDGET_SECONDS, 900))


class Budget:
    """The time a pass may spend, and the fact that it must stop."""

    def __init__(self, seconds=None, clock=time.monotonic):
        self.seconds = deadline_seconds(seconds)
        self._clock = clock
        self.started = clock()

    def elapsed(self):
        return self._clock() - self.started

    def left(self):
        return max(0.0, self.seconds - self.elapsed())

    def expired(self):
        # A step of margin: a pass that starts a call it cannot finish leaves
        # the run with a job nobody is watching.
        return self.left() <= min(60.0, self.seconds * 0.15)

    def sleep(self, seconds):
        """Wait inside the budget, returning whether the wait completed."""
        if seconds <= 0:
            return True
        allowed = min(seconds, self.left())
        if allowed <= 0:
            return False
        time.sleep(allowed)
        return not self.expired()

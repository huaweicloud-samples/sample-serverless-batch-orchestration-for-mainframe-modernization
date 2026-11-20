"""Entry point of the orchestration function.

One invocation of this function is one pass of a batch run: it reads the state
of the runs that are open, moves each of them as far as its time budget allows,
writes the state back and answers with what it did. The chain itself - a first
step that normalises the arriving extract, two steps that work on its output at
the same time, a last step that combines what those two produced - is not
executed inside one call: it is executed across the passes of a short schedule,
which is what keeps every call inside the time limit of a function.

The four actions a pass can be asked to do:

    start_run       open today's run, gate it on the business day, start the chain
    resume_runs     advance whichever runs are in flight
    run_regression  run the named modules on their own, inside an isolated
                    namespace and without the business day gate
    job_event       a container reported itself as finished: advance its run

What the pass does for each open run, in order:

* asks the validation function whether the inputs of a step are ready
* creates the batch container for the step in the cluster
* polls the job until the container reports an exit code
* routes that exit code: zero opens the steps behind it, anything else stops
  the chain and says which step stopped it
* when the run has no step left, records the verdict, publishes the run
  counters and sends the failure notice

Everything that has to survive from one pass to the next is in the checkpoint
object of the run, so a timeout, a restart or a redeploy loses nothing.

Failures are reported, never absorbed:

* a container that exits non-zero, a validation function that cannot be
  reached, a cluster call that keeps being refused and a job that stops
  reporting all end as a failed step carrying the reason and, where the
  cluster has one, the tail of the container log
* a transient cluster or invocation refusal is retried under the retry policy
  of the deployment; a step that exhausts it fails with the last refusal
* a failure notice the notification service refuses does not change the verdict
  of the run - the verdict is already written - but it is carried back in the
  answer under ``notification``
"""

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from batchcore.checks import file_check
from batchcore.orchestrator import events, plan, state
from batchcore.shared import config, credentials, logs, obs_store
from batchcore.shared.hwc import http, services

log = logs.get("orchestrator-index")

# The three counters the alarms of the deployment read. A finished run reports
# one of them plus how long it took; a run that ends because the day is not a
# processing day reports nothing, so it cannot raise an alarm.
METRIC_SUCCESS = "batch_run_success"
METRIC_FAILURES = "batch_run_failed"
METRIC_DURATION = "batch_run_duration"

# Why a run stopped, most attention deserving first. A genuine container
# failure is never reported as a missing input file behind it.
VERDICT_PRIORITY = (
    "BATCH_JOB_FAILED",
    "BATCH_JOB_TIMEOUT",
    "CLUSTER_CALL_FAILED",
    "JOB_NOT_FOUND",
    "CHECK_INVOCATION_FAILED",
    "CHECK_NOT_WIRED",
    "ANSWER_UNREADABLE",
    file_check.MISSING,
    file_check.TOO_OLD,
    file_check.INVALID,
    file_check.UNREADABLE,
)

# Reasons the platform gives for a job that never produced an exit code. They
# say nothing about the data, so the step is retried rather than failed.
TRANSIENT_REASONS = ("DeadlineExceeded", "DeadlineExceededSilent", "PodEvicted")

TAIL_LINES = 40
TAIL_CHARACTERS = 2000


def handler(event, context):
    """One pass of the batch workflow.

    The event may be a schedule wrapper, the payload an operator typed into
    the console, a notice about a finished container or a bare JSON document in
    a string; :func:`batchcore.orchestrator.events.parse` reads all of them and
    an event it cannot read becomes a resume pass, which is the pass that opens
    nothing and therefore the safe default.

    The answer describes the pass:

        {
          "action": "start_run", "kind": "scheduled",
          "passBudgetSeconds": 900, "passElapsedSeconds": 41.2,
          "budgetSpent": false,
          "runs": [
            {"runId": "batch-2026-10-26", "status": "SUCCEEDED",
             "errorType": null, "phases": {"JOB1": "succeeded", ...},
             "checkpoint": "runs/batch-2026-10-26.json", ...}
          ]
        }
    """
    logs.entry("orchestrator handler")
    creds = credentials.resolve(context)
    if creds is None:
        raise RuntimeError("orchestrator: %s" % obs_store.config_error())
    request = events.parse(event, context)
    log.info(
        "pass opened: action=%s kind=%s run=%s date=%s steps=%s",
        request.action,
        request.kind,
        request.run_id,
        request.run_date,
        list(request.steps),
    )
    return Engine(creds, context=context).handle(request)


class Engine:
    """One pass, with every contact point it uses handed in or built once.

    A test gives it stand-ins for the cluster, the validation functions, the
    data lake and the two publishing calls, and drives the whole flow of a run
    through the same code a live pass executes.
    """

    def __init__(
        self,
        creds,
        context=None,
        store=None,
        cluster=None,
        invoker=None,
        notifier=None,
        reporter=None,
        budget=None,
    ):
        self.creds = creds
        self.project = credentials.project(context)
        self._store = store
        self._cluster = cluster
        self.budget = budget
        self._invoker = invoker or self._invoke
        self._notifier = notifier or self._notify
        self._reporter = reporter or self._report
        # Side effects of the closing step, kept per run for the answer.
        self.receipts = {}

    # -- contact points ---------------------------------------------------

    @property
    def store(self):
        """The data lake of the deployment."""
        if self._store is None:
            self._store = obs_store.connect(self.creds)
        return self._store

    @property
    def cluster(self):
        """The cluster the batch containers run in."""
        if self._cluster is None:
            self._cluster = Cluster.from_environment(self.store)
        return self._cluster

    @property
    def checkpoints(self):
        return state.Checkpoints(self.store)

    def _invoke(self, urn, payload):
        return services.invoke(urn, payload, self.creds, project=self.project)

    def _notify(self, topic, subject, message):
        return services.notify(topic, subject, message, self.creds, project=self.project)

    def _report(self, points):
        return services.report(points, self.creds, project=self.project)

    def _retry(self, action):
        """A call that may be refused by the platform rather than by the data."""
        return http.with_retries(
            action,
            config.integer(config.RETRY_MAX_ATTEMPTS, 2),
            config.integer(config.RETRY_BASE_SECONDS, 10),
            sleep=self._wait,
        )

    def _wait(self, seconds):
        """Sleep inside the budget of this pass."""
        if self.budget is None:
            time.sleep(seconds)
            return
        self.budget.sleep(seconds)

    # -- one pass ---------------------------------------------------------

    def handle(self, request):
        if self.budget is None:
            self.budget = state.Budget(request.budget)
        started = time.time()
        runs = self._runs_for(request)
        reports = [self.advance(run) for run in runs]
        answer = {
            "action": request.action,
            "kind": request.kind,
            "passBudgetSeconds": self.budget.seconds,
            "passElapsedSeconds": round(time.time() - started, 1),
            "budgetSpent": self.budget.expired(),
            "runs": [report for report in reports if report],
        }
        unknown = _unknown_steps(request)
        if unknown:
            answer["rejected"] = unknown
        return answer

    def _runs_for(self, request):
        if request.action == events.ACTION_START:
            return [self.open_run(request)]
        if request.action == events.ACTION_REGRESSION:
            return [self.open_regression(request)]
        if request.action == events.ACTION_JOB_EVENT:
            if not request.run_id:
                log.warning("a container notice named no run; the pass resumes in flight runs")
                return self.checkpoints.open_runs()
            run = self.checkpoints.load(request.run_id)
            if run is None:
                log.warning("no run is open under %s, notice ignored", request.run_id)
                return []
            return [run]
        return self.checkpoints.open_runs()

    # -- opening a run ----------------------------------------------------

    def open_run(self, request):
        """Open the run of a scheduled or manually replayed day.

        The identifier is the day, so a second launch attempt on the same day
        continues the run it already opened instead of opening a second one
        over the same data.
        """
        run_id = request.run_id or "%s-%s" % ("batch", request.run_date)
        existing = self.checkpoints.load(run_id)
        if existing is not None and existing.status == state.RUN_RUNNING:
            log.info("run %s is already open; this pass continues it", run_id)
            return existing

        run = state.RunState(
            run_id,
            request.kind,
            request.run_date,
            isolation=config.isolation_prefix(request.run_id or ""),
            budget=int(self.budget.seconds),
        )
        gate = self.business_day_gate(run, request)
        if isinstance(gate, tuple):
            return self._aborted(run, gate[0], gate[1])
        if gate is not None and not gate.get("isBusinessDay"):
            run.status = state.RUN_SKIPPED
            run.message = "Nothing runs on %s: %s." % (run.run_date, gate.get("reason"))
            log.info("run %s ends here: %s", run_id, run.message)
            self.checkpoints.save(run)
            return run

        chosen = plan.plan()
        run.plan = chosen.summary()
        for step in chosen:
            run.begin(step.identifier)
        self.checkpoints.save(run)
        log.info("run %s opened: %s", run_id, run.plan["shape"])
        return run

    def open_regression(self, request):
        """Open an isolated run of the named modules.

        The modules of a regression run are executed on their own, in any
        order, and the business day gate does not apply to them.
        """
        if not request.steps:
            raise ValueError(
                "orchestrator: a regression pass has to name the modules it runs. "
                "Known modules: %s." % ", ".join(plan.BY_ID)
            )
        unknown = _unknown_steps(request)
        if unknown:
            raise ValueError(
                "orchestrator: no batch program is known for %s. Known modules: %s."
                % (", ".join(unknown), ", ".join(plan.BY_ID))
            )
        derived = config.isolation_prefix(request.run_id)
        if request.isolation and request.isolation != derived:
            raise ValueError(
                "orchestrator: an isolated run lives under %s, named by its run "
                'identifier. Pass {"runId": "<name>"} instead of a data prefix.'
                % derived
            )

        run_id = request.run_id
        existing = self.checkpoints.load(run_id)
        if existing is not None and existing.status == state.RUN_RUNNING:
            log.info("run %s is already open; this pass continues it", run_id)
            return existing

        chosen = plan.plan(
            request.steps,
            gated=False,
            concurrency=config.integer(config.REGRESSION_CONCURRENCY, 3),
        )
        run = state.RunState(
            run_id,
            request.kind,
            request.run_date,
            isolation=derived,
            budget=int(self.budget.seconds),
        )
        run.plan = chosen.summary()
        for step in chosen:
            run.begin(step.identifier)
        self.checkpoints.save(run)
        log.info("regression run %s opened under %s", run_id, derived)
        return run

    def _aborted(self, run, error_type, detail):
        """A gate that could not answer closes the run with the reason for it.

        A missing answer is never read as a non processing day: the run is
        reported as failed, naming what could not be reached, so the next pass
        does not silently skip a chain nobody approved.
        """
        run.status = state.RUN_FAILED
        run.error_type = error_type
        run.message = detail
        self.checkpoints.save(run)
        log.error("run %s opened nowhere: %s - %s", run.run_id, error_type, detail)
        return run

    def business_day_gate(self, run, request):
        """The answer of the calendar gate, an error pair, or ``None`` when no
        gate applies to this kind of run."""
        urn = config.text(config.CALENDAR_CHECK_URN)
        if not urn:
            log.error("run %s: no calendar gate has been named to this function", run.run_id)
            return ("CHECK_NOT_WIRED", "the calendar gate is not wired to the run")
        # A scheduled pass asks about the instant it fired at, resolved to the
        # time zone of the schedule by the gate itself. A replay asks about the
        # calendar day the operator named.
        payload = {"runDate": run.run_date} if request.kind == events.MANUAL else {"date": _instant()}
        return self._ask(urn, payload, run, "calendar")

    # -- advancing a run --------------------------------------------------

    def advance(self, run):
        """Move one run as far as this pass may, then report where it stands."""
        if run is None:
            return None
        if run.status != state.RUN_RUNNING:
            return self._report_of(run)

        chosen = self._plan_of(run)
        while True:
            self._reap(run)
            self._propagate(run, chosen)
            self._launch_ready(run, chosen)
            self.checkpoints.save(run)
            if not run.unfinished:
                break
            if self.budget.expired():
                log.info(
                    "run %s: the pass is out of time, the next scheduled pass continues it",
                    run.run_id,
                )
                break
            if not self._idle_wait(run):
                log.info("run %s: no step could be advanced in this pass", run.run_id)
                break

        self._close(run)
        return self._report_of(run)

    def _plan_of(self, run):
        """Rebuild the step set of an open run from its checkpoint."""
        recorded = run.plan or {}
        identifiers = [name for name in (recorded.get("steps") or []) if name in plan.BY_ID]
        if not identifiers:
            identifiers = [step.identifier for step in plan.CHAIN]
        return plan.Plan(
            [plan.BY_ID[name] for name in identifiers],
            gated=bool(recorded.get("gated", True)),
            concurrency=recorded.get("concurrency"),
        )

    def _reap(self, run):
        """Ask the cluster what the running steps of this run did."""
        for step in list(run.running):
            verdict = self._retry(lambda: self.cluster.observe(run.run_id, step.identifier, step.job))
            phase = verdict.get("phase")
            if phase == "succeeded":
                log.info("run %s step %s: exit code 0", run.run_id, step.identifier)
                run.record(
                    step,
                    state.SUCCEEDED,
                    exit_code=0,
                    error_type=None,
                    detail=verdict.get("detail"),
                )
            elif phase == "failed":
                self._account_for(run, step, verdict)
            elif phase == "absent":
                log.error(
                    "run %s step %s: job %s is no longer reporting anything",
                    run.run_id,
                    step.identifier,
                    step.job,
                )
                run.record(
                    step,
                    state.FAILED,
                    error_type="JOB_NOT_FOUND",
                    detail="job %s is gone and the run does not know how it ended" % step.job,
                )

    def _account_for(self, run, step, verdict):
        """Route the end of a container: a program answer fails the step, a
        platform answer is worth another attempt."""
        reason = verdict.get("reason") or ""
        code = verdict.get("exitCode")
        detail = verdict.get("detail") or reason
        if reason in TRANSIENT_REASONS:
            self._defer_or_fail(run, step, "BATCH_JOB_TIMEOUT", detail or reason)
            return
        if code is None:
            self._defer_or_fail(run, step, "CLUSTER_CALL_FAILED", detail or "no exit code reported")
            return
        log.error(
            "run %s step %s: container %s exited with code %s",
            run.run_id,
            step.identifier,
            step.job,
            code,
        )
        run.record(
            step,
            state.FAILED,
            exit_code=code,
            error_type="BATCH_JOB_FAILED",
            detail=_clip("%s / %s" % (reason, detail)) if reason else detail,
        )

    def _defer_or_fail(self, run, step, error_type, detail):
        """Retry a step that the platform refused, or give up on it."""
        attempts = config.integer(config.RETRY_MAX_ATTEMPTS, 2)
        if step.attempts >= attempts:
            log.error(
                "run %s step %s: %s after %s attempt(s): %s",
                run.run_id,
                step.identifier,
                error_type,
                step.attempts,
                detail,
            )
            run.record(step, state.FAILED, error_type=error_type, detail=detail)
            return
        delay = config.integer(config.RETRY_BASE_SECONDS, 10)
        retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        log.warning(
            "run %s step %s: %s, attempt %s of %s no earlier than %s: %s",
            run.run_id,
            step.identifier,
            error_type,
            step.attempts + 1,
            attempts,
            retry_at.isoformat(timespec="seconds"),
            detail,
        )
        run.record(
            step,
            state.WAITING,
            error_type=error_type,
            detail=detail,
            retry_at=retry_at.isoformat(timespec="seconds"),
        )

    def _propagate(self, run, chosen):
        """A step whose chain no longer reaches it is reported, not left waiting."""
        for identifier in chosen.identifiers:
            step = run.step(identifier) or run.begin(identifier)
            if step.phase != state.PENDING:
                continue
            for dependency in chosen.dependencies(identifier):
                behind = run.step(dependency)
                if behind is None or not behind.terminal:
                    continue
                if behind.phase == state.SUCCEEDED:
                    continue
                log.info(
                    "run %s step %s does not run: %s is %s",
                    run.run_id,
                    identifier,
                    dependency,
                    behind.phase,
                )
                run.record(
                    step,
                    state.SKIPPED,
                    error_type=behind.error_type or "UPSTREAM_STEP_SKIPPED",
                    detail="step %s did not produce its output" % dependency,
                )
                break

    def _launch_ready(self, run, chosen):
        """Start every step whose inputs are ready and whose place in the chain
    has come."""
        for identifier in chosen.identifiers:
            step = run.step(identifier) or run.begin(identifier)
            if step.phase == state.WAITING:
                if self._retry_due(step):
                    self._launch(run, identifier, step, gated=False)
                continue
            if step.phase != state.PENDING:
                continue
            if not self._dependencies_met(run, chosen, identifier):
                continue
            if chosen.slots_free(len(run.running)) <= 0:
                log.info(
                    "run %s: %s is ready but %s steps are already running",
                    run.run_id,
                    identifier,
                    len(run.running),
                )
                return
            self._launch(run, identifier, step, gated=True)

    def _dependencies_met(self, run, chosen, identifier):
        for dependency in chosen.dependencies(identifier):
            behind = run.step(dependency)
            if behind is None or behind.phase != state.SUCCEEDED:
                return False
        return True

    def _retry_due(self, step):
        raw = str(step.retry_at or "")
        try:
            moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return True
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment <= datetime.now(timezone.utc)

    def _launch(self, run, identifier, step, gated):
        """Ask whether the step may read its inputs, then start its container."""
        if gated:
            answer = self.input_gate(identifier, run)
            if isinstance(answer, tuple):
                error_type, detail = answer
                run.record(
                    step,
                    state.FAILED,
                    error_type=error_type,
                    detail=detail,
                    attempts=step.attempts + 1,
                )
                return False
            if not answer.get("allFilesPresent"):
                log.info(
                    "run %s step %s waits on its inputs: %s",
                    run.run_id,
                    identifier,
                    answer.get("errorType"),
                )
                run.record(
                    step,
                    state.SKIPPED,
                    error_type=answer.get("errorType") or file_check.MISSING,
                    detail=answer.get("message"),
                    check=_trim_check(answer),
                    attempts=step.attempts + 1,
                )
                return False
            step.check = _trim_check(answer)

        name = state.job_name(run.run_id, identifier)
        attempts = step.attempts + 1
        try:
            self._retry(lambda: self.cluster.create_job(self.job_body(run, identifier, name)))
        except http.ApiError as error:
            self._defer_or_fail(run, step, "CLUSTER_CALL_FAILED", str(error))
            return False
        log.info(
            "run %s step %s: job %s started (attempt %s)",
            run.run_id,
            identifier,
            name,
            attempts,
        )
        run.record(
            step,
            state.RUNNING,
            job=name,
            attempts=attempts,
            started_at=state.now(),
            error_type=None,
            detail=None,
            retry_at=None,
        )
        return True

    def input_gate(self, identifier, run):
        """The answer of the validation function for one step."""
        urn = config.text(config.FILE_CHECK_URN)
        if not urn:
            log.error("run %s: no validation function has been named", run.run_id)
            return ("CHECK_NOT_WIRED", "the input gate is not wired to the run")
        payload = {"jobName": identifier}
        if run.isolation:
            # An isolated run is asked about its own namespace, which the
            # validation function derives from the run identifier.
            payload["runId"] = run.run_id
        return self._ask(urn, payload, run, "input")

    def _ask(self, urn, payload, run, what):
        """Invoke one function of the deployment and insist on an answer.

        An unreachable function and a function that answers without the field
        the flow routes on are both reported as a failed step: neither is a
        verdict about the data, and guessing one would either stop a healthy
        chain or start a broken one.
        """
        try:
            answer = self._retry(lambda: self._invoker(urn, payload))
        except http.ApiError as error:
            log.error("run %s: the %s gate could not be reached: %s", run.run_id, what, error)
            return ("CHECK_INVOCATION_FAILED", str(error))
        field = "isBusinessDay" if "date" in payload or "runDate" in payload else "allFilesPresent"
        if not isinstance(answer, dict) or field not in answer:
            log.error(
                "run %s: the %s gate answered without %s: %r",
                run.run_id,
                what,
                field,
                answer,
            )
            return ("ANSWER_UNREADABLE", "the %s gate answered %r" % (what, answer))
        return answer

    def _idle_wait(self, run):
        """Wait for something to happen, inside the budget of this pass.

        ``False`` means there is nothing this pass can still wait for: no step
        is running, none is waiting for a retry and none started.
        """
        interval = config.integer(config.POLL_INTERVAL_SECONDS, 10)
        if run.waiting:
            delays = [0.0 if self._retry_due(step) else interval for step in run.waiting]
            return self.budget.sleep(min(delays))
        if run.running:
            return self.budget.sleep(interval)
        return False

    # -- closing a run ----------------------------------------------------

    def _close(self, run):
        """Record the verdict of a finished run, then tell whoever needs to
        know."""
        if run.unfinished or run.status != state.RUN_RUNNING:
            self.checkpoints.save(run)
            return
        failures = [step for step in run.steps.values() if step.phase == state.FAILED]
        skipped = [step for step in run.steps.values() if step.phase == state.SKIPPED]
        if failures:
            error_type = _first_verdict(failures)
            run.status = state.RUN_FAILED
            run.error_type = error_type
            run.message = "%d step(s) failed: %s" % (len(failures), _steps_of(failures))
        elif skipped:
            error_type = _first_verdict(skipped)
            run.status = state.RUN_FAILED
            run.error_type = error_type
            run.message = "%d step(s) never started: %s" % (len(skipped), _steps_of(skipped))
        else:
            run.status = state.RUN_SUCCEEDED
            run.error_type = None
            run.message = "Every step of %s finished." % run.run_id
        self.checkpoints.save(run)
        log.info(
            "run %s closed: %s%s",
            run.run_id,
            run.status,
            " (%s)" % run.error_type if run.error_type else "",
        )
        if run.status == state.RUN_FAILED:
            self.receipts[run.run_id] = {"notification": self._announce(run)}
        self.receipts.setdefault(run.run_id, {})["metrics"] = self._publish(run)

    def _announce(self, run):
        """Send the failure notice. A refused notice is carried back in the
        answer rather than changing the verdict of a run that already failed."""
        topic = config.text(config.NOTIFICATION_TOPIC_URN)
        if not topic:
            log.info("no notification topic configured, nothing announced for run %s", run.run_id)
            return {"skipped": "no notification topic configured"}
        workflow = config.text(config.WORKFLOW_DEFINITION) or "batch-orchestrator"
        subject = "%s: run %s failed (%s)" % (workflow, run.run_id, run.error_type)
        body = {
            "workflow": workflow,
            "runId": run.run_id,
            "kind": run.kind,
            "runDate": run.run_date,
            "status": run.status,
            "errorType": run.error_type,
            "message": run.message,
            "phases": run.phases(),
            "steps": {
                key: {
                    "phase": value.phase,
                    "errorType": value.error_type,
                    "exitCode": value.exit_code,
                    "job": value.job,
                    "attempts": value.attempts,
                    "detail": value.detail,
                }
                for key, value in sorted(run.steps.items())
            },
        }
        try:
            self._notifier(topic, subject, body)
        except http.ApiError as error:
            log.error("run %s: the failure notice was refused: %s", run.run_id, error)
            return {"rejected": str(error)}
        log.info("run %s: failure announced on the notification topic", run.run_id)
        return {"published": topic}

    def _publish(self, run):
        """Report the run counters. Skipped days report nothing, so they cannot
        raise the alarm a failure is meant to raise."""
        if not config.flag(config.ENABLE_CUSTOM_METRICS, True):
            return {"skipped": "custom counters are turned off"}
        if run.status == state.RUN_SKIPPED:
            return {"skipped": "a non processing day reports no counters"}
        points = {
            METRIC_SUCCESS if run.status == state.RUN_SUCCEEDED else METRIC_FAILURES: 1,
            METRIC_DURATION: max(0, _elapsed(run)),
        }
        return self._reporter(points)

    def _report_of(self, run):
        extras = self.receipts.get(run.run_id, {})
        return {
            "runId": run.run_id,
            "kind": run.kind,
            "runDate": run.run_date,
            "status": run.status,
            "errorType": run.error_type,
            "message": run.message,
            "phases": run.phases(),
            "unfinished": [step.identifier for step in run.unfinished],
            "checkpoint": self.checkpoints.path(run.run_id),
            "notification": extras.get("notification"),
            "counters": extras.get("metrics"),
        }

    # -- the container a step runs in -------------------------------------

    def job_body(self, run, identifier, name):
        """The job specification of one step of one run.

        The same fields as the template of the batch job, so a run started by
        hand and a run started by this function describe the same pod. Only the
        values that change per run are written here: the namespace the run
        reads and writes, the run it belongs to and the program to run.
        """
        namespace = config.text(config.K8S_NAMESPACE, "batch-jobs")
        container = {
            "name": config.text(config.JOB_CONTAINER_NAME, "batch-job"),
            "image": config.text(config.BATCH_IMAGE),
            "imagePullPolicy": "IfNotPresent",
            "args": [identifier],
            "env": [
                {"name": "OBS_PREFIX", "value": run.isolation},
                {"name": "RUN_ID", "value": run.run_id},
            ],
            "envFrom": [
                {"configMapRef": {"name": config.text(config.BATCH_CONFIGMAP, "batch-config")}},
                {"secretRef": {"name": "batch-obs-credentials", "optional": True}},
            ],
            "resources": {
                "requests": {
                    "cpu": config.text(config.JOB_CPU_REQUEST, "500m"),
                    "memory": config.text(config.JOB_MEMORY_REQUEST, "1Gi"),
                },
                "limits": {
                    "cpu": config.text(config.JOB_CPU_LIMIT, "1"),
                    "memory": config.text(config.JOB_MEMORY_LIMIT, "2Gi"),
                },
            },
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 10001,
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
            },
            "readOnlyRootFilesystem": False,
        }
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": {
                    "batch.job/run-id": run.run_id,
                    "batch.job/task": identifier,
                    "app.kubernetes.io/name": "batch-stub",
                },
            },
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": config.integer(config.JOB_DEADLINE_SECONDS, 7200),
                "ttlSecondsAfterFinished": config.integer(config.JOB_TTL_SECONDS, 3600),
                "template": {
                    "metadata": {
                        "labels": {"batch.job/run-id": run.run_id, "batch.job/task": identifier}
                    },
                    "spec": {
                        "serviceAccountName": config.text(config.JOB_SERVICE_ACCOUNT, "batch-job-runner"),
                        "automountServiceAccountToken": False,
                        "restartPolicy": "Never",
                        "containers": [container],
                        "securityContext": {"fsGroup": 10001},
                    },
                },
            },
        }


class ClusterError(http.ApiError):
    """A cluster call that was refused, or one that never got there.

    Built on the shared error type so that the retry policy of the deployment
    treats a refused cluster call and a refused service call alike.
    """

    def __init__(self, message, status=None, reason=None, body=None):
        super().__init__(message, status=status, code=reason)
        self.body = body


class Cluster:
    """The handful of cluster calls one orchestration pass makes.

    Bearer access to the API server of the cluster with the long lived token of
    the orchestration identity, whose reach is limited to jobs and pods of the
    batch namespace. Only the four calls the flow needs are here: create a job,
    read one back, read the pods it started and read the tail of a container log.

    ``transport`` replaces the network for a test that wants to drive the flow
    against recorded answers.
    """

    TIMEOUT_SECONDS = 20

    def __init__(self, server, namespace, token, ca=None, transport=None, timer=time.time):
        self.server = server.rstrip("/")
        self.namespace = namespace
        self.token = token
        self.ca = ca
        self.transport = transport or self._send
        self.timer = timer
        self._context = None

    @classmethod
    def from_environment(cls, store=None):
        """Bind to the cluster the deployment was stood up with, or say which
        of its addresses was never named to the function."""
        missing = [
            name
            for name in (config.text(config.K8S_API_SERVER), config.text(config.K8S_SA_TOKEN))
            if not name
        ]
        named = [
            name
            for name, present in (
                (config.K8S_API_SERVER, bool(config.text(config.K8S_API_SERVER))),
                (config.K8S_SA_TOKEN, bool(config.text(config.K8S_SA_TOKEN))),
                (config.BATCH_IMAGE, bool(config.text(config.BATCH_IMAGE))),
            )
            if not present
        ]
        if named:
            raise ClusterError(
                "the batch containers cannot be started: %s was never named to this "
                "function. Fill it in after the cluster exists and re-run the "
                "deployment." % ", ".join(named)
            )
        del missing, store
        return cls(
            config.text(config.K8S_API_SERVER),
            config.text(config.K8S_NAMESPACE, "batch-jobs"),
            config.text(config.K8S_SA_TOKEN),
            ca=config.text(config.K8S_CA_CERT),
        )

    # -- reading a job ----------------------------------------------------

    def observe(self, run_id, identifier, name):
        """What became of the job of one step: succeeded, failed, still
        running, or no longer reporting."""
        job = self.job(name)
        if job is None:
            return {"phase": "absent"}
        status = job.get("status") or {}
        conditions = {
            str(item.get("type")): item for item in (status.get("conditions") or []) if item
        }
        if status.get("succeeded"):
            return {"phase": "succeeded", "exitCode": 0}
        failure = conditions.get("Failed")
        if failure:
            reason = str(failure.get("reason") or "")
            code, detail = self._pod_failure(run_id, identifier)
            return {"phase": "failed", "reason": reason, "exitCode": code, "detail": detail}
        if reason_is_overdue(status, self.timer()):
            return {
                "phase": "failed",
                "reason": "DeadlineExceeded",
                "exitCode": None,
                "detail": "the job ran past its active deadline",
            }
        return {"phase": "running"}

    def _pod_failure(self, run_id, identifier):
        """The exit code of the container that ended, and the end of its log."""
        pods = self.pods(run_id, identifier)
        for pod in pods:
            for entry in _container_states(pod):
                if entry.get("exitCode") is None:
                    continue
                name = entry.get("name") or ""
                return entry.get("exitCode"), self._tail(pod.get("metadata", {}).get("name"), name)
        return None, None

    # -- the calls --------------------------------------------------------

    def create_job(self, body):
        try:
            return self._call("POST", self._jobs_path(), body=body)
        except ClusterError as error:
            if error.status == 409:
                # The job of this step is already there: a pass that restarted
                # after writing the specification continues it.
                log.info("job %s already exists, continuing it", body["metadata"]["name"])
                return body
            raise

    def job(self, name):
        """The job object, or ``None`` when the cluster no longer holds it."""
        try:
            return self._call("GET", "%s/%s" % (self._jobs_path(), name))
        except ClusterError as error:
            if error.status == 404:
                return None
            raise

    def pods(self, run_id, identifier):
        path = "/api/v1/namespaces/%s/pods" % urllib.parse.quote(self.namespace, safe="")
        answer = self._call(
            "GET",
            path,
            query=(("labelSelector", "batch.job/run-id=%s,batch.job/task=%s" % (run_id, identifier)),),
        )
        return list((answer or {}).get("items") or [])

    def _tail(self, pod, container):
        if not pod:
            return None
        path = "/api/v1/namespaces/%s/pods/%s/log" % (
            urllib.parse.quote(self.namespace, safe=""),
            urllib.parse.quote(pod, safe=""),
        )
        query = [("tailLines", TAIL_LINES)]
        if container:
            query.append(("container", container))
        try:
            raw = self._call("GET", path, query=tuple(query))
        except ClusterError as error:
            log.warning("log of pod %s could not be read: %s", pod, error)
            return None
        text = raw if isinstance(raw, str) else json.dumps(raw)
        return text[-TAIL_CHARACTERS:]

    def _jobs_path(self):
        return "/apis/batch/v1/namespaces/%s/jobs" % urllib.parse.quote(self.namespace, safe="")

    def _call(self, method, path, body=None, query=()):
        if not self.token:
            raise ClusterError(
                "no token for the cluster: the orchestration identity of the deployment "
                "was never named to this function"
            )
        headers = {"Authorization": "Bearer %s" % self.token, "Accept": "application/json"}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        return self.transport(method, self.server + path, headers, payload, tuple(query))

    # -- the network ------------------------------------------------------

    def _send(self, method, url, headers, payload, query):
        rendered = urllib.parse.urlencode(list(query))
        if rendered:
            url = "%s?%s" % (url, rendered)
        call = urllib.request.Request(url, data=payload, method=method.upper())
        for name, value in headers.items():
            call.add_header(name, str(value))
        try:
            with urllib.request.urlopen(call, timeout=self.TIMEOUT_SECONDS, context=self.tls()) as r:
                return http.decode(r.read(), r.headers)
        except urllib.error.HTTPError as error:
            info = http.decode(error.read(), error.headers)
            message = None
            if isinstance(info, dict):
                message = info.get("message") or info.get("reason")
            raise ClusterError(
                "%s %s refused: %s" % (method.upper(), url, message or error.reason),
                status=error.code,
                reason=(info or {}).get("reason") if isinstance(info, dict) else None,
                body=info,
            ) from error
        except urllib.error.URLError as error:
            raise ClusterError("%s %s did not reach the cluster: %s" % (method, url, error.reason)) from error

    def tls(self):
        """The trust anchor of the API server.

        The certificate the cluster serves is named for the address the
        deployment hands to this function, so the address has to be used as it
        is written in the cluster outputs.
        """
        if self._context is None:
            context = ssl.create_default_context()
            bundle = _pem(self.ca)
            if bundle:
                context.load_verify_locations(cadata=bundle)
            self._context = context
        return self._context


# -- small readers ---------------------------------------------------------


def _instant():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _unknown_steps(request):
    """Modules the pass named for which no batch program is known.

    The plan builder drops an identifier nothing answers to; reporting the
    dropped names is what keeps a misspelled module from silently opening an
    empty run that closes as a success.
    """
    return [name for name in (request.steps or ()) if name not in plan.BY_ID]


def _pem(value):
    """A certificate bundle out of a configuration value.

    A bundle written on one line with escaped newlines is a normal thing to
    find in a function input, so it is put back into the shape the trust store
    reads.
    """
    raw = str(value or "").strip().strip('"')
    if not raw:
        return None
    return raw.replace("\\n", "\n").replace("\\r", "\r")


def reason_is_overdue(status, moment):
    """Whether a job has run past the deadline the cluster was told to enforce.

    The platform enforces the deadline itself; this is the guard for the case
    where it does not, so that a run cannot wait on a container nobody is
    watching.
    """
    started = status.get("startTime")
    if not started:
        return False
    try:
        opened = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
    except ValueError:
        return False
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)
    deadline = config.integer(config.JOB_DEADLINE_SECONDS, 7200)
    return (datetime.fromtimestamp(moment, timezone.utc) - opened).total_seconds() > deadline + 120


def _container_states(pod):
    """Every terminated container state a pod reports, newest first."""
    status = (pod or {}).get("status") or {}
    entries = []
    for source in (status.get("containerStatuses") or [], status.get("initContainerStatuses") or []):
        for entry in source:
            terminated = (entry or {}).get("state", {}).get("terminated")
            if terminated:
                merged = dict(entry)
                merged["exitCode"] = terminated.get("exitCode")
                merged["reason"] = terminated.get("reason")
                merged["finishedAt"] = terminated.get("finishedAt")
                entries.append(merged)
    entries.sort(key=lambda item: str(item.get("finishedAt") or ""), reverse=True)
    return entries


def _first_verdict(steps):
    for wanted in VERDICT_PRIORITY:
        for step in steps:
            if step.error_type == wanted:
                return wanted
    return steps[0].error_type or "BATCH_JOB_FAILED"


def _steps_of(steps):
    return ", ".join(
        "%s (%s%s)"
        % (
            step.identifier,
            step.error_type,
            ", exit code %s" % step.exit_code if step.exit_code is not None else "",
        )
        for step in sorted(steps, key=lambda item: item.identifier)
    )


def _trim_check(answer):
    """The gate answer kept in the checkpoint: the verdict and why, not every
    listed object."""
    if not isinstance(answer, dict):
        return None
    return {
        key: answer.get(key)
        for key in ("step", "allFilesPresent", "errorType", "message", "missingCount", "maxAgeDays")
    }


def _clip(value):
    text = str(value or "")
    return text[-TAIL_CHARACTERS:] if len(text) > TAIL_CHARACTERS else text or None


def _elapsed(run):
    """Seconds between the moment the run was opened and now."""
    raw = str(run.started_at or "")
    try:
        opened = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)
    return int((datetime.now(timezone.utc) - opened).total_seconds())

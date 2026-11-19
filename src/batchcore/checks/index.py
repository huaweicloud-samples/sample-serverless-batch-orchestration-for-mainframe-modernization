"""Entry point of the two validation functions.

Both handlers are exported by this one module, because the deployment puts the
two answers into one code archive and points each function at a different name
inside it:

    index.handler_file_check       may this batch step read its inputs now?
    index.handler_calendar_check   does this launch fall on a processing day?

The judgement itself lives in :mod:`batchcore.checks.file_check` and
:mod:`batchcore.checks.calendar_check`. This module only does the three things
a call needs before that judgement can be trusted:

* it reads a request out of whatever wrapper the call arrived in, and refuses
  an event that is not a request at all
* it confirms the data lake the answer is about has been named to it
* it hands the call the identity the platform issued for it

What is answered and what is raised is deliberate:

* a step name nothing is configured for, and an input that is absent or older
  than the allowed window, come back as a definite result. They are ordinary
  outcomes the orchestration layer routes on, and an operator asks the same
  two questions by hand.
* a request that cannot be read, a bucket that was never named, an identity
  that was never issued and a lake that refuses to answer come back as an
  error. None of them is a verdict about the data, and reporting one as
  though it were would either stop a healthy chain or start a broken one.
"""

import json

from batchcore.checks import calendar_check, file_check
from batchcore.shared import config, credentials, logs, obs_store

log = logs.get("checks-index")

# Fields a wrapper can hide the actual request inside. A schedule pass writes
# its payload into one of them, the console test box writes the request
# straight, and an operator may paste either shape.
WRAPPERS = ("data", "user_event", "additional_information", "detail", "Message", "message")


def handler_file_check(event, context):
    """Are the inputs of one batch step present, and recent where recency is
    what makes them belong to this run?

    The request is a mapping naming the step. Any of these names it:

        {"jobName": "JOB1"}
        {"jobName": "Check_JOB2A_Input_Files"}
        {"step": "job3", "runId": "night-7"}

    ``runId`` moves the question into the namespace of an isolated run; without
    it the production prefixes of the deployment are checked.

    The answer is the structure ``file_check`` builds: ``allFilesPresent``, an
    ``errorType`` when it is not present, and one entry per input folder
    saying what was looked for and what was found.
    """
    logs.entry("handler_file_check")
    request = read_event(event, "handler_file_check")
    store = lake(context, "handler_file_check")
    answer = file_check.run(request, store)
    log.info(
        "file check answered: step=%s present=%s errorType=%s",
        answer.get("step"),
        answer.get("allFilesPresent"),
        answer.get("errorType"),
    )
    return answer


def handler_calendar_check(event, context):
    """Does the launch of a run fall on a day the business processes on?

    The request may carry the moment the decision is taken for:

        {}                                     the current schedule instant
        {"runDate": "2026-11-14"}              one named calendar day
        {"date": "2026-11-14T02:00:00Z"}       one named instant, as a schedule
                                               pass reports it

    The answer is ``isBusinessDay`` with the date it was taken for, the time
    zone it was resolved in and the reason: the weekend, a date the calendar
    names, a business day, or a business day under a weekend rule alone.

    Unlike the file check, this question is answerable without the data lake:
    the calendar object is an addition to the weekend rule, so an unavailable
    identity or an absent object leaves the weekend rule standing on its own.
    That is the state a fresh deployment starts in, not a fault, so it is
    reported inside the answer rather than raised.
    """
    logs.entry("handler_calendar_check")
    request = read_event(event, "handler_calendar_check")
    store = lake(context, "handler_calendar_check", required=False)
    answer = calendar_check.run(request, store)
    log.info(
        "calendar check answered: date=%s businessDay=%s reason=%s",
        answer.get("date"),
        answer.get("isBusinessDay"),
        answer.get("reason"),
    )
    return answer


def read_event(event, where):
    """The request mapping inside ``event``, or a refusal that names the
    handler it was asked of.

    A schedule pass, a console test and an invoked call reach the same handler
    with the request at a different depth, so one level of wrapping is
    followed here and nothing deeper.
    """
    request = _as_mapping(event)
    if request is None:
        raise ValueError(
            "%s: the event is not a request. Expected a JSON object such as "
            '{"jobName": "JOB1"} or {"runDate": "2026-11-14"}; got %s."'
            % (where, _shape(event))
        )
    return _unwrap(request)


def lake(context, where, required=True):
    """A data lake the answer can be looked up in.

    ``required`` separates the two questions: the file check is a question
    about the contents of the lake, so no identity means no answer and the call
    has to say so. The calendar check is a question about a date, and the lake
    only refines it.
    """
    bucket = config.bucket()
    if not bucket:
        raise ValueError(
            "%s: no data bucket has been named to this function, so there is "
            "nothing to check. The deployment sets it as the bucket of the run."
            % where
        )
    creds = credentials.resolve(context)
    if creds is None:
        if not required:
            log.warning(
                "%s: %s - answering with the weekend rule alone",
                where,
                credentials.describe(creds),
            )
            return None
        raise RuntimeError("%s: %s" % (where, obs_store.config_error()))
    log.debug("%s: connected to bucket %s with %s", where, bucket, credentials.describe(creds))
    return obs_store.connect(creds)


def _as_mapping(event):
    if isinstance(event, (bytes, bytearray)):
        event = event.decode("utf-8", "replace")
    if isinstance(event, str):
        try:
            event = json.loads(event)
        except ValueError:
            return None
    return event if isinstance(event, dict) else None


def _unwrap(request):
    """Follow one wrapper that carries a JSON object inside it."""
    for field in WRAPPERS:
        inner = request.get(field)
        if isinstance(inner, dict):
            return dict(inner)
        if isinstance(inner, str) and inner.strip().startswith("{"):
            found = _as_mapping(inner)
            if found:
                merged = dict(request)
                merged.update(found)
                return merged
    return request


def _shape(event):
    if isinstance(event, (bytes, bytearray)):
        return "bytes"
    return type(event).__name__

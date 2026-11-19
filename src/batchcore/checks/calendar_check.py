"""Business day decision for a scheduled launch.

The gate a scheduler would express as a processing calendar: a launch that
falls on a day the business does not process ends cleanly, without a failure
signal and without a notice to the operators.

The rule set lives outside the batch programs so that a calendar can change
without a redeploy. Two parts:

* the weekend, which is always a non-processing day
* a list of further non-processing dates, read from one object of the data
  lake when that object exists

The date is resolved in the time zone the schedule is expressed in. A launch
that fires at two in the morning local time can arrive as the previous day in
coordinated universal time, and it is the local calendar day that decides.
"""

from datetime import datetime, timezone

from ..shared import config, logs

log = logs.get("calendar-check")

WEEKEND = "weekend"
HOLIDAY = "non-processing date"
BUSINESS = "business day"
NO_CALENDAR = "no calendar object"

_DATE_FORMATS = ("%Y-%m-%d", "%Y%m%d", "%Y-%m-%dT%H:%M:%S")


def run(event, store=None):
    """Decide whether the launch of ``event`` falls on a processing day."""
    zone, zone_name = _zone()
    moment = _resolve(event, zone)
    local = moment.astimezone(zone) if moment.tzinfo else moment.replace(tzinfo=zone)
    date = local.strftime("%Y-%m-%d")

    if local.weekday() >= 5:
        return _answer(date, False, WEEKEND, zone_name, None)

    days, loaded = _non_processing_days(store)
    if not loaded:
        # No calendar in the lake: the weekend rule alone. This is the state a
        # fresh deployment starts in, so it is not reported as a failure.
        return _answer(date, True, BUSINESS, zone_name, NO_CALENDAR)
    if date in days:
        return _answer(date, False, HOLIDAY, zone_name, len(days))
    return _answer(date, True, BUSINESS, zone_name, len(days))


def _answer(date, is_business_day, reason, zone_name, calendar):
    log.info("date=%s businessDay=%s reason=%s", date, is_business_day, reason)
    result = {
        "isBusinessDay": is_business_day,
        "date": date,
        "reason": reason,
        "timezone": zone_name,
        "calendar": (
            calendar
            if calendar == NO_CALENDAR
            else ("%d non-processing date(s)" % calendar if calendar is not None else NO_CALENDAR)
        ),
    }
    return result


def _zone():
    """The time zone the schedule is expressed in, UTC when it is unusable.

    A misspelled zone must not stop the chain: the deployment default is
    evaluated instead and the mistake is left in the log.
    """
    name = config.schedule_timezone()
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name), name
    except Exception as error:  # unknown key, or no zone database in the image
        log.warning("time zone %s unusable (%s), evaluating in UTC", name, error)
        return timezone.utc, "UTC"


def _resolve(event, zone):
    """The date the decision is taken for.

    A pass of the orchestration layer carries the run date, so that a resumed
    pass decides about the day the run was opened rather than the day it
    happens to continue. An operator asking the question directly gets today.
    """
    raw = str(event.get("runDate") or event.get("date") or "").strip()
    if raw:
        parsed = _parse(raw)
        if parsed is not None:
            return parsed
        log.warning("run date %r is not readable, falling back to now", raw)
    return datetime.now(zone)


def _parse(raw):
    text = raw.replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        moment = None
    if moment is not None:
        if moment.tzinfo is None:
            # A plain date is the calendar day of the schedule, not a point in
            # coordinated universal time: midnight local is the day it names.
            return moment.replace(tzinfo=_local_zone(moment))
        return moment
    for form in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, form)
        except ValueError:
            continue
    return None


def _local_zone(moment):
    zone, _name = _zone()
    return zone


def _non_processing_days(store):
    """The dates the calendar names, and whether a calendar could be read.

    Accepted shapes are a list of dates and an object carrying them under
    ``nonProcessingDays``. Anything else is treated as no calendar, because a
    half read calendar would silently mark processing days as free days.
    """
    if store is None:
        return set(), False
    key = config.calendar_object()
    try:
        value = store.read_json(key)
    except Exception as error:
        log.warning("calendar object %s unreadable (%s)", key, error)
        return set(), False
    if value is None:
        return set(), False
    if isinstance(value, dict):
        value = value.get("nonProcessingDays") or value.get("dates")
    if not isinstance(value, list):
        log.warning("calendar object %s does not hold a list of dates", key)
        return set(), False
    days = set()
    for entry in value:
        moment = _parse(str(entry))
        if moment is None:
            log.warning("calendar entry %r is not a date, skipped", entry)
            continue
        days.add(moment.strftime("%Y-%m-%d"))
    return days, True

"""The business day gate: weekend rule, calendar object, and the zones it runs in."""

from datetime import datetime, timedelta, timezone

from conftest import make_store
from batchcore.checks import calendar_check
from batchcore.shared import config

SATURDAY = "2026-11-14"
SUNDAY = "2026-11-15"
WEDNESDAY = "2026-11-11"


def calendar_store(backend, value):
    """A lake holding one calendar document."""
    import json

    body = value if isinstance(value, str) else json.dumps(value)
    backend.put_object(config.calendar_object(), body)
    return make_store(backend)


def test_a_saturday_is_never_a_processing_day(env):
    answer = calendar_check.run({"runDate": SATURDAY})
    assert answer["isBusinessDay"] is False
    assert answer["reason"] == calendar_check.WEEKEND
    assert answer["date"] == SATURDAY
    assert answer["timezone"] == "UTC"


def test_a_sunday_is_read_as_a_weekend_too(env):
    assert calendar_check.run({"date": SUNDAY})["reason"] == calendar_check.WEEKEND


def test_a_wednesday_without_a_calendar_runs_on_the_weekend_rule_alone(env):
    answer = calendar_check.run({"runDate": WEDNESDAY})
    assert answer["isBusinessDay"] is True
    assert answer["reason"] == calendar_check.BUSINESS
    assert answer["calendar"] == calendar_check.NO_CALENDAR


def test_a_wednesday_named_by_the_calendar_does_not_run(env, backend):
    store = calendar_store(backend, [WEDNESDAY])
    answer = calendar_check.run({"runDate": WEDNESDAY}, store)
    assert answer["isBusinessDay"] is False
    assert answer["reason"] == calendar_check.HOLIDAY
    assert answer["calendar"] == "1 non-processing date(s)"


def test_the_other_days_of_a_calendar_do_not_change_the_answer(env, backend):
    store = calendar_store(backend, ["2026-12-25", "2026-01-01"])
    assert calendar_check.run({"runDate": WEDNESDAY}, store)["isBusinessDay"] is True


def test_a_calendar_can_carry_its_dates_under_a_field(env, backend):
    for value in (
        {"nonProcessingDays": [WEDNESDAY]},
        {"dates": [WEDNESDAY]},
    ):
        store = calendar_store(backend, value)
        assert calendar_check.run({"runDate": WEDNESDAY}, store)["isBusinessDay"] is False, value


def test_dates_written_without_dashes_are_the_same_day(env, backend):
    store = calendar_store(backend, ["20261111"])
    assert calendar_check.run({"runDate": WEDNESDAY}, store)["reason"] == calendar_check.HOLIDAY


def test_an_instant_is_read_as_the_day_it_fell_on(env, backend):
    answer = calendar_check.run({"date": "2026-11-14T02:00:00Z"})
    assert answer["date"] == SATURDAY
    assert answer["isBusinessDay"] is False


def test_a_calendar_that_holds_no_list_is_treated_as_no_calendar_at_all(env, backend):
    store = calendar_store(backend, "the twenty sixth of october")
    answer = calendar_check.run({"runDate": WEDNESDAY}, store)
    assert answer["isBusinessDay"] is True
    assert answer["calendar"] == calendar_check.NO_CALENDAR


def test_entries_that_are_not_dates_are_skipped_not_stored(env, backend):
    store = calendar_store(backend, ["tomorrow", WEDNESDAY, 1234])
    answer = calendar_check.run({"runDate": WEDNESDAY}, store)
    assert answer["reason"] == calendar_check.HOLIDAY


def test_a_calendar_the_lake_refuses_to_open_leaves_the_weekend_rule_standing(env, backend):
    store = calendar_store(backend, [WEDNESDAY])
    store.backend.fail_on_get = config.calendar_object()
    answer = calendar_check.run({"runDate": WEDNESDAY}, store)
    assert answer["isBusinessDay"] is True
    assert answer["calendar"] == calendar_check.NO_CALENDAR


def test_an_absent_calendar_object_is_not_a_failure(env, backend):
    store = make_store(backend)
    assert calendar_check.run({"runDate": WEDNESDAY}, store)["isBusinessDay"] is True


def test_no_request_at_all_answers_for_this_instant(env):
    answer = calendar_check.run({})
    assert answer["date"] == datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert answer["isBusinessDay"] in (True, False)


def test_a_date_nobody_can_read_falls_back_to_now(env, caplog):
    answer = calendar_check.run({"runDate": "the day after tomorrow"})
    assert answer["date"] == datetime.now(timezone.utc).strftime("%Y-%m-%d")


def fake_zone(name, offset_hours):
    """Stand in for the zone database, which the test image may not carry."""
    return timezone(timedelta(hours=offset_hours), name)


def test_the_day_is_resolved_in_the_zone_the_schedule_is_expressed_in(env, monkeypatch):
    env.setenv(config.SCHEDULE_TIMEZONE, "Asia/Shanghai")
    monkeypatch.setattr("zoneinfo.ZoneInfo", lambda key: fake_zone(key, 8))
    answer = calendar_check.run({"date": "2026-11-14T00:30:00+08:00"})
    assert answer["timezone"] == "Asia/Shanghai"
    assert answer["date"] == SATURDAY


def test_an_evening_in_universal_time_can_be_tomorrow_locally(env, monkeypatch):
    env.setenv(config.SCHEDULE_TIMEZONE, "Asia/Shanghai")
    monkeypatch.setattr("zoneinfo.ZoneInfo", lambda key: fake_zone(key, 8))
    # The same instant is Saturday in universal time and Sunday eight hours
    # east: the local calendar day is what the rule decides on.
    answer = calendar_check.run({"date": "2026-11-14T20:00:00Z"})
    assert answer["date"] == SUNDAY
    assert answer["reason"] == calendar_check.WEEKEND


def test_a_date_written_without_a_zone_names_the_day_of_the_schedule(env, monkeypatch):
    env.setenv(config.SCHEDULE_TIMEZONE, "Asia/Shanghai")
    monkeypatch.setattr("zoneinfo.ZoneInfo", lambda key: fake_zone(key, 8))
    answer = calendar_check.run({"runDate": SATURDAY})
    assert answer["date"] == SATURDAY
    assert answer["reason"] == calendar_check.WEEKEND


def test_a_zone_the_image_cannot_find_does_not_stop_the_chain(env):
    env.setenv(config.SCHEDULE_TIMEZONE, "Nowhere/Nothingland")
    answer = calendar_check.run({"runDate": SATURDAY})
    assert answer["timezone"] == "UTC"
    assert answer["isBusinessDay"] is False

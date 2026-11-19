"""The input gate: is this step allowed to read its inputs now?

Each branch of the judgement is driven against an in-memory lake: present,
absent, too old, a time that cannot be read, a lake that refuses to answer, and
a name nothing is configured for.
"""

from datetime import datetime, timezone

from conftest import days_ago, http_date, make_store
from batchcore.checks import file_check
from batchcore.shared import config


def lake_with(backend, **objects):
    for key, body in objects.items():
        backend.put_object(key, body)
    return make_store(backend)


def folder_of_step(step):
    """The folders one step reads, as the deployment derives them."""
    return [
        file_check.folder_of(prefix_key, producer)
        for prefix_key, producer, _ in file_check.STEPS[step]["reads"]
    ]


def test_the_steps_the_chain_uses_are_all_configured():
    assert file_check.step_names() == ["JOB1", "JOB2A", "JOB2B", "JOB3"]


def test_an_arriving_extract_that_is_there_and_recent_answers_yes(env, backend):
    store = lake_with(backend, **{"inbound/transactions/txns.20261026.csv": "a,b"})
    store.backend.times["inbound/transactions/txns.20261026.csv"] = http_date(days_ago(0))
    answer = file_check.run({"jobName": "JOB1"}, store)
    assert answer["allFilesPresent"] is True
    assert answer["errorType"] is None
    assert answer["step"] == "JOB1"
    assert answer["filesChecked"] == 1
    assert answer["missingCount"] == 0
    assert answer["fileResults"][0]["ageStatus"] == "WITHIN_ALLOWED_AGE"
    assert answer["message"] == "Every input of JOB1 is present."


def test_an_arriving_extract_nobody_has_delivered_answers_no(env, backend):
    store = lake_with(backend, **{"inbound/transactions/readme.txt": "not a extract"})
    answer = file_check.run({"jobName": "JOB1"}, store)
    assert answer["allFilesPresent"] is False
    assert answer["errorType"] == file_check.MISSING
    assert answer["fileResults"][0]["ageStatus"] == "FILE_NOT_FOUND"


def test_a_file_that_is_not_named_like_an_extract_is_not_counted(env, backend):
    store = lake_with(backend, **{"inbound/transactions/txns.2026-10-26.csv": "a"})
    assert file_check.run({"jobName": "JOB1"}, store)["errorType"] == file_check.MISSING


def test_yesterdays_extract_is_still_recent_enough(env, backend):
    key = "inbound/transactions/txns.20261025.csv"
    store = lake_with(backend, **{key: "a"})
    store.backend.times[key] = http_date(days_ago(1))
    answer = file_check.run({"jobName": "JOB1"}, store)
    assert answer["allFilesPresent"] is True
    assert answer["fileResults"][0]["numberOfDaysOld"] == 1


def test_the_window_is_counted_in_calendar_days(env, backend):
    key = "inbound/transactions/txns.20261020.csv"
    store = lake_with(backend, **{key: "a"})
    store.backend.times[key] = http_date(days_ago(config.max_age_days() + 3))
    answer = file_check.run({"jobName": "JOB1"}, store)
    assert answer["allFilesPresent"] is False
    assert answer["errorType"] == file_check.TOO_OLD
    assert answer["fileResults"][0]["ageStatus"] == "OLDER_THAN_2_DAYS"
    assert answer["maxAgeDays"] == 2


def test_the_edge_of_the_window_still_passes(env, backend):
    key = "inbound/transactions/txns.20261024.csv"
    store = lake_with(backend, **{key: "a"})
    store.backend.times[key] = http_date(days_ago(2))
    assert file_check.run({"jobName": "JOB1"}, store)["allFilesPresent"] is True


def test_a_file_without_a_readable_time_is_not_proof_of_freshness(env, backend):
    store = lake_with(backend, **{"inbound/transactions/txns.20261026.csv": "a"})
    answer = file_check.run({"jobName": "JOB1"}, store)
    assert answer["allFilesPresent"] is False
    assert answer["errorType"] == file_check.TOO_OLD
    assert answer["fileResults"][0]["ageStatus"] == "MODIFICATION_TIME_UNKNOWN"


def test_an_unreadable_lake_is_never_reported_as_a_missing_input(env, backend):
    store = lake_with(backend)
    store.backend.fail_on_prefix = "inbound/"
    answer = file_check.run({"jobName": "JOB1"}, store)
    assert answer["errorType"] == file_check.UNREADABLE
    assert answer["fileResults"][0]["ageStatus"] == "STORAGE_ERROR"
    assert answer["allFilesPresent"] is False


def test_an_intermediate_data_set_is_checked_for_presence_only(env, backend):
    key = "processing/JOB1/output/normalized.csv"
    store = lake_with(backend, **{key: "a"})
    store.backend.times[key] = http_date(days_ago(400))
    answer = file_check.run({"jobName": "JOB2A"}, store)
    assert answer["allFilesPresent"] is True
    assert answer["fileResults"][0]["ageStatus"] == "AGE_CHECK_NOT_REQUIRED"
    assert answer["fileResults"][0]["ageCheckApplied"] is False


def test_a_step_with_two_inputs_reports_both(env, backend):
    store = lake_with(
        backend,
        **{"processing/JOB2A/output/fees.csv": "a", "outbound/unrelated": "b"}
    )
    answer = file_check.run({"jobName": "JOB3"}, store)
    assert answer["filesChecked"] == 2
    assert answer["missingCount"] == 1
    assert answer["allFilesPresent"] is False
    assert answer["errorType"] == file_check.MISSING
    statuses = [item["ageStatus"] for item in answer["fileResults"]]
    assert statuses == ["AGE_CHECK_NOT_REQUIRED", "FILE_NOT_FOUND"]


def test_a_step_with_both_inputs_present_starts(env, backend):
    store = lake_with(
        backend,
        **{
            "processing/JOB2A/output/fees.csv": "a",
            "processing/JOB2B/output/rebates.csv": "b",
        }
    )
    answer = file_check.run({"jobName": "JOB3"}, store)
    assert answer["allFilesPresent"] is True
    assert answer["errorType"] is None


def test_the_check_accepts_the_names_a_caller_really_uses(env, backend):
    store = lake_with(backend, **{"processing/JOB1/output/normalized.csv": "a"})
    for request in (
        {"jobName": "JOB2A"},
        {"jobName": "Check_JOB2A_Input_Files"},
        {"step": "job2a"},
        {"job": "JOB2A"},
        {"module": "check_job2a_input_files"},
    ):
        assert file_check.run(request, store)["step"] == "JOB2A", request


def test_a_name_nothing_is_configured_for_is_reported_not_guessed(env, backend):
    answer = file_check.run({"jobName": "JOB9"}, make_store(backend))
    assert answer["errorType"] == file_check.INVALID
    assert answer["step"] is None
    assert answer["allFilesPresent"] is False
    assert answer["knownSteps"] == ["JOB1", "JOB2A", "JOB2B", "JOB3"]


def test_an_empty_request_is_a_name_that_matches_nothing(env, backend):
    assert file_check.run({}, make_store(backend))["errorType"] == file_check.INVALID


def test_a_run_identifier_moves_the_question_into_that_runs_namespace(env, backend):
    store = lake_with(backend, **{"runs/night-7/inbound/transactions/txns.20261026.csv": "a"})
    store.backend.times["runs/night-7/inbound/transactions/txns.20261026.csv"] = http_date(
        datetime.now(timezone.utc)
    )
    answer = file_check.run({"jobName": "JOB1", "runId": "night-7"}, store)
    assert answer["prefix"] == "runs/night-7/"
    assert answer["allFilesPresent"] is True
    assert store.backend.list_calls[-1][1] == "runs/night-7/inbound/transactions/"


def test_without_a_run_identifier_the_production_prefixes_are_checked(env, backend):
    store = lake_with(backend)
    answer = file_check.run({"jobName": "JOB1"}, store)
    assert answer["prefix"] == "(production)"
    assert store.backend.list_calls[-1][1] == "inbound/transactions/"


def test_a_long_backlog_is_probed_only_at_its_newest_end(env, backend):
    objects = {"inbound/transactions/txns.%08d.csv" % index: "a" for index in range(60)}
    store = lake_with(backend, **objects)
    probed = []
    read_time = store.backend.head

    def counting(bucket, key):
        probed.append(key)
        return read_time(bucket, key)

    store.backend.head = counting
    answer = file_check.run({"jobName": "JOB1"}, store)
    assert len(probed) == file_check.MODIFICATION_PROBE_LIMIT
    # The newest names by key are the ones asked about, not the oldest.
    assert probed[0] == "inbound/transactions/txns.00000059.csv"
    assert answer["errorType"] == file_check.TOO_OLD


def test_the_newest_of_a_matched_set_is_the_one_judged(env, backend):
    store = lake_with(
        backend,
        **{
            "inbound/transactions/txns.00000001.csv": "a",
            "inbound/transactions/txns.00000002.csv": "a",
        }
    )
    store.backend.times = {
        "inbound/transactions/txns.00000001.csv": http_date(days_ago(30)),
        "inbound/transactions/txns.00000002.csv": http_date(days_ago(1)),
    }
    answer = file_check.run({"jobName": "JOB1"}, store)
    assert answer["allFilesPresent"] is True
    assert answer["fileResults"][0]["newestKey"] == "inbound/transactions/txns.00000002.csv"
    assert answer["fileResults"][0]["matchedObjects"] == 2


def test_the_probe_stops_at_the_newest_names_of_a_long_backlog(env, backend):
    keys = ["inbound/transactions/txns.20261026.csv"]
    store = lake_with(backend, **{key: "a" for key in keys})
    probed = []

    def recording_modified(key):
        probed.append(key)
        return None

    store.modified = recording_modified
    file_check.run({"jobName": "JOB1"}, store)
    assert probed == ["inbound/transactions/txns.20261026.csv"]


def test_a_folder_is_derived_from_the_deployment_not_written_out_in_full(env):
    assert folder_of_step("JOB3") == [
        "processing/JOB2A/output/",
        "processing/JOB2B/output/",
    ]
    assert file_check.folder_of(config.INBOUND_PREFIX, None) == "inbound/transactions/"


def test_the_answer_of_a_present_input_names_the_bucket_it_was_taken_in(env, backend):
    store = lake_with(backend, **{"processing/JOB1/output/normalized.csv": "a"})
    answer = file_check.run({"jobName": "JOB2A"}, store)
    assert answer["bucket"] == "batch-data-test"
    assert answer["jobName"] == "JOB2A"

"""The data lake, driven through an in-memory stand-in for its client.

The store is exercised with a fake lake, and once more against a fake client
that answers the way the runtime client does, so the status judgement - absent,
refused, accepted - is covered without a single request leaving the process.
"""

import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

from batchcore.shared import config, obs_store
from batchcore.shared.obs_store import Object, ObjectStore
from batchcore.shared.obs_store import StorageError

BUCKET = "batch-data-test"


def answer(status, body=None, code=None):
    return types.SimpleNamespace(status=status, body=body, code=code)


class FakeClient:
    """The five calls the store makes, answered the way the runtime answers."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.closed = False

    def _take(self, name, *args):
        self.calls.append((name, args))
        return self.responses[name]

    def listObjects(self, bucket, prefix=None, marker=None, max_keys=None):
        return self._take("list", bucket, prefix, marker, max_keys)

    def getObject(self, bucket, key, loadStreamInMemory=True):
        return self._take("get", bucket, key)

    def getObjectMetadata(self, bucket, key):
        return self._take("head", bucket, key)

    def putContent(self, bucket, key, content=None):
        return self._take("put", bucket, key, content)

    def deleteObject(self, bucket, key):
        return self._take("delete", bucket, key)

    def close(self):
        self.closed = True


def backend_with(objects=None, times=None, page_size=1000):
    from conftest import FakeBackend

    return FakeBackend(objects, times, page_size)


def test_an_object_knows_its_own_name():
    item = Object("inbound/transactions/txns.20261026.csv")
    assert item.name == "txns.20261026.csv"
    assert repr(item) == "Object('inbound/transactions/txns.20261026.csv')"


def test_every_object_under_a_prefix_is_listed(env):
    store = ObjectStore(
        backend_with(
            {
                "inbound/transactions/a.csv": "one",
                "inbound/transactions/b.csv": "two",
                "outbound/a.csv": "elsewhere",
            }
        )
    )
    assert sorted(item.key for item in store.list("inbound/transactions/")) == [
        "inbound/transactions/a.csv",
        "inbound/transactions/b.csv",
    ]


def test_a_directory_placeholder_is_not_data(env):
    store = ObjectStore(backend_with({"inbound/": "", "inbound/a.csv": "x"}))
    assert [item.key for item in store.list("inbound/")] == ["inbound/a.csv"]


def test_a_long_prefix_is_walked_page_by_page(env):
    keys = {"prefix/key-%03d.csv" % index: "x" for index in range(25)}
    store = ObjectStore(backend_with(keys, page_size=10))
    listed = [item.key for item in store.list("prefix/")]
    assert len(listed) == 25
    assert len(store.backend.list_calls) == 3


def test_a_page_that_never_ends_is_still_bounded(env):
    class AlwaysTruncated:
        def list_objects(self, bucket, prefix, marker, limit):
            return {
                "objects": [Object(prefix + "one")],
                "truncated": True,
                "next_marker": prefix + "one",
            }

    store = ObjectStore(AlwaysTruncated())
    assert len(store.list("prefix/")) == 500


def test_reading_an_absent_object_is_not_an_error(env):
    store = ObjectStore(backend_with({"a": "text"}))
    assert store.read("nothing") is None
    assert store.read("a") == "text"


def test_a_json_object_is_read_or_reported_as_unreadable(env, caplog):
    store = ObjectStore(backend_with({"good": '{"a": 1}', "bad": "{oops", "blank": ""}))
    assert store.read_json("good") == {"a": 1}
    assert store.read_json("bad") is None
    assert store.read_json("blank") is None
    assert store.read_json("missing") is None


def test_a_write_goes_to_the_lake_named_for_this_deployment(env):
    store = ObjectStore(backend_with())
    assert store.write("runs/a.json", "{}") is True
    assert store.backend.put_calls == [(BUCKET, "runs/a.json")]
    assert store.write_json("runs/b.json", {"b": 2}) is True
    assert store.read("runs/b.json") == '{"b": 2}'


def test_a_bucket_named_by_the_deployment_is_used_for_every_call(env):
    store = ObjectStore(backend_with(), bucket="another-bucket")
    assert store.bucket == "another-bucket"
    store.write("k", "v")
    assert store.backend.put_calls == [("another-bucket", "k")]


def test_the_bucket_of_the_deployment_is_the_default(env):
    assert ObjectStore(backend_with()).bucket == BUCKET


def test_a_modification_time_is_read_as_an_instant(env):
    moment = datetime(2026, 10, 26, 4, 30, tzinfo=timezone.utc)
    store = ObjectStore(
        backend_with({"a": "x"}, {"a": moment.strftime("%a, %d %b %Y %H:%M:%S GMT")})
    )
    assert store.modified("a") == moment


def test_a_time_without_a_zone_is_read_as_coordinated_universal_time(env):
    store = ObjectStore(
        backend_with({"a": "x"}, {"a": "Mon, 26 Oct 2026 04:30:00"})
    )
    read = store.modified("a")
    assert read.tzinfo == timezone.utc
    assert read.hour == 4


def test_an_unreadable_time_is_unknown_rather_than_guessed(env):
    store = ObjectStore(backend_with({"a": "x"}, {"a": "yesterday evening"}))
    assert store.modified("a") is None
    store2 = ObjectStore(backend_with({"a": "x"}))
    assert store2.modified("a") is None


def test_a_deleting_call_reports_what_it_did(env):
    store = ObjectStore(backend_with({"a": "x"}))
    assert store.delete("a") is True
    assert store.read("a") is None


def test_the_client_answers_are_reduced_to_plain_values(env):
    body = types.SimpleNamespace(
        contents=[
            types.SimpleNamespace(key="p/a", lastModified="Mon, 26 Oct 2026 04:30:00 GMT", size=3)
        ],
        isTruncated=False,
        nextMarker=None,
    )
    client = FakeClient(
        {
            "list": answer(200, body),
            "get": answer(200, types.SimpleNamespace(data=b"payload")),
            "head": answer(200, types.SimpleNamespace(lastModified="Mon, 26 Oct 2026 04:30:00 GMT")),
            "put": answer(200, None),
            "delete": answer(204, None),
        }
    )
    store = ObjectStore(obs_store._ObsBackend(client), BUCKET)
    listed = store.list("p/")
    assert listed[0].key == "p/a"
    assert listed[0].size == 3
    assert store.read("p/a") == "payload"
    assert store.modified("p/a").year == 2026
    assert store.write("p/a", "payload") is True
    assert store.delete("p/a") is True


def test_a_refused_listing_is_a_storage_error_not_an_empty_prefix():
    client = FakeClient({"list": answer(500, None, code="OB0511"), "get": answer(404),
                         "head": answer(404), "put": answer(200), "delete": answer(204)})
    store = ObjectStore(obs_store._ObsBackend(client), BUCKET)
    with pytest.raises(StorageError) as refused:
        store.list("p/")
    assert refused.value.status == 500
    assert refused.value.code == "OB0511"
    assert "list p/ refused" in str(refused.value)


def test_an_absent_object_reads_as_nothing_and_not_as_a_failure():
    client = FakeClient({"get": answer(404), "head": answer(404), "list": answer(200, None),
                         "put": answer(200), "delete": answer(204)})
    store = ObjectStore(obs_store._ObsBackend(client), BUCKET)
    assert store.read("p/a") is None
    assert store.modified("p/a") is None


def test_a_refused_write_is_reported_to_the_caller(env):
    client = FakeClient({"put": answer(403), "get": answer(404), "head": answer(404),
                         "list": answer(200, None), "delete": answer(204)})
    store = ObjectStore(obs_store._ObsBackend(client), BUCKET)
    assert store.write("p/a", "body") is False


def test_a_object_body_stream_is_read_when_no_buffer_came_back():
    body = types.SimpleNamespace(data=None, response=types.SimpleNamespace(read=lambda: b"late"))
    client = FakeClient({"get": answer(200, body), "head": answer(404), "list": answer(200, None),
                         "put": answer(200), "delete": answer(204)})
    store = ObjectStore(obs_store._ObsBackend(client), BUCKET)
    assert store.read("p/a") == "late"


def test_a_deletion_that_found_nothing_still_counts_as_done():
    for status, expected in ((200, True), (204, True), (404, True), (500, False)):
        client = FakeClient({"delete": answer(status), "get": answer(404), "head": answer(404),
                             "list": answer(200, None), "put": answer(200)})
        assert ObjectStore(obs_store._ObsBackend(client), BUCKET).delete("k") is expected


def test_the_client_is_closed_with_the_store():
    client = FakeClient({"delete": answer(204), "get": answer(404), "head": answer(404),
                         "list": answer(200, None), "put": answer(200)})
    backend = obs_store._ObsBackend(client)
    backend.close()
    assert client.closed is True

    class Refusing:
        def close(self):
            raise RuntimeError("already gone")

    obs_store._ObsBackend(Refusing()).close()


def test_no_identity_means_no_store_built_at_all(env):
    with pytest.raises(ValueError) as missing:
        obs_store.connect(None)
    assert "no temporary credentials" in str(missing.value)


def test_the_client_of_the_runtime_is_bound_to_the_regional_endpoint(env, monkeypatch):
    seen = {}

    class ObsClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def listObjects(self, *args, **kwargs):
            return answer(200, types.SimpleNamespace(contents=[], isTruncated=False))

    module = types.ModuleType("obs")
    module.ObsClient = ObsClient
    monkeypatch.setitem(sys.modules, "obs", module)

    store = obs_store.connect(("AK", "SK", "TOKEN"), bucket="named-bucket")
    assert seen["access_key_id"] == "AK"
    assert seen["security_token"] == "TOKEN"
    assert seen["server"] == "https://obs.cn-north-4.myhuaweicloud.com"
    assert seen["path_style"] is True
    assert store.bucket == "named-bucket"
    assert store.list("any/") == []


def test_the_endpoint_is_taken_over_as_a_bare_host(env, monkeypatch):
    env.setattr(config, "text", lambda name, default="": "http://obs.example.com:443/")
    assert config.obs_endpoint() == "obs.example.com:443"
    assert config.obs_scheme() == "https"


def test_an_unreachable_lake_is_not_reported_as_a_missing_file(env):
    store = ObjectStore(backend_with({"runs/blocked/a.txt": "x"}))
    store.backend.fail_on_prefix = "runs/"
    with pytest.raises(RuntimeError):
        store.list("runs/")

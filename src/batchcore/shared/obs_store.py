"""Object storage access for the functions.

A function never carries a key: the temporary triple issued to it by the
agency is passed to the client, and the bucket statement written by the
deployment is what makes that triple count. The client library itself is part
of the function runtime image, so it is imported lazily and is never bundled
into the deployed archive.

Everything the orchestration layer and the validation layer need is four
calls: list a prefix, read an object, write an object, read the modification
time of an object. They are wrapped here behind one small class so that a test
can hand in a stand-in for the whole service and the logic on top of it stays
unchanged.
"""

from . import config, logs

log = logs.get("obs")

PAGE_SIZE = 1000
LIST_RETRIES = 3


class Object:
    """One listed object: its key and the raw modification time it was
    reported with."""

    __slots__ = ("key", "raw_modified", "size")

    def __init__(self, key, raw_modified=None, size=None):
        self.key = key
        self.raw_modified = raw_modified
        self.size = size

    @property
    def name(self):
        return self.key.rsplit("/", 1)[-1]

    def __repr__(self):
        return "Object(%r)" % self.key


class ObjectStore:
    """Prefix oriented access to the one bucket of the deployment."""

    def __init__(self, backend, bucket=None):
        self.backend = backend
        self.bucket = bucket or config.bucket()

    # -- reading ----------------------------------------------------------

    def list(self, prefix):
        """Every real object under ``prefix``.

        A key ending in a slash is a directory placeholder, not data, and is
        dropped. The listing is paginated: a prefix holding more than one
        page is normal for a batch lake.
        """
        objects = []
        marker = None
        for _ in range(500):
            page, truncated, marker = self._page(prefix, marker)
            for item in page:
                if not item.key.endswith("/"):
                    objects.append(item)
            if not truncated:
                break
        return objects

    def _page(self, prefix, marker):
        response = self.backend.list_objects(self.bucket, prefix, marker, PAGE_SIZE)
        return (
            list(response.get("objects") or []),
            bool(response.get("truncated")),
            response.get("next_marker"),
        )

    def read(self, key):
        """Text of an object, or ``None`` when it is not there."""
        return self.backend.get(self.bucket, key)

    def read_json(self, key):
        import json

        raw = self.read(key)
        if raw is None or raw == "":
            return None
        try:
            return json.loads(raw)
        except ValueError:
            log.warning("object %s is not readable as JSON, ignored", key)
            return None

    def modified(self, key):
        """Modification time of an object as a UTC datetime, or ``None``.

        The service reports the time as an HTTP date; a value that cannot be
        read is reported as unknown rather than guessed, and an unknown time
        makes the caller treat the object as not fresh.
        """
        from email.utils import parsedate_to_datetime
        from datetime import timezone

        raw = self.backend.head(self.bucket, key)
        if not raw:
            return None
        try:
            moment = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            log.warning("cannot read modification time %r of %s", raw, key)
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)

    # -- writing ----------------------------------------------------------

    def write(self, key, body):
        """Store text under ``key``. Writing the same content again is a
        valid retry, so this is safe to call from a resumed pass."""
        return self.backend.put(self.bucket, key, body)

    def write_json(self, key, value):
        import json

        return self.write(key, json.dumps(value, ensure_ascii=False, sort_keys=True))

    def delete(self, key):
        return self.backend.delete(self.bucket, key)


def connect(creds, bucket=None, endpoint=None):
    """A store bound to the runtime client of the assumed agency.

    The client library is imported here, not at module load: the deployed
    archive does not carry it, the runtime image provides it, and a test that
    injects its own backend therefore never needs it.
    """
    if creds is None:
        raise ValueError(config_error())
    from obs import ObsClient

    host = endpoint or config.obs_endpoint()
    scheme = config.obs_scheme()
    server = "%s://%s" % (scheme, host)
    access_key, secret_key, token = creds
    client = ObsClient(
        access_key_id=access_key,
        secret_access_key=secret_key,
        security_token=token,
        server=server,
        path_style=True,
    )
    return ObjectStore(_ObsBackend(client), bucket)


def config_error():
    return (
        "no temporary credentials: the agency of the function was not assumed "
        "and no credential triple was supplied"
    )


class StorageError(Exception):
    """A data plane call the service refused.

    Turned into a failed step by every caller: an unreadable lake is not a
    missing input file and must not be reported as one.
    """

    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code

    def __str__(self):
        return "%s / status %s / code %s" % (self.message, self.status, self.code)


def _check(response, what):
    status = getattr(response, "status", None) or 0
    if status // 100 == 2:
        return
    raise StorageError(
        "%s refused" % what,
        status=status,
        code=getattr(response, "code", None),
    )


class _ObsBackend:
    """The four client calls the store needs, reduced to plain values.

    Each one is judged by its status: a two hundred range is a success, a
    four hundred four is an absent object and anything else is an error that
    the caller turns into a failed step. The client is closed with the store.
    """

    def __init__(self, client):
        self.client = client

    def list_objects(self, bucket, prefix, marker, limit):
        response = self.client.listObjects(
            bucket, prefix=prefix, marker=marker, max_keys=limit
        )
        _check(response, "list %s" % prefix)
        body = response.body
        contents = list(getattr(body, "contents", None) or [])
        return {
            "objects": [
                Object(item.key, getattr(item, "lastModified", None), getattr(item, "size", None))
                for item in contents
            ],
            "truncated": bool(getattr(body, "isTruncated", False)),
            "next_marker": getattr(body, "nextMarker", None),
        }

    def get(self, bucket, key):
        response = self.client.getObject(bucket, key, loadStreamInMemory=True)
        if response.status == 404:
            return None
        _check(response, "get %s" % key)
        body = response.body
        raw = getattr(body, "data", None)
        if raw is None:
            stream = getattr(body, "response", None)
            raw = stream.read() if stream is not None else b""
        return raw.decode("utf-8") if isinstance(raw, bytes) else (raw or "")

    def head(self, bucket, key):
        response = self.client.getObjectMetadata(bucket, key)
        if response.status == 404:
            return None
        _check(response, "metadata of %s" % key)
        return getattr(response.body, "lastModified", None)

    def put(self, bucket, key, body):
        response = self.client.putContent(bucket, key, content=body)
        if response.status // 100 != 2:
            log.error("write of %s refused with status %s", key, response.status)
            return False
        return True

    def delete(self, bucket, key):
        response = self.client.deleteObject(bucket, key)
        return response.status in (204, 200, 404)

    def close(self):
        try:
            self.client.close()
        except Exception:  # a closed client is not worth a failed run
            pass

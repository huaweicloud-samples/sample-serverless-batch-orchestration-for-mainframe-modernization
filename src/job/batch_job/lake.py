"""Data lake access of the batch container.

Four calls cover a batch step: list a folder, read an object, write an object
and ask how large a written object is. They are wrapped behind one small class
so that the step logic never sees a client library and a self-test can hand in a
stand-in for the whole service.

The client is imported inside ``open_lake``, never at module load: the step
logic, the record parsing and the exit code protocol then run in any interpreter
that has this package, with no dependency installed at all.
"""

from . import settings

PAGE_SIZE = 1000
MAX_PAGES = 500


class StorageError(Exception):
    """A data plane call the service refused.

    Kept apart from a missing data set on purpose: an unreadable lake is a fault
    of its own and must not be reported to the operator as absent input.
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


class Lake:
    """Prefix oriented access to the one data bucket of the deployment.

    ``backend`` is anything offering the four calls below with the shape the
    service client has, so the same class serves a pod and a self-test.
    """

    def __init__(self, backend, bucket):
        self.backend = backend
        self.bucket = bucket

    def list(self, prefix):
        """Keys of every real object under ``prefix``.

        A key ending in a slash is a folder placeholder, not data, and is
        dropped. The listing is paginated: a prefix holding more than one page
        is normal for a data lake that keeps its history.
        """
        found = []
        marker = None
        for _ in range(MAX_PAGES):
            page = self.backend.list_objects(self.bucket, prefix, marker, PAGE_SIZE)
            for item in page["objects"]:
                if not item.endswith("/"):
                    found.append(item)
            if not page["truncated"]:
                return found
            marker = page["next_marker"]
            if not marker:
                return found
        raise StorageError("listing of %s did not finish within %d pages" % (prefix, MAX_PAGES))

    def read(self, key):
        """Text of an object, or ``None`` when it is not there."""
        return self.backend.get(self.bucket, key)

    def rows(self, prefix, pattern):
        """Lines of every object of a folder whose name matches ``pattern``.

        The keys are taken in listing order, which the service reports in
        lexicographic order: the newest day file is therefore read last, and a
        step that runs twice over the same folder gets the same records in the
        same order both times.
        """
        import re

        expression = re.compile(pattern)
        lines = []
        matched = 0
        for key in self.list(prefix):
            if not expression.match(key.rsplit("/", 1)[-1]):
                continue
            matched += 1
            body = self.read(key)
            if body is None:
                continue
            for line in body.splitlines():
                if line.strip():
                    lines.append(line.strip())
        return lines, matched

    def write(self, key, body):
        """Store text under ``key``.

        Writing the same content again is a valid retry, which is what lets a
        step of a resumed run repeat itself without harming the data set behind
        it.
        """
        return self.backend.put(self.bucket, key, body)

    def close(self):
        closer = getattr(self.backend, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # a closed client is not worth a failed step
                pass


def open_lake(conf):
    """A lake bound to the keys of this run.

    :param conf: a populated :class:`settings.Settings`.
    :raises StorageError: when the environment has no key pair to sign with.
    """
    if not conf.ready():
        raise StorageError("; ".join(conf.problems), status=None, code="CONFIG")
    from obs import ObsClient

    client = ObsClient(
        access_key_id=conf.access_key,
        secret_access_key=conf.secret_key,
        security_token=conf.security_token,
        server="https://%s" % conf.endpoint,
        path_style=True,
        is_obs_style=False,
    )
    return Lake(_ClientBackend(client), conf.bucket)


class _ClientBackend:
    """The four service calls reduced to plain values.

    A two hundred range is a success, a four hundred four is an absent object
    and anything else is an error the caller turns into a failed step.
    """

    def __init__(self, client):
        self.client = client

    def list_objects(self, bucket, prefix, marker, limit):
        response = self.client.listObjects(bucket, prefix=prefix, marker=marker, max_keys=limit)
        _check(response, "listing of %s" % prefix)
        body = response.body
        return {
            "objects": [item.key for item in (getattr(body, "contents", None) or [])],
            "truncated": bool(getattr(body, "isTruncated", False)),
            "next_marker": getattr(body, "nextMarker", None),
        }

    def get(self, bucket, key):
        response = self.client.getObject(bucket, key, loadStreamInMemory=True)
        if response.status == 404:
            return None
        _check(response, "read of %s" % key)
        body = response.body
        raw = getattr(body, "data", None)
        if raw is None:
            stream = getattr(body, "response", None)
            raw = stream.read() if stream is not None else b""
        return raw.decode("utf-8") if isinstance(raw, bytes) else (raw or "")

    def put(self, bucket, key, body):
        response = self.client.putContent(bucket, key, content=body)
        _check(response, "write of %s" % key)
        return True

    def close(self):
        try:
            self.client.close()
        except Exception:  # closing is best effort
            pass

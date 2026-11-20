"""Minimal signed transport for the regional service endpoints.

Only what the orchestration layer needs: one request function that signs,
sends, decodes and, on a rejected call, raises an error carrying the service
error code. Everything is built on the standard library, because the deployed
archive has to stay small and a generated client library drags in
dependencies a batch function never uses.

The transport is injectable: ``transport`` is a callable that performs the
call, and a test replaces it to drive the whole logic without a network.
"""

import json
import random
import time
import urllib.error
import urllib.request

from .. import credentials, logs
from . import sign

log = logs.get("http")

DEFAULT_TIMEOUT_SECONDS = 15


class ApiError(Exception):
    """A call the service refused, or one that never reached it."""

    def __init__(self, message, status=None, code=None, request_id=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.request_id = request_id

    def __str__(self):
        parts = [self.message]
        if self.status is not None:
            parts.append("http %s" % self.status)
        if self.code:
            parts.append("code %s" % self.code)
        if self.request_id:
            parts.append("request id %s" % self.request_id)
        return " / ".join(str(part) for part in parts)


def request(method, host, path, query=(), body=None, creds=None, transport=None, timeout=None):
    """Sign, send and decode one call.

    ``body`` is a JSON structure or raw bytes. The decoded response is a
    dictionary when the service answered with JSON and a string otherwise.
    """
    do = transport or perform
    return do(method, host, path, tuple(query), body, creds, timeout or DEFAULT_TIMEOUT_SECONDS)


def perform(method, host, path, query, body, creds, timeout):
    """The real transport: standard library only, over TLS, signed headers."""
    if creds is None:
        raise ApiError(
            "no credentials for %s: %s" % (host, credentials.describe(creds)),
        )
    payload = None
    if body is not None:
        payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")

    headers = sign.sign_request(method, host, path, query, None, payload, creds)
    url = "https://%s%s" % (host, path)
    rendered = sign.canonical_query(query)
    if rendered:
        url = url + "?" + rendered

    call = urllib.request.Request(url, data=payload, method=method.upper())
    for name, value in headers.items():
        call.add_header(name, str(value))

    log.debug("%s %s", method.upper(), url)
    try:
        with urllib.request.urlopen(call, timeout=timeout) as response:
            return decode(response.read(), response.headers)
    except urllib.error.HTTPError as error:
        raw = error.read()
        info = decode(raw, error.headers)
        info = info if isinstance(info, dict) else {}
        raise ApiError(
            info.get("error_msg") or info.get("message") or _text(raw) or error.reason,
            status=error.code,
            code=info.get("error_code") or info.get("ErrorCode") or error.code,
            request_id=error.headers.get("x-request-id"),
        ) from error
    except urllib.error.URLError as error:
        raise ApiError("request to %s failed: %s" % (host, error.reason)) from error


def decode(raw, headers):
    if not raw:
        return {}
    body = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    try:
        return json.loads(body)
    except ValueError:
        return body


def _text(raw):
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


def retryable(error):
    """Failures worth another attempt: the transient status codes of the
    gateway layer and anything that never reached the service."""
    if error.status is None:
        return True
    return error.status in (429, 500, 502, 503, 504)


def with_retries(action, attempts, base_seconds, sleep=time.sleep, chooser=None):
    """Run ``action`` under a bounded retry policy.

    The policy is the one the orchestration definitions are built with: a
    fixed number of attempts, a fixed interval, no growth and a full
    randomisation of the wait. The wait is drawn uniformly between zero and
    the interval, so two passes that collided do not collide again on the
    retry.
    """
    draw = chooser or random.Random()
    attempts = max(1, int(attempts))
    base = max(0, int(base_seconds))
    last = None
    for attempt in range(attempts):
        try:
            return action()
        except ApiError as error:
            last = error
            if not retryable(error) or attempt + 1 >= attempts:
                raise
            wait = draw.uniform(0, base) if base else 0
            log.warning("attempt %s failed (%s), retrying in %.1fs", attempt + 1, error, wait)
            sleep(wait)
    raise last

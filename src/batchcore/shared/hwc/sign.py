"""Request signing for the regional service endpoints.

The services reached by the orchestration layer all accept the same signed
header form, so the whole signing layer is one function. Keeping it local to
the package instead of pulling a generated software development kit also keeps
the deployed archive small and free of transgressive dependencies.

The form implemented here is the documented ``SDK-HMAC-SHA256`` scheme:

    canonical request = method, canonical uri, canonical query,
                        canonical headers, signed headers, hash of payload
    string to sign    = algorithm, request time, hash of canonical request
    signature         = keyed hash of the string to sign with the secret key

Headers whose name contains an underscore are not signed. The time used both
in the header and in the string to sign is taken from the signed header set,
so the two can never disagree.
"""

import hashlib
import hmac
from datetime import datetime
from urllib.parse import quote, unquote

ALGORITHM = "SDK-HMAC-SHA256"
BASIC_DATE_FORMAT = "%Y%m%dT%H%M%SZ"
EMPTY_PAYLOAD_HASH = hashlib.sha256(b"").hexdigest()
SDK_HEADER = "X-Sdk-Date"


def uri_encode(value):
    """Percent encoding used inside the canonical request. ``~`` stays bare."""
    return quote(value, safe="~")


def canonical_uri(path):
    """Each path segment encoded on its own, always with a trailing slash."""
    segments = [uri_encode(part) for part in unquote(path or "/").split("/")]
    joined = "/".join(segments)
    return joined if joined.endswith("/") else joined + "/"


def canonical_query(pairs):
    """Sorted ``key=value`` pairs, both sides encoded, empty value kept."""
    rendered = []
    for key, value in sorted(pairs or ()):
        if isinstance(value, (list, tuple)):
            for item in sorted(str(v) for v in value):
                rendered.append("%s=%s" % (uri_encode(str(key)), uri_encode(item)))
        else:
            rendered.append("%s=%s" % (uri_encode(str(key)), uri_encode(str(value))))
    return "&".join(rendered)


def sha256_hex(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data or b"").hexdigest()


def signed_header_names(headers):
    """Lower-cased names without an underscore, sorted."""
    return sorted(h.lower() for h in headers if "_" not in h)


def canonical_headers(headers, names):
    lowered = {k.lower(): str(v).strip() for k, v in headers.items()}
    lines = ["%s:%s" % (name, lowered[name]) for name in names]
    return "\n".join(lines) + "\n"


def canonical_request(method, path, query, headers, names, payload):
    return "\n".join(
        [
            method.upper(),
            canonical_uri(path),
            canonical_query(query),
            canonical_headers(headers, names),
            ";".join(names),
            sha256_hex(payload) if payload else EMPTY_PAYLOAD_HASH,
        ]
    )


def string_to_sign(canonical, request_time):
    return "%s\n%s\n%s" % (ALGORITHM, request_time, sha256_hex(canonical))


def sign(secret, message):
    digest = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()


def header_value(access_key, names, signature):
    return "%s Access=%s, SignedHeaders=%s, Signature=%s" % (
        ALGORITHM,
        access_key,
        ";".join(names),
        signature,
    )


def sign_request(method, host, path, query=(), headers=None, body=None, credentials=None):
    """Add the signed headers to ``headers`` and return them.

    ``credentials`` is the triple ``(access key, secret key, security token)``;
    the token is only carried when the runtime handed one over, which is the
    case for every call made through an agency.
    """
    headers = dict(headers or {})
    access_key, secret_key, token = credentials
    request_time = datetime.utcnow().strftime(BASIC_DATE_FORMAT)

    headers["Host"] = host
    headers[SDK_HEADER] = request_time
    if token:
        headers["X-Security-Token"] = token
    if body:
        headers.setdefault("Content-Type", "application/json")

    names = signed_header_names(headers)
    canonical = canonical_request(method, path, query, headers, names, body)
    headers["Authorization"] = header_value(
        access_key, names, sign(secret_key, string_to_sign(canonical, request_time))
    )
    # Added after signing: the transport writes it itself and a disagreement
    # between the signed value and the sent value would invalidate the
    # signature.
    headers["Content-Length"] = str(len(body or b""))
    return headers

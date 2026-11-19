"""Where a call gets its identity from.

The deployed functions never hold a key: the runtime assumes the agency of the
function and hands a short lived triple to the handler through the execution
context. That triple is the only source used in production.

For a run outside the runtime the same triple is read from the environment,
which is how the self-tests and an operator run exercise the real data path.
The keys are then whatever the operator's own profile is entitled to, so the
bucket statement has to cover that identity as well.
"""

import os

from . import config

ACCESS_KEY = "HW_ACCESS_KEY"
SECRET_KEY = "HW_SECRET_KEY"
SECURITY_TOKEN = "HW_SECURITY_TOKEN"


def from_context(context):
    """Temporary triple of the assumed agency, as issued by the runtime."""
    if context is None:
        return None
    readers = (
        getattr(context, "getSecurityAccessKey", None),
        getattr(context, "getSecuritySecretKey", None),
        getattr(context, "getSecurityToken", None),
    )
    if not all(callable(reader) for reader in readers):
        return None
    access_key, secret_key, token = (reader() for reader in readers)
    if not access_key or not secret_key:
        return None
    return (str(access_key), str(secret_key), str(token) if token else None)


def from_environment():
    access_key = os.environ.get(ACCESS_KEY, "").strip()
    secret_key = os.environ.get(SECRET_KEY, "").strip()
    token = os.environ.get(SECURITY_TOKEN, "").strip()
    if not access_key or not secret_key:
        return None
    return (access_key, secret_key, token or None)


def resolve(context=None):
    """The triple to sign with, or ``None`` when no identity is available."""
    return from_context(context) or from_environment()


def describe(triple):
    """Non secret rendering used in a failure message."""
    if not triple:
        return "no credentials: the agency was not assumed and %s is unset" % ACCESS_KEY
    return "access key ending %s" % triple[0][-4:]


def project(context=None):
    """Project the regional services are addressed under."""
    if context is not None:
        getter = getattr(context, "getProjectId", None)
        if callable(getter) and getter():
            return str(getter())
    return config.project_id() or os.environ.get("HW_PROJECT_ID", "")

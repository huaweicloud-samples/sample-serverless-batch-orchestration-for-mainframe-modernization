"""The environment contract of one batch container run.

Three places write into the environment of the container, and the names below
are the whole of what it reads:

* the ``batch-config`` map of the namespace carries the values that are constant
  for a deployment, see ``CONFIG_MAP_KEYS``;
* the job spec carries the two values that change from run to run, see
  ``RUN_KEYS``;
* the ``batch-obs-credentials`` secret carries the access keys of the run, see
  ``SECRET_KEYS``.

A value the container does not read is noise in a manifest; a value the
container reads and no manifest supplies is a run that stops on the first call.
Both lists are therefore kept in one place, and the container compares them
against its own environment before it touches the data: a name the deployment
forgot is reported as a readable reason and the run stops with a non-zero
status, rather than failing inside a call to the service.
"""

import os

# Served by the config map named batch-config.
CONFIG_MAP_KEYS = (
    "OBS_BUCKET",
    "OBS_ENDPOINT",
    "OBS_REGION",
    "INBOUND_PREFIX",
    "PROCESSING_PREFIX",
    "OUTBOUND_PREFIX",
    "LOG_LEVEL",
)

# Written per run by the job spec.
RUN_KEYS = ("OBS_PREFIX", "RUN_ID")

# Served by the optional secret named batch-obs-credentials.
SECRET_KEYS = ("HW_ACCESS_KEY", "HW_SECRET_KEY", "HW_SECURITY_TOKEN")

ALL_KEYS = CONFIG_MAP_KEYS + RUN_KEYS + SECRET_KEYS

CLOUD_SUFFIX = "myhuaweicloud.com"


def text(name, default=""):
    """Environment value as text, with the default used for an empty value."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = raw.strip()
    return raw or default


def log_level():
    return text("LOG_LEVEL", "INFO").upper()


def _slash(value):
    return value if value.endswith("/") else value + "/"


def _host(value):
    """Bare host of an endpoint: a scheme is accepted and dropped."""
    for scheme in ("https://", "http://"):
        if value.lower().startswith(scheme):
            return value[len(scheme):].strip("/")
    return value.strip("/")


class Settings:
    """Everything one container run needs besides its step name."""

    __slots__ = (
        "bucket",
        "endpoint",
        "region",
        "inbound_prefix",
        "processing_prefix",
        "outbound_prefix",
        "scope",
        "run_id",
        "access_key",
        "secret_key",
        "security_token",
        "log_level",
    )

    def __init__(self):
        self.bucket = text("OBS_BUCKET")
        self.region = text("OBS_REGION", "cn-north-4")
        self.endpoint = _host(
            text("OBS_ENDPOINT", "obs.%s.%s" % (self.region, CLOUD_SUFFIX))
        )
        self.inbound_prefix = _slash(text("INBOUND_PREFIX", "inbound/transactions/"))
        self.processing_prefix = _slash(
            text("PROCESSING_PREFIX", "processing/")
        )
        self.outbound_prefix = _slash(text("OUTBOUND_PREFIX", "outbound/"))
        # The scope of a run: empty for the scheduled production run, the run
        # folder of a manual or regression run otherwise. Everything the
        # container reads and writes stays inside it.
        self.scope = text("OBS_PREFIX")
        if self.scope:
            self.scope = _slash(self.scope.lstrip("/"))
        self.run_id = text("RUN_ID")
        self.access_key = text("HW_ACCESS_KEY")
        self.secret_key = text("HW_SECRET_KEY")
        self.security_token = text("HW_SECURITY_TOKEN") or None
        self.log_level = log_level()

    # -- validation -------------------------------------------------------

    @property
    def problems(self):
        """What stops this run before it starts, as readable phrases."""
        found = []
        if not self.bucket:
            found.append("OBS_BUCKET is empty: the batch-config map has no bucket")
        if not self.endpoint:
            found.append("OBS_ENDPOINT is empty: the batch-config map has no endpoint")
        if not self.access_key or not self.secret_key:
            found.append(
                "no access key pair: create the batch-obs-credentials secret with "
                "the literals HW_ACCESS_KEY and HW_SECRET_KEY"
            )
        return found

    def ready(self):
        return not self.problems

    # -- addressing -------------------------------------------------------

    def scope_of(self, base):
        """Absolute prefix of a data folder for this run.

        A folder name is always taken from the deployment, never written out by
        hand, so one container image moves between environments untouched.
        """
        return self.scope + _slash(base)

    def folder(self, base, *parts):
        """Absolute prefix of a step folder inside a data area."""
        joined = _slash(base)
        for part in parts:
            joined = _slash(joined + part)
        return self.scope_of(joined)

    def object_key(self, folder, name):
        """Absolute object key of a file name inside an already scoped folder."""
        return _slash(folder) + name

    # -- reporting --------------------------------------------------------

    def describe(self):
        """Single line identity of the run, safe to print: no key material."""
        return "bucket=%s endpoint=%s scope=%s run=%s" % (
            self.bucket,
            self.endpoint,
            self.scope or "(production)",
            self.run_id or "(none)",
        )

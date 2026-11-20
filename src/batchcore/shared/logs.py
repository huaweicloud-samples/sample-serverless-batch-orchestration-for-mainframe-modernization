"""Logging for the functions.

The runtime captures everything written to standard output and forwards it to
the log stream of the function, so a single stream handler with a
correlation-friendly format is enough. The first line a handler writes states
the entry point that was reached: when a cold start fails inside an import,
that line is the only evidence left in the stream.
"""

import logging
import sys

from . import config

_FORMAT = "[batch] %(levelname)s %(name)s %(message)s"
_configured = False


def get(name):
    """Child logger of the single configured root logger."""
    configure()
    return logging.getLogger("batch." + name)


def entry(name):
    """Announce the handler entry point before anything else happens."""
    get("entry").info("=== HANDLER ENTRY === %s", name)


def configure():
    """Install the stream handler once per container."""
    global _configured
    if _configured:
        return
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.log_level(), logging.INFO))
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(handler)
    _configured = True

"""Command line of the batch container.

One argument reaches the container, the name of the step, handed by the job spec
and forwarded by the entry point. Everything else the run needs comes from the
environment contract of the deployment, read once by
:class:`settings.Settings`.

The exit status is the only answer the orchestration layer gets back from a
step, so it is the whole of the protocol: zero lets the steps behind this one
start, anything else stops the chain and names the step that stopped it. A step
that ends in a traceback is a failed step, never a silence.
"""

import argparse
import logging
import sys

from . import steps
from .lake import StorageError, open_lake
from .settings import Settings

log = logging.getLogger("batch.job")

# Zero and non-zero is what the chain reads; the two non-zero values are kept
# apart because an unknown step is a fault of the job spec, while a failed step
# is a fault of the data or of the lake, and the operator who has to act is a
# different one in each case.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_UNKNOWN_STEP = 2

# The container writes its whole report to standard output, and the tail of it
# is what a failed step leaves behind, so the line is the evidence.
_FORMAT = "[batch] %(levelname)s %(name)s %(message)s"

DEFAULT_STEP = "JOB1"


def configure(level):
    """Install the one stream handler this container writes through."""
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))


def parse(argv):
    """The step name the container was handed, defaulting to the first step."""
    parser = argparse.ArgumentParser(
        prog="batch_job",
        description="Run one step of the batch chain over the data lake.",
    )
    parser.add_argument(
        "step",
        nargs="?",
        default=DEFAULT_STEP,
        metavar="STEP",
        help="step to run: %s (default: %s)" % ("|".join(steps.names()), DEFAULT_STEP),
    )
    # A stray argument is an operator typing at the wrong place; failing loudly
    # beats running a step nobody asked for.
    return parser.parse_args(argv).step


def main(argv=None, settings=None, lake=None):
    """Run one step and return the status the container exits with.

    ``settings`` and ``lake`` are handed in by a self-test, which drives this
    same command line over an in-memory lake. A container leaves both empty: the
    environment then carries the contract and the service carries the data.
    """
    name = parse(sys.argv[1:] if argv is None else list(argv))
    conf = settings if settings is not None else Settings()
    configure(conf.log_level)
    if name not in steps.STEPS:
        log.error("unknown step=%s known=%s", name, "|".join(steps.names()))
        return EXIT_UNKNOWN_STEP
    log.info("step=%s %s", name, conf.describe())
    for problem in conf.problems:
        log.error("configuration: %s", problem)
    if not conf.ready():
        return EXIT_FAILED

    data = None
    try:
        data = lake if lake is not None else open_lake(conf)
        steps.STEPS[name](conf, data)
    except StorageError as exc:
        # Absent input and a refused call are reported apart: an operator who
        # is told the data is missing goes looking for a delivery.
        log.error("step=%s the lake refused: %s", name, exc)
        return EXIT_FAILED
    except Exception:  # a step that cannot finish has failed, whatever it is
        log.exception("step=%s failed", name)
        return EXIT_FAILED
    finally:
        if data is not None:
            data.close()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

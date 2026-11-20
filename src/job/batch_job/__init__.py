"""Batch container of the orchestration sample.

One image, one step per run: the job spec names the step, this package reads
the environment contract of the deployment, pulls the input data set from the
object storage service, transforms the records and writes both the result data
set and a small marker of what happened.

The modules load without touching the storage client, so
the step logic runs in a plain interpreter with a stand-in for the service.
``python3 -m batch_job <STEP>`` is the command line the container entry point
runs; it is deliberately not re-exported here, because importing this package
from a script would then shadow the module runpy executes.
"""

from . import lake, settings, steps

__all__ = ["lake", "settings", "steps"]

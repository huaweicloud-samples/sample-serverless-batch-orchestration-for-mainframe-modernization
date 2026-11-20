"""The job chain, expressed as data.

The chain this solution runs is the one the job definitions describe: a first
step normalises the arriving extract, two steps work on its output at the same
time, a last step combines what those two produced. Everything the flow needs
to know is here: the identifiers, what each step reads before it may start and
what it produces for the steps behind it.

A regression run is the same chain with the gates opened: the modules asked
for are executed on their own, in any order, because the point is to exercise
one program, not to reproduce the chain.
"""

from collections import namedtuple

Step = namedtuple("Step", "identifier depends produces")

# The chain. An identifier is also the name handed to the batch container, the
# name the input validation knows and the label put on the job.
CHAIN = (
    Step("JOB1", (), ("JOB2A", "JOB2B")),
    Step("JOB2A", ("JOB1",), ("JOB3",)),
    Step("JOB2B", ("JOB1",), ("JOB3",)),
    Step("JOB3", ("JOB2A", "JOB2B"), ()),
)

BY_ID = {step.identifier: step for step in CHAIN}


def plan(identifiers=(), gated=True, concurrency=None):
    """The set of steps a run will execute, with the gates it respects.

    ``identifiers`` empty means the whole chain. Unknown identifiers are
    dropped by the caller, which reports them instead of guessing a step.
    """
    chosen = [BY_ID[name] for name in identifiers if name in BY_ID] if identifiers else list(CHAIN)
    if not gated:
        # The identifiers and what each of them reads stay, the gates go.
        chosen = [Step(step.identifier, (), step.produces) for step in chosen]
    return Plan(chosen, gated=gated, concurrency=concurrency)


class Plan:
    """A run's step set plus the two rules that decide its order."""

    def __init__(self, steps, gated=True, concurrency=None):
        self.steps = list(steps)
        self.gated = gated
        self.concurrency = concurrency

    def __iter__(self):
        return iter(self.steps)

    @property
    def identifiers(self):
        return [step.identifier for step in self.steps]

    @property
    def size(self):
        return len(self.steps)

    def dependencies(self, identifier):
        """What has to have succeeded before this step may start.

        An ungated plan has no dependencies at all: that is the whole of what
        makes a regression run independent of the chain order.
        """
        if not self.gated:
            return ()
        for step in self.steps:
            if step.identifier == identifier:
                # A dependency that is not part of this plan cannot be
                # satisfied, so it is dropped and the step stands on its own.
                return tuple(part for part in step.depends if part in self.identifiers)
        return ()

    def slots_free(self, running):
        """How many further steps may be started alongside ``running``."""
        if not self.concurrency:
            return len(self.steps)
        return max(0, int(self.concurrency) - running)

    def summary(self):
        return {
            "steps": self.identifiers,
            "gated": self.gated,
            "concurrency": self.concurrency,
            "shape": "chain" if self.gated else "independent modules",
        }

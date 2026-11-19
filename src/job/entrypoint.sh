#!/bin/sh
# Entry point of the batch container.
#
# The job spec hands the step name as the container argument, exactly the way
# the daily launch hands it: "JOB1", "JOB2A", "JOB2B" or "JOB3". Anything else
# on the command line is passed through untouched, so an operator can run one
# step by hand without editing the spec.
#
# The exit status of the Python module is the exit status of the container, and
# the orchestration layer reads that status to decide the next step.
set -eu

STEP="${1:-JOB1}"

exec python3 -m batch_job "$STEP"

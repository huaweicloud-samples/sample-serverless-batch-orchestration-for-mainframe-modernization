# Batch container image.
#
# One image carries every step of the chain: the job spec passes the step name
# as the container argument, and the container selects the step from it. The
# address this file builds towards is
#
#     <SWR_SERVER>/<SWR_ORGANIZATION>/<SWR_REPOSITORY>:<SWR_TAG>
#
# which is the same address src/job/build-and-push.sh pushes to and the address
# the batch job spec resolves into its image field.
#
# Build it from this directory:
#
#     docker build -t <SWR_SERVER>/<SWR_ORGANIZATION>/<SWR_REPOSITORY>:<SWR_TAG> .
#
# or let the push script do both steps:
#
#     SWR_SERVER=<registry host> SWR_PASSWORD=<registry token> ./build-and-push.sh

FROM python:3.11-slim

# No stray package cache inside the image, no buffered logging: a batch
# container writes its whole report to standard output and then exits, and the
# log is what an operator reads afterwards.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

# Dependencies first so that a source-only change reuses this layer.
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY batch_job/ ./batch_job/
COPY entrypoint.sh ./entrypoint.sh

# The step container runs unprivileged, with the user id and the group id the
# job spec asks for, so that a flaw in the record parsing cannot reach a root
# shell inside the node.
RUN chmod 0555 ./entrypoint.sh \
    && mkdir -p /work \
    && chown -R 10001:10001 /work
USER 10001:10001
WORKDIR /work

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["JOB1"]

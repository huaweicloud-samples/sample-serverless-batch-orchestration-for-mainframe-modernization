#!/bin/sh
# Build and push the batch container image.
#
# The four values below are the segments the batch job spec resolves into its
# image field, so they are read from the environment and never written into a
# manifest: the same address must appear as swr_organization / swr_repository /
# swr_image_tag of the deployment, or a job pulls a tag nobody built.
#
# Usage, from this directory:
#
#   SWR_SERVER=<registry host> SWR_ORGANIZATION=<organization> \
#   SWR_PASSWORD=<registry access key> ./build-and-push.sh
#
#   ./build-and-push.sh --dry-run     # build the image, do not log in or push
#   SKIP_PUSH=1 ./build-and-push.sh   # the same, stated without a flag
#   ./build-and-push.sh --help
#
# The access key is taken from the environment and handed to the login call on
# its standard input: it never reaches a printed line, a command argument or a
# file, because whatever an operator pastes into a terminal of this kind ends
# up in a scrollback or a recorded session.
set -eu

# Defaults are the values of a fresh deployment; the two that identify a
# particular account are deliberately left empty so that a run without them
# stops here instead of pushing to an address nobody chose.
SWR_REPOSITORY="${SWR_REPOSITORY:-batch-stub}"
SWR_TAG="${SWR_TAG:-1.0.0}"
DOCKER_BIN="${DOCKER_BIN:-docker}"
SKIP_PUSH="${SKIP_PUSH:-0}"

usage() {
  sed -n '2,21p' "$0"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)
      SKIP_PUSH=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1 (try --help)" >&2
      exit 2
      ;;
  esac
done

fail() {
  echo "$*" >&2
  exit 1
}

[ -n "${SWR_SERVER:-}" ] || fail "SWR_SERVER is empty: set it to the registry host, e.g. the value of the terraform output image_registry_host."
[ -n "${SWR_ORGANIZATION:-}" ] || fail "SWR_ORGANIZATION is empty: set it to the organization the image repository lives in."

# The login name of the registry is the organization, unless an operator names
# the identity that owns the access key explicitly.
SWR_USER="${SWR_USER:-$SWR_ORGANIZATION}"

IMAGE="$SWR_SERVER/$SWR_ORGANIZATION/$SWR_REPOSITORY:$SWR_TAG"

# The build context is the directory of this script: the image copies the
# package and the entry point from it by name.
CONTEXT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

command -v "$DOCKER_BIN" >/dev/null 2>&1 || fail "$DOCKER_BIN not found: install the container engine, or point DOCKER_BIN at it."

echo "building $IMAGE from $CONTEXT"
"$DOCKER_BIN" build -t "$IMAGE" "$CONTEXT"

if [ "$SKIP_PUSH" = "1" ]; then
  echo "push skipped (--dry-run or SKIP_PUSH=1): the image is local only, nothing was sent to $SWR_SERVER"
  exit 0
fi

[ -n "${SWR_PASSWORD:-}" ] || fail "SWR_PASSWORD is empty: set it to the registry access key of the organization, or run --dry-run to build without pushing."

# --password-stdin keeps the key out of the process arguments of the login call.
printf '%s\n' "$SWR_PASSWORD" | "$DOCKER_BIN" login -u "$SWR_USER" --password-stdin "$SWR_SERVER"
unset SWR_PASSWORD

echo "pushing $IMAGE"
"$DOCKER_BIN" push "$IMAGE"

echo "done: $IMAGE"
echo "check the address against the deployment: the batch job spec pulls exactly this image."

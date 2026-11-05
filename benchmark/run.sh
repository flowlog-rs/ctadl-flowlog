#!/usr/bin/env bash
# Load the normal CTADL environment, then invoke the benchmark scripts.
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"
exec python3 "$CTADL_ROOT/benchmark/run.py" "$@"

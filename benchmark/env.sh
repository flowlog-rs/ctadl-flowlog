#!/usr/bin/env bash
# Source this for normal ctadl commands as well as benchmark scripts.
CTADL_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [[ -f "$CTADL_ROOT/benchmark/local.env" ]]; then
    source "$CTADL_ROOT/benchmark/local.env"
fi
export FLOWLOG_SRC=${FLOWLOG_SRC:-"$CTADL_ROOT/../flowlog"}
export FLOWLOG_COMPILER="$FLOWLOG_SRC/target/release/flowlog-compiler"
export FLOWLOG_RUNTIME_PATH="$FLOWLOG_SRC/flowlog-runtime"
export PATH="$CTADL_ROOT/.venv/bin:$CTADL_ROOT/.local/bin:$PATH"
export CARGO_TARGET_DIR="$CTADL_ROOT/.cache/flowlog-target"
export XDG_STATE_HOME="$CTADL_ROOT/.local/state"
export XDG_CONFIG_HOME="$CTADL_ROOT/.local/config"
export XDG_CACHE_HOME="$CTADL_ROOT/.cache"
export TMPDIR="$CTADL_ROOT/.cache/tmp"
export PIP_CACHE_DIR="$CTADL_ROOT/.cache/pip"
export LC_ALL=C.UTF-8
if [[ -d "$CTADL_ROOT/.local/frontends/jdk-21.0.13+9" ]]; then
    export JAVA_HOME=${JAVA_HOME:-"$CTADL_ROOT/.local/frontends/jdk-21.0.13+9"}
    export PATH="$JAVA_HOME/bin:$PATH"
fi
if [[ -d "$CTADL_ROOT/.local/frontends/ghidra_11.2_PUBLIC" ]]; then
    export GHIDRA_HOME=${GHIDRA_HOME:-"$CTADL_ROOT/.local/frontends/ghidra_11.2_PUBLIC"}
fi
mkdir -p "$TMPDIR" "$XDG_STATE_HOME" "$XDG_CONFIG_HOME" "$CTADL_ROOT/.local/bin"

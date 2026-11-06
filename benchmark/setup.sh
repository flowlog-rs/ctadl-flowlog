#!/usr/bin/env bash
# Prepare the ordinary CTADL environment and build the latest FlowLog main.
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"
cd "$CTADL_ROOT"
setup_log="$CTADL_ROOT/benchmark/runs/setup-$(date -u +%Y%m%dT%H%M%S)-$$"
mkdir -p "$setup_log"
exec > >(tee "$setup_log/stdout.log") 2> >(tee "$setup_log/stderr.log" >&2)
for executable in git cargo rustc c++ souffle dune curl; do
    command -v "$executable" >/dev/null || { echo "Missing tool: $executable" >&2; exit 2; }
done
[[ -d "$FLOWLOG_SRC/.git" ]] || git clone --branch main https://github.com/flowlog-rs/flowlog.git "$FLOWLOG_SRC"
[[ $(git -C "$FLOWLOG_SRC" branch --show-current) == main && -z $(git -C "$FLOWLOG_SRC" status --porcelain) ]] || {
    echo "FLOWLOG_SRC must be a clean main checkout" >&2; exit 2;
}
git -C "$FLOWLOG_SRC" fetch origin main
git -C "$FLOWLOG_SRC" merge --ff-only origin/main
[[ $(git -C "$FLOWLOG_SRC" rev-parse HEAD) == $(git -C "$FLOWLOG_SRC" rev-parse origin/main) ]]
CARGO_TARGET_DIR="$FLOWLOG_SRC/target" cargo build --manifest-path "$FLOWLOG_SRC/Cargo.toml" \
    --release --locked -p flowlog-compiler -j "${CARGO_BUILD_JOBS:-8}"
python3 -m venv .venv
.venv/bin/python -m pip install -r benchmark/requirements.txt -e . -e plugins/taint-front -e plugins/ghidra -e plugins/networkx-export
(cd taint-front && dune build)
ln -sfn "$CTADL_ROOT/taint-front/_build/default/taint.exe" "$CTADL_ROOT/.local/bin/taintfront"
# Verify the actual compiler/runtime contract before recording a usable build.
python3 tests/flowlog/native_sqlite/run.py
python3 - <<'PY'
import hashlib, json, os, subprocess
from pathlib import Path
from ctadl.flowlog import SQLITE_IO_MODE
root = Path(os.environ['FLOWLOG_SRC'])
compiler = Path(os.environ['FLOWLOG_COMPILER'])
record = {'commit': subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'], text=True).strip(),
          'sqlite_io': SQLITE_IO_MODE,
          'compiler_sha256': hashlib.sha256(compiler.read_bytes()).hexdigest(),
          'rustc': subprocess.check_output(['rustc','--version'], text=True).strip(),
          'cargo_lock_sha256': hashlib.sha256((root/'Cargo.lock').read_bytes()).hexdigest()}
Path('benchmark/compiler.json').write_text(json.dumps(record, indent=2)+'\n')
PY
.venv/bin/python -m pip freeze --all > "$setup_log/packages.txt"
git -C "$FLOWLOG_SRC" rev-parse HEAD > "$setup_log/flowlog-commit.txt"
echo "Ready. source benchmark/env.sh; then use ctadl normally or python3 benchmark/run.py."
echo "Setup logs: $setup_log"

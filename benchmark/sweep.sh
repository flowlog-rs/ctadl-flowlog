#!/usr/bin/env bash
# Soufflé against FlowLog on CTADL's workloads, in one command; see benchmark/SWEEP.md.
set -euo pipefail
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
ROOT=$(dirname -- "$HERE")

usage() {
    cat <<'EOF'
Usage: bash benchmark/sweep.sh [options]

Runs benchmark/run.sh on one workload after another (both engines, index and query, 1 warm-up
and 3 measured runs each), checks FlowLog's choices on 4 apps, and writes DIR/report/README.md.
Run the same command again to resume: workloads whose latest run finished (complete or
needs_review) are skipped, the others run again.

  --setup             run setup.sh first, and setup-frontends.sh for P-code or JADX workloads
  --dir DIR           the sweep's status.tsv, logs/, choice-model/ and report/
                      (default benchmark/runs/sweep)
  --suite NAME        the workloads of a workloads.json suite (default all, 29 workloads)
  --workloads IDS     comma-separated workload IDs instead
  --workers N         workers of both engines (default 16)
  --choice-model IDS  JADX workloads whose FlowLog index to check as a choice model, or none
                      (default diary,editor,netguard,binaryeye)
  --redo              run the selected workloads again, finished or not
  --prune             delete the warm-up databases of finished workloads after their checks
  --reps N, --timeout SECONDS, --backends LIST, --offline, --keep-work
                      passed on to run.py
EOF
}

dir=$ROOT/benchmark/runs/sweep suite=all names='' workers=16 checks=diary,editor,netguard,binaryeye
setup=0 redo=0 prune=0 run_args=()
while (($#)); do
    case $1 in
        --setup) setup=1 ;;
        --redo) redo=1 ;;
        --prune) prune=1 ;;
        --offline | --keep-work) run_args+=("$1") ;;
        --dir | --suite | --workloads | --workers | --choice-model | --reps | --timeout | --backends)
            (($# > 1)) || { echo "sweep.sh: $1 needs a value" >&2; exit 2; }
            case $1 in
                --dir) dir=$2 ;;
                --suite) suite=$2 ;;
                --workloads) names=$2 ;;
                --workers) workers=$2 ;;
                --choice-model) checks=$2 ;;
                *) run_args+=("$1" "$2") ;;
            esac
            shift
            ;;
        -h | --help) usage; exit 0 ;;
        *) echo "sweep.sh: unknown option $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done
[[ $workers =~ ^[1-9][0-9]*$ ]] || { echo "sweep.sh: --workers takes one number" >&2; exit 2; }
mkdir -p -- "$dir/logs"
dir=$(cd -- "$dir" && pwd -P)
cd "$ROOT"
# run.py fails a run if the files git sees in the checkout change during it, as logs/ would; git
# check-ignore exits 0 for an ignored path, 1 for another in the checkout, 128 outside it.
rc=0
git check-ignore -q -- "$dir/status.tsv" 2>/dev/null || rc=$?
if ((rc == 1)); then
    echo "sweep.sh: git does not ignore $dir; use a directory in benchmark/runs/ or outside the checkout" >&2
    exit 2
fi
exec 9>"$dir/.lock"
flock -n 9 || { echo "sweep.sh: another sweep is using $dir" >&2; exit 2; }

# The workloads, in workloads.json's order: ID, language, whether to check the choice model.
selection=$(python3 - "$suite" "$names" "$checks" <<'EOF'
import sys
sys.path.insert(0, "benchmark")
from inputs import select
suite, names, checks = sys.argv[1], sys.argv[2] or None, sys.argv[3]
checks = [] if checks == "none" else checks.split(",")
try:
    if not select(suite):
        raise ValueError(f"no workloads in suite {suite}")
    rows = select(suite, names)
    if set(checks) - {r["id"] for r in select("jadx")}:
        raise ValueError("--choice-model takes JADX workloads, or none")
except ValueError as exc:
    sys.exit(f"sweep.sh: {exc}")
for r in rows:
    print(r["id"], r["lang"], int(r["id"] in checks))
EOF
) || exit 2
workloads=() frontends=0
declare -A check=()
while read -r w lang c; do
    workloads+=("$w")
    [[ $lang == taint-front ]] || frontends=1
    if ((c)); then check[$w]=1; fi
done <<<"$selection"

if ((setup)); then
    bash benchmark/setup.sh
    ((frontends == 0)) || bash benchmark/setup-frontends.sh
fi

status=$dir/status.tsv
[[ -s $status ]] || printf 'workload\texit\tstarted\tfinished\trun\n' >"$status"

# The exit code and run directory of the latest attempt at workload $1.
latest() {
    awk -F'\t' -v w="$1" '$1 == w { code = $2; run = $5 } END { print code, run }' "$status"
}

outcome() {
    case $1 in
        0) echo complete ;;
        4) echo needs_review ;;
        1) echo failed ;;
        130) echo interrupted ;;
        *) echo "error (exit $1)" ;;
    esac
}

# Is FlowLog's index of app $1, in run $2, a choice model of CTADL's index analysis? FlowLog
# indexes the run's facts again with --all-outputs, whose index outputs should equal the run's
# (same-index.txt); check_choice_model.py then runs Soufflé with each choice-domain relation
# restricted to FlowLog's witnesses, and compares what it derives with FlowLog's database.
choice_model() {
    local w=$1 run=$2 out=$dir/choice-model/$1 verdict
    local warm=$ROOT/$run/workloads/$w/flowlog/workers-$workers/warmup/work
    verdict=$(grep -hs '^choice model:' "$out/check.log") || true
    if [[ -n $verdict && $(cat "$out/run" 2>/dev/null) == "$run" ]]; then
        echo "    $verdict"
        return
    fi
    if [[ ! -f $warm/ctadlir.db ]]; then
        echo "    choice model: not checked, no FlowLog warm-up database"
        return
    fi
    rm -rf -- "$out"
    mkdir -p -- "$out/work"
    echo "$run" >"$out/run"
    cp -r -- "$warm/facts" "$out/work/facts"
    # The verdict is check.log's last line; any step that fails leaves none. Errexit is off in
    # this subshell (it runs under ||), hence the explicit exits.
    (
        source benchmark/env.sh || exit
        ctadl --directory "$out/work" index --engine flowlog -j "$workers" -f --all-outputs \
            >"$out/index.log" 2>&1 || exit
        outputs=$(python3 - "$warm/index.dl" <<'EOF'
import sys
sys.path.insert(0, "benchmark")
from check_choice_model import DRIVER_WRITTEN, Program
names = Program(open(sys.argv[1]).read()).output_names()
print(" ".join(f"--only {n}" for n in names if n not in DRIVER_WRITTEN))
EOF
        ) || exit
        read -ra outputs <<<"$outputs"
        python3 benchmark/compare_jadx.py "$warm/ctadlir.db" "$out/work/ctadlir.db" -q \
            --keys VirtualAssign=0,1,2,3,4,5,6 "${outputs[@]}" >"$out/same-index.txt" 2>&1
        python3 benchmark/compare_jadx.py ords "$out/work/ctadlir.db" >"$out/ords.tsv" || exit
        python3 benchmark/check_choice_model.py "$out/work/index.dl" "$out/work/facts" \
            "$out/work/ctadlir.db" "$out/check" -j "$workers" --ord-map "$out/ords.tsv" \
            >"$out/check.log" 2>&1
    ) || true
    verdict=$(grep -hs '^choice model:' "$out/check.log") \
        || verdict="choice model: check failed, see ${out#"$ROOT"/}"
    echo "    $verdict"
}

# What --prune frees once workload $1's checks are done: the warm-up databases of run $2,
# which run.py keeps when the engines' results differ, and the choice-model check's.
prune() {
    local w=$1 run=$2 out=$dir/choice-model/$1
    if [[ -n ${check[$w]:-} ]] && ! grep -qs '^choice model:' "$out/check.log"; then
        return
    fi
    rm -rf -- "$ROOT/$run/workloads/$w"/*/workers-*/warmup/work \
        "$out/work" "$out/check/db" "$out/check/facts"
}

n=${#workloads[@]} i=0 stop=0
echo "Sweep of $n workloads with $workers workers in ${dir#"$ROOT"/}; logs in logs/<workload>.log"
for w in "${workloads[@]}"; do
    i=$((i + 1))
    read -r rc run <<<"$(latest "$w")"
    [[ $run != - ]] || run=
    if ((redo)) || [[ $rc != 0 && $rc != 4 ]]; then
        started=$(date -u +%FT%TZ) t0=$SECONDS rc=0
        bash benchmark/run.sh --suite "$suite" --workloads "$w" --workers "$workers" "${run_args[@]}" \
            >"$dir/logs/$w.log" 2>&1 || rc=$?
        run=$(sed -nE 's#^(COMPLETE|NEEDS_REVIEW|FAILED|INTERRUPTED): .*/(benchmark/runs/[^/]+)$#\2#p' \
            "$dir/logs/$w.log" | tail -n 1)
        printf '%s\t%s\t%s\t%s\t%s\n' "$w" "$rc" "$started" "$(date -u +%FT%TZ)" "${run:--}" >>"$status"
        t=$((SECONDS - t0))
        printf '[%d/%d] %s: %s in %d:%02d:%02d%s\n' "$i" "$n" "$w" "$(outcome "$rc")" \
            $((t / 3600)) $((t % 3600 / 60)) $((t % 60)) "${run:+, $run}"
        if [[ $rc != 0 && $rc != 4 ]]; then
            tail -n 4 "$dir/logs/$w.log" | sed 's/^/    /'
        fi
        if [[ $rc == 2 || $rc == 130 ]]; then
            stop=$rc
            break
        fi
    else
        echo "[$i/$n] $w: $(outcome "$rc") before, $run"
    fi
    if [[ $rc == 0 || $rc == 4 ]]; then
        if [[ -n ${check[$w]:-} ]]; then choice_model "$w" "$run"; fi
        if ((prune)); then prune "$w" "$run"; fi
    fi
done

# Like run.py's: 0 all complete, 4 some need review (choice-domain witnesses differ), 1 some
# failed; 2 or 130 if a configuration error or an interrupt stopped the sweep.
code=$stop
if ((stop == 0)); then
    for w in "${workloads[@]}"; do
        read -r rc _ <<<"$(latest "$w")"
        case $rc in
            0) ;;
            4) ((code == 1)) || code=4 ;;
            *) code=1 ;;
        esac
    done
fi
echo
if ! python3 benchmark/report.py "$dir"; then
    ((code)) || code=1
fi
exit "$code"

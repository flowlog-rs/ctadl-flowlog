# The whole benchmark in one command

`sweep.sh` runs Soufflé and FlowLog on all 29 workloads of [`workloads.json`](workloads.json) (2
synthetic taint-front programs, 14 Ubuntu executables through Ghidra, 13 F-Droid apps through
JADX) with 16 workers, checks that their results agree, and writes a report.
[`results/`](results/README.md) is such a sweep.

## Run

On Linux, from the CTADL root, with the tools that [README.md](README.md) lists:

```bash
bash benchmark/sweep.sh --setup   # the first time: setup.sh and setup-frontends.sh, then the sweep
bash benchmark/sweep.sh           # resume: skips the workloads that finished, runs the rest
```

The report is `benchmark/runs/sweep/report/README.md`.

- `--setup` runs `setup.sh`, which clones or fast-forwards FlowLog `main` in `../flowlog` (a clean
  checkout), builds `flowlog-compiler`, installs CTADL into `.venv` and builds taint-front; then
  `setup-frontends.sh`, which installs Ghidra 11.2, JDK 21.0.13 and the JADX plugin 0.6.2. Tools
  that are not on `PATH` go in `benchmark/local.env`. On Debian and Ubuntu, `python3 -m venv`
  needs the `python3-venv` package.
- It takes about 10 hours (9 h 53 min on a 24-core AMD EPYC 7V13), so start it in `tmux` or with
  `nohup`. Ctrl-C stops it, and the same command resumes it.
- Leave the CTADL and FlowLog checkouts as they are until it finishes: `run.py` fails a workload
  whose sources change during its run.
- It needs 16 CPUs, at least 16 GiB of memory (FlowLog peaks at 11.8 GiB), and the network the
  first time, for the inputs, the frontends and the analyses' Rust crates. With several NUMA nodes,
  pin it to one: `numactl --cpunodebind=0 --membind=0 bash benchmark/sweep.sh`.
- It uses about 110 GiB of disk, mostly warm-up databases that `run.py` keeps when the engines'
  results differ. `--prune` deletes them once a workload is checked, for a peak of about 20 GiB.

## What it runs

For each workload, one `bash benchmark/run.sh --workloads W --workers 16` with its own
`benchmark/runs/<UTC time>-<pid>/` (see [README.md](README.md#每轮如何执行)). It imports the program
once, and runs each engine's index and query once to warm up, which also compiles the analyses.
Then it compares the two engines' databases and SARIF reports, and measures 3 more runs per engine,
swapping the engines' order every other run. FlowLog always compiles with string interning
(`--str-intern`, which `ctadl.flowlog` passes).

The engines may pick different witnesses in CTADL's choice-domain relations. Their databases then
differ, and `run.py` exits 4 (`needs_review`) on every workload. So for 4 apps (diary, editor,
netguard, binaryeye), the sweep checks that FlowLog's index is a choice model of the index analysis.
FlowLog indexes the facts again with `--all-outputs`, and `check_choice_model.py` checks that
Soufflé, with each choice-domain relation restricted to FlowLog's witnesses, derives FlowLog's
database.

## Options

| Option | |
|---|---|
| `--setup` | run `setup.sh` first, and `setup-frontends.sh` for P-code or JADX workloads |
| `--dir DIR` | the sweep's directory (default `benchmark/runs/sweep`), outside the checkout or git-ignored |
| `--suite NAME` | a suite of `workloads.json`: `all` (default), `synthetic`, `pcode`, `jadx` or `realistic` |
| `--workloads IDS` | comma-separated workload IDs, instead of a suite |
| `--workers N` | the workers of both engines (default 16) |
| `--choice-model IDS` | the apps to check, or `none` (default `diary,editor,netguard,binaryeye`) |
| `--redo` | run the selected workloads again, even if they finished |
| `--prune` | delete the warm-up databases of finished workloads after their checks |
| `--reps`, `--timeout`, `--backends`, `--offline`, `--keep-work` | passed on to `run.py` |

For a quick try of about an hour, including the setup:
`bash benchmark/sweep.sh --setup --workloads syn10,libz,diary --reps 1 --dir benchmark/runs/try`.

## Output

```text
benchmark/runs/sweep/
  status.tsv               one line per attempt: workload, exit code, start, end, run directory
  logs/<workload>.log      run.py's output
  choice-model/<app>/      the check: check.log (its verdict last), same-index.txt, and its files
  report/
    README.md              setup; Soufflé's time over FlowLog's per suite and workload; problems
    measurements.csv       all records of the runs
    comparisons.txt        how the engines' databases and SARIF reports differ
    choice-model.txt       the choice-model checks
```

In the report, x is Soufflé's time over FlowLog's: above 1, FlowLog is faster. Its Problems
section lists what needs a look: failed or timed-out runs, failed comparisons, choice models not
confirmed. `python3 benchmark/report.py [DIR]` writes the report again.

The exit code is 0 if all workloads completed, 4 if some need review (expected, see above), 1 if
some failed, 2 on a configuration error, and 130 if interrupted. The run directories listed in
`status.tsv` keep the raw records and databases; delete them with the sweep's directory when done.

# CTADL benchmark scripts

Analysis runs through the standard CTADL CLI. This directory prepares inputs,
invokes the CLI repeatedly, checks results, and saves logs. It does not provide a
separate analysis engine or generate charts. Input generators, comparison tools,
and correctness checks all live here.

FlowLog uses the native shared SQLite dictionary layout, which requires the
merged FlowLog #503. The standard CLI, tests, and benchmarks use the same path,
without copying SQLite relations into intermediate databases. The
`flowlog.sqlite_io` field in `manifest.json` and each measurement's `sqlite_io`
field are set to `native-shared-dictionary-v1`. Samples without the corresponding
CLI stage markers are rejected. `finalize_outputs` records the handling of field
names, indexes, empty outputs, and record display metadata; it is not a conversion
of the entire database.

## Environment and standalone usage

Run these commands from the CTADL repository root. On Linux, the prerequisites
are Python 3.10+, Rust, Soufflé 2.5 with SQLite support, a C++ compiler,
OCaml/dune/menhir, curl, GNU time, and Git. Configure tool paths and environment
variables in an uncommitted `benchmark/local.env`: PATH, CARGO_HOME, RUSTUP_HOME,
RUSTUP_TOOLCHAIN, CARGO_BUILD_JOBS, and FLOWLOG_SRC.

```bash
bash benchmark/setup.sh
source benchmark/env.sh
```

`setup.sh` updates the clean `main` checkout in the adjacent `../flowlog`
directory and builds the compiler with `--locked`. It installs this repository,
the local frontend plugins, and the networkx export plugin into `.venv`, builds
taint-front, and checks native SQLite reads and writes using the actual compiler.
After those checks pass, the compiler version, SHA-256, and SQLite I/O mode are
written to `compiler.json`; setup logs go to `runs/setup-*/`. FlowLog is updated
only when setup runs again; measurement scripts do not update code. The compiler
and `FLOWLOG_RUNTIME_PATH` refer to the same FlowLog checkout. CTADL's analysis
cache includes fingerprints of the local runtime's source and dependencies, so
runtime updates also trigger analysis recompilation.

You can then use CTADL independently:

```bash
ctadl import taint-front tests/taint-front/ex3.tnt -o .local/example
ctadl --directory .local/example index --engine flowlog -j 32 --timings .local/logs/index.jsonl
ctadl --directory .local/example query --engine flowlog -j 32 --timings .local/logs/query.jsonl
```

`--timings FILE` enables optional CTADL logging for either engine. It records stage
timings and captures subprocess stdout/stderr. These files are not generated when
the option is omitted. Paths are relative to the directory from which CTADL is
invoked.

## Running benchmarks

Once the environment is ready, start a run with one command. `run.sh` only loads
`env.sh` and invokes `run.py`:

```bash
# Minimal correctness and measurement check: one warmup and one measured run per engine by default
bash benchmark/run.sh --suite smoke

# Default: syn10 + syn25, 32 workers, three measured runs per engine
bash benchmark/run.sh

# Select inputs, worker counts, and repetitions
bash benchmark/run.sh --workloads syn10 --workers 1,32 --reps 3

# On a machine with multiple NUMA nodes, check CPU/node IDs with numactl --hardware first.
# On the example machine, physical cores 32-63 belong to node 1; bind CPUs and memory to that node.
numactl --physcpubind=32-63 --membind=1 bash benchmark/run.sh --workloads libz,diary --workers 1,4,8
```

`workloads.json` lists the inputs: one smoke workload, two synthetic programs,
14 Ubuntu binaries, and 13 F-Droid APKs. Synthetic programs use fixed random seeds.
Real inputs have pinned versions, and the SHA-256 of each extracted binary or APK
is verified. Runs prepare the selected inputs by default. You can also prepare
inputs separately:

```bash
python3 benchmark/prepare.py --workloads libz,diary
python3 benchmark/prepare.py --suite realistic
```

Real programs require additional frontends. The pinned versions are Ghidra 11.2,
JDK 21.0.13+9, and JADX plugin 0.6.2:

```bash
bash benchmark/setup-frontends.sh
source benchmark/env.sh
python3 benchmark/run.py --workloads libz,diary --reps 1
python3 benchmark/run.py --suite realistic
```

To use existing Ghidra/JDK installations, set GHIDRA_HOME and JAVA_HOME in
`local.env`. Results from different tool versions should be treated as separate
experiments. Input sources and checksums come from the
[original input manifest](https://github.com/flowlog-rs/ctadl-flowlog/blob/b11379f89779fc295eeeb4ef133dc285cd2678b5/flowlog-backend/results/realistic/inputs.txt).
If a pinned version is no longer available, preparation fails rather than silently
substituting a newer version. `--offline` requires real inputs to be cached;
the initial Rust analysis compilation may still access crates.io.

## Execution workflow

Each input is imported once. For each worker count, both engines warm up index and
query on separate copies of the facts. The databases are then compared, along
with SARIF reports for real programs. Measured runs use fresh copies of the facts.
Runs execute sequentially, reversing engine order on even-numbered repetitions.
Warmup records use `mode=warmup`; measured records use `mode=measure`. If a
compilation event occurs during a measured stage, the sample is marked
`invalid_metrics` and must not be used as performance data.

`manifest.json` records CPU affinity and NUMA memory policy. An outer `numactl`
command applies the NUMA binding, which the compiler, frontends, and both engines
inherit. Keep the binding consistent across runs.

Comparisons use the existing compare_db/compare_pcode/compare_jadx/compare_sarif
tools and retain their raw output. Different valid choice-domain witnesses can
produce differences. Even comparisons reporting only `WITNESS` differences with
matching keys require review; scripts do not automatically accept unexplained
differences as correct. A difference stops the smoke suite. Other workloads
continue measuring, but the overall run is marked `needs_review` and exits with
code 4. Selecting only one engine skips comparisons between engines.

A `needs_review` result requires investigation. Matching choice-domain keys alone
do not establish correctness. Use `benchmark/check_choice_model.py` to have
Soufflé derive results using FlowLog's choices, checking that the choices are
derivable and that all outputs match, including component outputs. Index checks
require the standard CLI's `--all-outputs` option; first verify that adding these
outputs does not change the original index results. Findings apply only to the
specific input, program, and version checked. Do not add a difference found in
one run to a global ignore list.

`run_taintfront.py` also marks witness or other comparison differences as
`needs_review`, returns 4, and retains the corresponding databases. `--prune`
removes databases only when comparisons match and tests pass.

By default, successful runs delete their working databases, facts copies, and
reports after recording results, while retaining logs and generated Datalog.
`--keep-work` preserves these working directories. Failed runs and warmup
databases with comparison differences are always retained. Real inputs and
compilation caches can be reused. Large collections of real inputs or retained
databases require more disk space; scripts do not automatically delete data from
other experiments.

## Log layout and fields

Each run creates a new `benchmark/runs/<UTC-timestamp>-<pid>/` directory without
overwriting previous runs:

```text
manifest.json                 Code versions, source/compiler hashes, environment, machine, inputs, final status
ctadl-source.tar.gz            CTADL source snapshot for this run, including uncommitted implementation changes
analysis-sources.tar.gz        Translated programs, schemas, UDFs, build logs, and available lockfiles from the build cache
events.jsonl                  Command start/end events
measurements.jsonl            Raw measurements for each stage
measurements.csv              The same raw records as CSV, without aggregation
workloads/<id>/
  input.json                  Actual input path and SHA-256
  import/                     Import command and raw logs
  checks/workers-32/flowlog/   Database/SARIF comparison logs and findings
  <engine>/workers-32/
    warmup/{index,query}/
    rep-001/{index,query}/
      command.json            argv, cwd, UTC start/end, exit code, timeout, wall time, load before launch
      stdout.log              Raw stdout from the standard CTADL command
      stderr.log              Raw stderr from the standard CTADL command
      resources.txt           Raw GNU time -v output
      timings.jsonl           CTADL's stage and subprocess records
      timings.jsonl.commands/ Captured raw subprocess stdout/stderr
      measurement.json        Structured measurements for this stage
      index.dl / query.dl     Datalog actually passed to the analysis layer
```

| Field | Meaning |
|---|---|
| `wall_seconds` | Time for the entire CTADL command, including the driver, model generation, analysis, and reporting; warmups may also include compilation |
| `analyze_seconds` | Time for the analysis call; for FlowLog, this includes input preparation, the native engine, and output metadata handling |
| `engine_seconds` | Time running the analysis binary, including the engine's own I/O and SQLite output; not pure fixpoint computation time |
| `process_peak_rss_mib` | Maximum RSS recorded by GNU time for the CTADL process and its subprocesses, in MiB; not the sum of their RSS values or a measurement of the engine alone |
| `mode` / `repetition` | `warmup` uses repetition 0; `measure` starts at 1 |
| `status` | Success, failure, timeout, launch failure, or invalid measurement; missing values remain null or empty CSV cells |

Overall exit codes are 0 for completion, 1 for execution or measurement failure,
2 for configuration errors, 4 for comparison results requiring review, and 130 for
user interruption. A timeout on the outer command terminates the entire subprocess
group and saves logs. If index fails, query is skipped for that sample. Keep runs
with different machines, source code, or inputs separate. Frontends can consume
these records directly.

## Validation scripts

```bash
python3 -m unittest tests.test_timings tests.test_flowlog_runtime_cache tests.test_facts_writer tests.test_sarif_path_order tests.test_choice_model
python3 -m unittest discover -s benchmark -p 'test_*.py'
```

# FlowLog in CTADL

FlowLog is an engine for the normal `ctadl index` and `ctadl query` commands.
Imports, models, the index database and report generation use CTADL's existing
pipeline. Soufflé remains the default engine.

## Setup and use

The [benchmark setup](../benchmark/README.md) builds a clean checkout of FlowLog
`main` next to CTADL, installs CTADL and configures the local environment:

```sh
bash benchmark/setup.sh
source benchmark/env.sh
ctadl import taint-front tests/taint-front/ex3.tnt -o .local/example
ctadl --directory .local/example index --engine flowlog -j 32
ctadl --directory .local/example query --engine flowlog -j 32
```

For an existing installation, set `FLOWLOG_COMPILER` to `flowlog-compiler` and
`FLOWLOG_RUNTIME_PATH` to the matching checkout's `flowlog-runtime` directory.
`CARGO_TARGET_DIR` can share Rust dependencies between analysis compilations.
`CTADL_ENGINE=flowlog` selects FlowLog without adding `--engine` to every command.

The first use of an analysis compiles it. CTADL caches the executable along with
the translated program, schema and compilation logs in its XDG state directory.
The cache key covers the analysis, translator, policy, UDFs, compiler identity,
and the local runtime sources and dependency manifests. Updating the runtime
invalidates cached analyses. Keep compiler and runtime on the same revision.

## Execution and logs

CTADL preprocesses its Soufflé analysis and translates it with
[`ctadl.flowlog`](../src/ctadl/flowlog). The Rust functors and translation policy
live next to the Soufflé analysis sources. Input and output adapters preserve
CTADL's SQLite layout, so either engine can query an index produced by the other.
Writing query outputs preserves existing tables and indexes.

Use `--timings FILE` on a normal command to record phase timings and captured
subprocess output. It is optional and works for both engines:

```sh
ctadl --directory .local/example index --engine flowlog \
  --timings .local/logs/index.jsonl
```

The path is relative to the caller's working directory. The JSONL records
distinguish compilation, input adaptation, engine execution, output adaptation
and report work. Captured subprocess streams are stored beside the JSONL file.
[Benchmark scripts](../benchmark/README.md) add command metadata, resource usage,
input checksums and source snapshots while calling the same CLI.

## Correctness and limits

Choice-domain relations can select different witnesses in the two engines.
Inside recursion, a different choice can also change contexts, summaries and
downstream relations. Equal choice keys alone do not establish correctness.
The benchmark keeps such differences for review.

`benchmark/check_choice_model.py` checks whether Soufflé can derive FlowLog's
results when constrained to its choices. It checks qualified component outputs
as well as top-level outputs. The index must be run with `--all-outputs` so that
internal choices are available; first compare its normal outputs with the
original index. A successful check applies to that program, input and revision.
Engine-local record and symbol IDs need the schema-aware comparison tools.

P-code and JADX also use `ord`, whose numbering can differ between engines.
`compare_pcode.py`, `compare_jadx.py` and `ord_oracle.sh` support investigating
those differences. Unsupported translation constructs produce an error;
FlowLog does not support Soufflé's interpreter mode or `--souffle-arg` options.

## Validation

```sh
python3 -m unittest tests.test_timings tests.test_flowlog_runtime_cache \
  tests.test_choice_model tests.test_facts_writer tests.test_sarif_path_order
python3 -m unittest discover -s benchmark -p 'test_*.py'
bash tests/flowlog/adapter/run.sh
bash tests/flowlog/semantics/run.sh
bash tests/flowlog/choice_mini/run.sh
python3 benchmark/run_taintfront.py --backend both --prune \
  --schema benchmark/index_schema.json --schema benchmark/query_schema.json
bash benchmark/run.sh --suite smoke
```

The semantics and taint-front suites include compilation and can take time.
Historical experiments are retained in the
[original PR branch](https://github.com/flowlog-rs/ctadl-flowlog/tree/b11379f89779fc295eeeb4ef133dc285cd2678b5/flowlog-backend).
Their performance numbers describe their recorded revisions, not a newer run.

# CTADL on FlowLog

This fork runs [CTADL](https://github.com/sandialabs/ctadl)'s taint analysis on
[FlowLog](https://github.com/flowlog-rs/flowlog) instead of Soufflé, with `ctadl index --engine
flowlog` and `ctadl query --engine flowlog` ([Usage](#usage)). CTADL's analysis is written as
Soufflé Datalog programs. The questions:

- Can FlowLog, as it is today, express the computation?
- What FlowLog-specific logic does it need, for example Rust UDFs?

[ctadl-rs](https://github.com/sandialabs/ctadl-rs), CTADL's Rust/Ascent reimplementation, was used
as a reference and as an extra test corpus.

## Verdict

**FlowLog can express all of CTADL, given a translation layer.** CTADL's complete index and query
analyses run on FlowLog and produce the same results as Soufflé:

- **Programs:** the index has about 171 relations and 240 rules, the query about 173 relations and
  134 rules. Both use components, `choice-domain`, functors, records, `inline`, `range`, and more.
- **Differential runs:** 435 runs, each comparing every database relation (120 per run, 143
  with `--all-outputs`) and the SARIF report. Every run is identical except one. In that run,
  FlowLog picked a different but valid `choice-domain` witness ([Limitations](#limitations)).
- **CTADL's own tests:** with `CTADL_ENGINE=flowlog`, `tests/bin/check-taintfront` passes, as with
  Soufflé (`results/check-taintfront.txt`). `check-taintfront2` passes too, except its two steps
  that run Soufflé's interpreter (`--no-compile-analysis`), which the engine refuses. The steps of
  `check-pcode`, `check-analysis-options` and `check-ActivityCommunication4`, which use the Ghidra
  (P-code) and JADX front ends, pass on inputs like theirs ([Front ends](#front-ends)), and
  `check-static` (black, pyright) passes. Instead of `checksarif`, the SARIF reports were
  validated against SARIF's JSON schema: each FlowLog report has the same violations as Soufflé's
  report of the same run, if any ([Front ends](#front-ends)).
- **Front ends:** on curl and nginx (P-code) and on DroidBench's Android apps (JADX), the
  databases, SARIF reports, GML exports and slices are Soufflé's. Two kinds of difference remain,
  both valid. CTADL's statistics count rows by a `choice-domain` witness, which the engines pick
  differently. The GML exporter takes a node's type from the last of its database rows, and the
  engines order rows differently. Of DroidBench's 188 apps, 111 are identical, and 4 differ only
  in such a GML type. The other 73 fail to load with either engine: CTADL's JADX plugin writes
  facts that CTADL's analysis does not accept ([Front ends](#front-ends)).
- **Semantics suite:** small programs check each translated construct against Soufflé. 20 programs
  give Soufflé's rows with 1, 4 and 32 workers; on 11 more, Soufflé stops with an error and so does
  FlowLog. The translator refuses 41 programs it cannot translate faithfully, rejects 7 erroneous
  programs as Soufflé does, and warns about 3 ([Validation](#validation)).
- **Integers:** Soufflé computes on 64 bits with wrap-around. FlowLog types a literal `int32`
  where no column types it. The translator computes constant expressions itself and pins the rest
  to 64 bits with FlowLog's `as` ([Integers](#integers)). CTADL's program needs none of this.
- **Runtime errors:** Soufflé stops a program on `to_number` of a non-number and on division by
  zero. The port stops too wherever it can prove that Soufflé evaluates the call, and warns about
  the other calls. CTADL's three such calls parse option values that cannot fail
  ([Runtime errors](#runtime-errors)).
- **Larger programs:** on 30 generated programs, `choice-domain` keys often get tied candidates.
  Soufflé breaks such ties by thread scheduling and the port by a hash. In 9 programs this changes
  downstream relations. Every FlowLog result is a valid choice model of the program. Soufflé's
  own results also vary: with 32 threads, its downstream relations differ from its 1-thread
  results in 6 of the 30 programs, 3 in an earlier sweep ([Larger programs](#larger-programs)).

FlowLog does not accept CTADL's program as is. It took four things:

1. **A Soufflé→FlowLog translator** (`souffle2flowlog.py`). It rewrites the Soufflé features
   FlowLog lacks or spells differently ([table](#translation)). Most rewrites are syntactic.
   `choice-domain` is the one that needs real design work.
2. **Rust UDFs**, loaded through FlowLog's `.extern fn` and `--udf-file`:
   - Ports of CTADL's 13 C++ functors (call strings, access paths, substring checks), in
     `functors.rs` next to CTADL's `functors.cpp`. Soufflé loads these from a C++ shared library.
   - Soufflé's semantics for builtins that FlowLog computes differently (`strlen`, `substr`,
     `to_number`, `^`), and for `/` and `%`, which panic in FlowLog on a zero divisor, also
     where Soufflé may never divide ([Runtime errors](#runtime-errors)). FlowLog's `ord`, an
     `int32`, is widened to Soufflé's 64-bit `number`.
   - Stateful helpers that emulate `choice-domain`:
     - `choice_keyN` and `choice_field` intern a candidate's dependent columns into an `int64`
       key, so that a FlowLog `min` lattice keeps one deterministic witness per key.
     - `choice_claimN` fixes the witness that reads see inside recursion.

   The functor ports are CTADL's; the other UDFs are the translator's (`souffle2flowlog.rs`), for
   any program it translates. UDFs fit the functors naturally. For `choice-domain` they are a
   workaround: the claims are mutable state outside the dataflow ([details](#choice-domain)).
3. **One FlowLog compiler fix**, for an internal compiler error that CTADL's string constraints
   trigger (B1, [flowlog-rs/flowlog#464](https://github.com/flowlog-rs/flowlog/issues/464)). The
   fix, [flowlog-rs/flowlog#477](https://github.com/flowlog-rs/flowlog/pull/477), is merged into
   FlowLog's `main`. For other programs, the translator also relies on the fixes for B2, B4, B5
   and B10–B13 ([Setup](#setup)).
4. **An engine option in CTADL:** `ctadl index` and `ctadl query` take `--engine flowlog`. The
   engine, the package `ctadl.flowlog`, translates and compiles the analysis, caches the binary
   as CTADL caches Soufflé's, and converts the run's inputs and outputs:
   - sqlite layout adapters between FlowLog's plain tables and the Soufflé symbol-table layout
     that CTADL's Python tooling reads;
   - a fact-file converter.

**Performance** on the larger generated program (syn25), in engine time:

- **With 1 worker,** FlowLog's index takes half of Soufflé's time, and its query is 1.1× as fast.
- **With 32 workers,** contention in FlowLog's string interner holds it back: its index takes 2%
  longer than Soufflé's, and its query is 1.8× as fast. With the interner fix,
  [#499](https://github.com/flowlog-rs/flowlog/pull/499) (since merged), its index is 3.9× as fast
  as Soufflé's and its query 2.2× ([With the interner fix](#with-the-interner-fix)).
- **Join order:** CTADL gives Soufflé hand-written join orders (`.plan`: 67 in the index, 6 in the
  query), one per semi-naive version of a recursive rule. FlowLog's `.plan` takes one order per
  rule, so the translation keeps the first version's orders, which 4 hints give; they halve
  FlowLog's index time with 1 worker. Without any hints, both engines join in the order the rules
  are written (FlowLog has no SIPS). FlowLog's index is then 2.7× as fast as Soufflé's with 1
  worker, and 1.5× with 32, or 4.5× with #499 ([Join order](#join-order)).
- **End to end,** FlowLog's index phase takes about half of Soufflé's wall time, with 1 worker, or
  with 32 and #499. Its query phase takes 1.1–1.2× as long as Soufflé's: the port converts
  FlowLog's results into CTADL's sqlite layout, which takes about 19 s.
- **Memory:** FlowLog's index needs 2.3–7.5× Soufflé's peak memory.
- **Compiling** takes 1.2–1.8× as long, and the binaries are 19–28× larger
  ([Performance](#performance)).

**FlowLog bugs:** the port found fifteen (B1–B15), each with a minimal repro in `repros/`. B1–B13
are filed on FlowLog (B7 for `^` only), and its `main` fixes all of them
([FlowLog findings](#flowlog-findings)). CTADL's program needs the fix for B1. The translator used
to work around B2, B4, B5 and B10–B13; it now relies on FlowLog's fixes instead. B14 can give a
compilation the binary of another program compiled at the same time; the engine and the tests
avoid it. B15 is an internal compiler error on a rule whose body has only negated atoms, which the
translator anchors on a one-row relation.

**Status: an end-to-end alternative, opt-in, not yet the default.** With `--engine flowlog`, CTADL
indexes and queries programs from its three front ends (taint-front, Ghidra's P-code, JADX), with
every option but the two that run Soufflé's interpreter, and its exports, reports and checks give
Soufflé's results. What keeps it from replacing Soufflé as the default:

- **No FlowLog release has the B1 fix:** the latest, `flowlog-compiler-v0.8.0`, predates #477, so
  the engine needs FlowLog built from `main` ([Setup](#setup)).
- **CI:** CTADL's CI (`nix run .#test`) builds Soufflé only, so nothing tests the engine on each
  change.
- **Cost:** FlowLog is faster on large inputs (on nginx, 2.5× in both phases), but needs 2–9×
  Soufflé's peak memory (19 GB for nginx's index), and about 70 MB per worker on any input (2.3 GB
  with CTADL's default of 32 workers on this machine, for an app that Soufflé indexes in 0.05 GB).
  The compiled analyses take 110–250 MB each, 19–28× Soufflé's.
- **Symbol numbering:** P-code's type propagation depends on the engines' symbol ids, which on
  curl and nginx coincide but are not guaranteed to ([Limitations](#limitations)).

## Layout

The fork is CTADL 0.14.1 (`4f6f8cb`) with a FlowLog engine: the files below, and the `--engine`
option of CTADL's command-line driver, `bin/ctadl`. CTADL's analyses and the rest of its code are
unchanged. The scripts that test and benchmark the engine expect a workspace that holds the fork,
FlowLog and ctadl-rs side by side. Run them from the workspace:

```
workspace/
  ctadl/                this fork; taint-front front end built with dune
  flowlog/              FlowLog main (b99ed29), which has the fixes the port relies on
  ctadl-rs/             ctadl-rs (7c98b73e), for its .tnt test corpus
  bin/taintfront        symlink to ctadl/taint-front/_build/default/taint.exe
  .venv/                Python env with ctadl and its taint-front plugin
  fltarget/             cargo target dir shared by all FlowLog compilations
  bench/                synthetic benchmark programs (gen_tnt.py)
  runs/                 output of the test and benchmark runs
```

The engine, in `ctadl/`:

```
src/ctadl/flowlog/        the FlowLog engine, package ctadl.flowlog
  __init__.py             compiles an analysis (cached) and converts the inputs and outputs of
                          its runs; bin/ctadl's FlowLogCompiledAnalysis runs it
  souffle2flowlog.py      Soufflé -> FlowLog translator (also a CLI)
  souffle2flowlog.rs      the translator's Rust UDFs: Soufflé builtins, choice helpers
  sqlite_layout.py        sqlite layout conversion (FlowLog <-> Soufflé)
  facts.py                input and output files, by component instance (record columns
                          and nullary facts in Soufflé's text)
src/ctadl/souffle-logic/  CTADL's analyses, and:
  functors.rs             Rust port of functors.cpp, CTADL's functors
  flowlog-policy.json     choice-domain policies for CTADL's relations
tests/flowlog/
  semantics/              semantics suite: the translator and UDFs against Soufflé
  choice_mini/            regression test for the recursive choice-domain emulation
  adapter/                the engine's file and sqlite I/O (facts.py, sqlite_layout.py),
                          against Soufflé's
```

The experiment, `ctadl/flowlog-backend/`:

```
index_tf.fl, query_tf.fl,            the translated taint-front analyses and their
index_schema.json, query_schema.json   schema manifests (for reference; the engine
                                       regenerates them)
run_taintfront.py     differential runner (CTADL tests or any .tnt corpus)
run_variants.sh       differential runs under non-default CTADL options
run_sweep.sh          differential runs on generated programs: Soufflé -j 1 against Soufflé
                      and FlowLog on JOBS workers, and the choice-model check
run_engine_checks.sh  the engine option: queries on the other engine's index, CTADL's
                      check-taintfront2, a --dl fragment's file I/O, two FlowLog runs
                      compiling it at once
run_front_ends.sh     CTADL's Ghidra (P-code) and JADX checks with both engines, compared
run_droidbench.sh     Android apps (DroidBench) with both engines, compared
compare_db.py         relation-by-relation DB comparison
compare_sarif.py      SARIF comparison
check_sarif_schema.py SARIF reports against SARIF 2.1.0's JSON schema, which CTADL's tests
                      check with checksarif; FlowLog's violations against Soufflé's
compare_gml.py        GML comparison: nodes and edges as multisets; the `type` and `ctx` that
                      the exporter takes from the last of several rows are compared apart
compare_pcode.py,     compare_db.py for P-code and JADX DBs, with the ids that CTADL computes
  compare_jadx.py       with `ord` renamed; compare_jadx.py also renames them in SARIF and GML
ord_oracle.sh, .cpp   P-code's type propagation, which orders by `ord`: Soufflé with
                      FlowLog's numbering, compared with FlowLog
check_choice_model.py checks that a FlowLog DB is a valid choice model (Soufflé on the
                      program restricted to FlowLog's choice-domain witnesses)
bench.py, gen_tnt.py  benchmark harness, synthetic program generator
bench_reps.py         repeated benchmark runs, and their medians
repros/               minimal programs for each FlowLog bug/gap, check.sh; same_name.sh (B14)
results/              summaries of the runs reported here
```

## Setup

You need Soufflé 2.5 (the reference engine), stable Rust (1.95 was used), Python 3.10 or later,
OCaml with dune (for CTADL's taint-front front end), and sqlite3.

```bash
mkdir workspace && cd workspace
git clone -b flowlog-backend https://github.com/flowlog-rs/ctadl-flowlog ctadl
git clone https://github.com/flowlog-rs/flowlog
(cd flowlog && git checkout -b ctadl-port b99ed29 && cargo build --release)
git clone https://github.com/sandialabs/ctadl-rs && git -C ctadl-rs checkout 7c98b73e
python3 -m venv .venv && . .venv/bin/activate
pip install -e ctadl -e ctadl/plugins/taint-front
(cd ctadl/taint-front && dune build)
mkdir -p bin && ln -s "$PWD/ctadl/taint-front/_build/default/taint.exe" bin/taintfront
export PATH=$PWD/bin:$PATH CARGO_TARGET_DIR=$PWD/fltarget \
       FLOWLOG_COMPILER=$PWD/flowlog/target/release/flowlog-compiler \
       FLOWLOG_RUNTIME_PATH=$PWD/flowlog/flowlog-runtime
```

`ctadl-port` is FlowLog's `main` at `b99ed29` (#499). CTADL's analyses need the B1 fix, PR
[#477](https://github.com/flowlog-rs/flowlog/pull/477). The translator relies on the fixes for B2,
B4, B5 and B10–B13 as well ([FlowLog findings](#flowlog-findings)), so the semantics suite and
`repros/check.sh -t` need `main` at `c605e65` (#484) or later. On `b99ed29`, CTADL's taint-front
tests, both corpora, the option variants, the semantics suite, the `choice-domain` test and the
front ends give the results reported here ([Validation](#validation)). The benchmarks used `main`
at `84b3a1c` plus #477 as it was before it was merged (`fed6293`), except those [with the
interner fix](#with-the-interner-fix). The translator's later simplifications leave CTADL's
FlowLog programs (`index_tf.fl`, `query_tf.fl`) unchanged, byte for byte, but for the `ord` fix:
`souffle_ord`'s declaration and its 3 calls in each program. The fixed programs are as fast: on
syn25 with 32 workers, medians of 3 runs on `b99ed29`, FlowLog's engine took 3.47 s for the index
(3.43 s before the fix) and 17.0 s for the query (17.9 s). On syn10, the query's fell from 2.37 s
to 0.77 s (`results/bench/ord-ab.txt`).

The runs write large databases (1.07–1.10 GB each on the larger benchmark program), and the compiled
analyses and the cargo target dir take a few GB ([Disk usage](#disk-usage)). Put the workspace,
CTADL's state directory (`XDG_STATE_HOME`, where the compiled analyses are cached) and `TMPDIR` on
a disk with room.

## Usage

`ctadl index` and `ctadl query` take `--engine flowlog`, and `CTADL_ENGINE=flowlog` makes it the
default:

```bash
ctadl import taint-front prog.tnt -o work
ctadl --directory work index --engine flowlog -j 8
ctadl --directory work query --engine flowlog --format sarif -o prog.sarif
```

- **First compile:** the first `index` or `query` with a given analysis program translates and
  compiles it, which takes a few minutes. The binary is cached next to CTADL's Soufflé binaries,
  in `~/.local/state/ctadl/analysis/<version>/flowlog/<key>`, with the translation, its schema
  manifest and the compiler's log.
- **Cache key:** it covers the program, the translator and its UDFs, `flowlog-policy.json`,
  `functors.rs`, the compiler binary and `FLOWLOG_RUNTIME_PATH`. Editing any of them triggers a
  recompile; editing the runtime's source does not.
- **Mixing engines:** the index database has Soufflé's layout whichever engine wrote it, and a
  query with either engine reads an index from either.
- **Options:** CTADL's options work with both engines, except `--souffle-arg` and
  `--no-compile-analysis` (Soufflé's interpreter), which need `--engine souffle`. `ctadl` refuses
  them with FlowLog before it touches any output, so `index -f` keeps the existing index.
- **Environment variables:** `CTADL_ENGINE`; `FLOWLOG_COMPILER` (default: `flowlog-compiler` on
  `PATH`); `FLOWLOG_RUNTIME_PATH`, the `flowlog-runtime` source that the compiler builds against
  (default: the crates.io release it was built with); `CARGO_TARGET_DIR`, the cargo target dir
  that the compilations share (default: in the cache).

The translator also works standalone:

```bash
F=ctadl/src/ctadl/flowlog L=ctadl/src/ctadl/souffle-logic
python3 $F/souffle2flowlog.py analysis.dl -o analysis.fl --policy $L/flowlog-policy.json --schema schema.json
cat $F/souffle2flowlog.rs $L/functors.rs > udf.rs
flowlog-compiler analysis.fl --str-intern --udf-file udf.rs -o analysis
```

Without `--policy`, every `choice-domain` is encoded as `minchoice` ([choice-domain](#choice-domain)).
A program that uses none of CTADL's functors needs only `souffle2flowlog.rs`.

## How it works

### Pipeline

CTADL's own command-line driver does all the work except running the Datalog analysis:

- importing,
- model generation,
- macro preprocessing of the `.dl` sources,
- index metadata,
- query views,
- SARIF export.

With `--engine flowlog`, CTADL's `build_analysis` returns a `FlowLogCompiledAnalysis` instead of
a `SouffleCompiledAnalysis`. It has the same interface, so `index` and `query` use it unchanged.
It works in three steps (`ctadl.flowlog`):

1. **Translate.** `souffle2flowlog.py` translates the preprocessed Soufflé program into
   `program.fl`, with the policies in `flowlog-policy.json`. It also writes `schema.json`, a
   manifest of the sqlite and file inputs and outputs, with their original attribute names and
   flattened record columns. A directive inside a component gets one entry per instance,
   `inst.R`, which is also the default file name of both engines.
2. **Compile.** `flowlog-compiler --str-intern --udf-file udf.rs` compiles the program, where
   `udf.rs` is `souffle2flowlog.rs` followed by `functors.rs`. It builds in a scratch directory
   that it deletes afterwards ([Disk usage](#disk-usage)).
3. **Run.** The binary runs as `-F in -D out -w <jobs>`, on a scratch directory in CTADL's
   `--tmpdir`. Around the run:
   - **Inputs:** `facts.py` links each input file into the run's input directory. Where the
     input has record columns, it reads the records as Soufflé does instead and writes their
     fields as columns. sqlite inputs, which the query phase reads from the index DB, are
     converted to FlowLog's layout by `sqlite_layout.py`. Inputs that no rule reads are created
     empty. CTADL's query declares 46 of them, including three with record columns. An input
     file or table that is missing is left out: like Soufflé, FlowLog then fails if it reads
     the relation.
   - **Outputs:** FlowLog writes each relation as a plain table. `sqlite_layout.py` converts the
     outputs to Soufflé's layout (a `__SymbolTable`, shadow tables `_R` holding ids, and views
     `R`) and appends them to CTADL's database. CTADL's Python tooling and SQL views therefore
     work unchanged. Output files are placed where Soufflé writes them, with record columns in
     Soufflé's text (`[1, a]`) and a nullary fact as `()`, where FlowLog writes `True`. FlowLog
     writes no table or file for an output that it prunes, because nothing derives it; Soufflé
     writes an empty one, and so does the engine. FlowLog fails on an output file whose
     directory is missing, so the engine creates the directories.

   Both conversions are `INSERT ... SELECT` statements that SQLite runs on the two databases,
   attached to one connection. They intern the symbols with `INSERT OR IGNORE`, then fill each
   table through lookups in the symbol table's unique index; no rows pass through Python.

<a id="translation"></a>
### Translation

Each rewrite has a minimal program in `repros/`. The repro shows what FlowLog does with the
Soufflé construct; `repros/check.sh -t` runs the rewrite. A few repros are FlowLog programs, as
they need FlowLog's `int64` type or its UDFs; the semantics suite tests those rewrites.

| Soufflé construct (as CTADL uses it) | FlowLog encoding | Repro |
|---|---|---|
| `.comp` declared inside a `.comp` | Hoisted to top level. FlowLog cannot resolve a nested component from a nested `.init`. Relation names in a component's clauses resolve as in Soufflé ([below](#components)). | `nested_comp` |
| Record types `[a: T, b: U]` | One column per field (`attr_field`). Fact and output files keep Soufflé's text, `[1, a]`, which the engine converts (with the tab delimiter only). In sqlite, records become synthetic ids, with the fields kept in `__FlowLogRecord`. A DB that Soufflé writes keeps only the ids, so a query that reads a record column needs a FlowLog index. CTADL's query reads none, so it also runs on a Soufflé index. | `record_type`, `tests/flowlog/adapter` |
| `.functor F(..)`, `@F(..)` | `.extern fn F(..)`, `F(..)`; the implementations are Rust UDFs | `functor_decl` |
| Variadic `cat(a, b, c)` | `cat(cat(a, b), c)` | `variadic_cat` |
| `to_string(s)` of a symbol (CTADL's JADX object ids) | `s`, as Soufflé computes it. FlowLog's `to_string` takes numbers only. | `tests/flowlog/semantics/cases/strings.dl` |
| `x = range(lo, hi)` | `FlowlogRange(hi, x), x >= lo`. `FlowlogRange` is a precomputed table of `0 <= x < hi < 1024`, built without recursion. Other bounds use a demand-driven recursive `RangeN` relation. | `range_gen` |
| `choice-domain` | Per-relation policy, `minchoice` by default ([below](#choice-domain)) | `choice_domain` |
| `inline` relations | Expanded at their positive uses, as Soufflé does. Materialised when used negated or in an aggregate, or when their clauses are given per component instance. Unused ones are dropped. | `inline_rel` |
| `true`, `false`, `!(a < b)`, `x = x` | Simplified; rules with unsatisfiable bodies are dropped | `true_false` |
| Expression arguments of atoms, `S(cat(a, "y"))` | `t = cat(a, "y"), S(t)`. In an aggregate, the binding goes inside the aggregate body. | `agg_computed` |
| Negation of a computed value, `!S(t)` with `t = cat(..)` | A `NegPreN` helper relation materialises the positive part, then `NegPreN(.., t), !S(t)` | `neg_computed`, `neg_computed_lifted` |
| Facts with computed arguments, `C(cat("a", "b")).`, and bodies with no positive atom or aggregate, `C(s) :- s = cat("a", "b").`, `A(1) :- !B(1).` | `C(cat("a", "b")) :- FlowlogUnit(_).`, `C(s) :- FlowlogUnit(_), s = cat("a", "b").`, `A(1) :- FlowlogUnit(_), !B(1).` (bug B15) | `computed_fact`, `computed_body_only`, `neg_only_body` |
| `_` inside an aggregate body | Named fresh variables, where Soufflé aggregates over the tuples of the body's single atom. FlowLog aggregates over the distinct bindings of the named variables ([FlowLog findings](#flowlog-findings)). | `count_all_wild` |
| Variables named like FlowLog keywords (`fn`) | Renamed | `keyword_fn` |
| `.plan` | FlowLog's `.plan` gives one join order per rule, Soufflé's one per semi-naive version of a recursive rule. The translation keeps version 0's order (the delta on the rule's first recursive atom) and drops the others ([Join order](#join-order)). Like Soufflé, it applies the plan to the rule with its relations inlined. It drops a plan where the translated rule reads other atoms (a `range` rewrite; a `choice-domain` relation read inside its own recursion, in a component instantiated more than once). | `tests/flowlog/semantics/cases/plans.dl` |
| `.pragma`, representation qualifiers | Dropped. `.limitsize`, which truncates a relation, is rejected. | `pragma` |
| Output relations that no fact, input or rule derives | Kept. FlowLog prunes them and writes nothing for them; the engine creates their tables and files empty, as Soufflé does ([Pipeline](#pipeline)). | `empty_dataflow`, `empty_output` |
| `number`, `unsigned` | `int64`, `uint64`: CTADL needs Soufflé's 64-bit domain. Constant expressions are computed with Soufflé's wrap-around, and expressions FlowLog would compute on `int32` are pinned to 64 bits with `as` ([below](#integers)). `float` is rejected (CTADL has none). | `cast_literal`, `tests/flowlog/semantics/cases/widths.dl` |
| `strlen`, `substr`, `^` | Calls to UDFs with Soufflé's semantics (`souffle_strlen`, …). FlowLog's `strlen` and `substr` count characters, not bytes (bug B7), and its `^` is exact where Soufflé truncates a `double`. | `souffle_builtins`, `tests/flowlog/semantics/cases/pow.dl` |
| `ord(s)` | `souffle_ord(ord(s))`. In both engines, `ord` is the symbol's id in the engine's symbol table. FlowLog's is an `int32`, which `as` cannot convert (it only types), so a UDF widens it. FlowLog loads the inputs of a program that calls its `ord` on one worker, in order, so the ids of the inputs' symbols do not depend on the workers' timing; a UDF that interned `s` itself gave CTADL's P-code ids that varied between runs. The ids of strings that rules compute (`ord(cat(..))`) depend on timing, as in Soufflé with several threads. | `tests/flowlog/semantics/cases/strings.dl` |
| `to_number`, `/`, `%` | Total UDFs with Soufflé's semantics, plus check rules for Soufflé's runtime errors ([below](#runtime-errors)). FlowLog's `to_number` parses decimal only. Its `/` and `%` panic on a zero divisor, also on rows Soufflé may never evaluate (as in bugs B8 and B9, now fixed). | `filter_order`, `antijoin_projection` |
| `.input`/`.output` | `IO`, `filename` and `delimiter` kept; other parameters dropped with a warning. Sqlite and file directives are recorded in the schema manifest, one entry per component instance (`inst.R`). | `tests/flowlog/adapter` |
| Several `.output`s of a top-level relation, `.output R`, `.output R(filename="R2.csv")` | FlowLog writes one per relation. The one not to a file (at most one), else the first, stays on `R`, and each other one writes a copy, `FlowlogOutputN(x) :- R(x).`, to the file Soufflé writes. Two outputs to one file are rejected. | `dup_output`, `tests/flowlog/semantics/cases/outputs.dl` |

The translator reports anything it does not handle as an error rather than mistranslating it. That
includes `eqrel`, `float`, ADTs, component inheritance, `autoinc`, `.limitsize`, numeric
conversions, `bnot`, `lnot` and `land`, `^` and `range` over `unsigned`, negation or disjunction
inside an aggregate, an aggregate correlated through a computed value, record columns in a file
with a delimiter other than tab, and two `.output`s of a relation that write one file, or that are
both not to a file. It also refuses arithmetic whose type depends on the order of Soufflé's alias
resolution ([Integers](#integers)), and component clauses whose names resolve to different
relations in different instances ([Components](#components)). `tests/flowlog/semantics/rejects/`
has 41 such programs. CTADL uses none of these.

Soufflé rejects an atom of an undefined relation or of the wrong arity, and so does the
translator, before any rewrite can drop the clause that holds it ([Components](#components)).
`tests/flowlog/semantics/invalid/` has 7 such programs.

**Timing.** The `range` and `inline` rewrites preserve the iteration in which Soufflé derives a
tuple, not just the fixpoint. In Soufflé a `range` generator or an inline relation is evaluated
inside the clause that uses it. A recursive helper relation yields one element per iteration, and
a materialised inline relation reaches its consumers one iteration late. Both reach the same
fixpoint, but inside recursion the delay changes which candidate reaches a `choice-domain` key
first. So `range` reads a table computed in a lower stratum, and inline relations are expanded
where possible. The one inline relation that stays materialised, `StartVertex` (its clauses are
given per component instance), feeds only `Vertex` and `ReachableVertex`, whose `choice-domain` is
functional (`drop` below).

<a id="choice-domain"></a>
### choice-domain

CTADL declares `choice-domain` on 7 relations (8 declarations: `ReachableVertex` is declared twice).
Soufflé keeps the first tuple inserted for each key. Which tuple comes first depends on Soufflé's
evaluation order, and its documentation calls the choice non-deterministic. FlowLog has no
equivalent, so the translator encodes each `choice-domain` by a policy: `minchoice` by default, or
the one that the policy file names for the relation (`flowlog-policy.json` for CTADL):

| Policy | Relations | Encoding |
|---|---|---|
| `drop` | AliasedBy, IntraObjectAliasedBy, ReachableVertex | The `choice-domain` is removed. The dependent columns are functions of the key (always `""`, or `ord` of the key and `0`), so there is one tuple per key anyway. |
| `minchoice` (default) | VirtualAssign, ParthianVertex, ContextObj, LostAssign | `R_choice(keys.., ck: min<int64>)` with `ck = choice_keyN(prio, deps..)`. `R` is decoded with `choice_field(ck, i)`. |

The translator also supports `{"min": "COLUMN"}`, which turns one dependent column into a FlowLog
`min` lattice. It is unused. For ParthianVertex it would keep the shortest length per (var, path),
but Soufflé keeps the first length derived, which need not be the shortest.

**Choice key.** A `minchoice` key has three fields, from most to least significant:

1. `prio` (9 bits): the clause's rank in Soufflé's evaluation order for that relation: 0–255 for its
   non-recursive clauses, 256–511 for its recursive ones.
2. `len` (11 bits): the total length of the dependent fields, a proxy for derivation depth.
3. `hash` (43 bits): an FNV-1a hash of the dependent fields, interned with linear probing.

The key is a function of the candidate alone, so the choice does not depend on the worker count.
Strings cannot be lattice values: `min` aggregation over strings is unsupported, and a `min<string>`
lattice column hits bug B3. Interning the fields into an `int64` key gets around both.

**Clause order.** Soufflé evaluates a stratum's non-recursive clauses first and then iterates its
recursive ones. Within each group it follows the relation's clause order after its AST
transformers, which is not source order. Several transformers replace a clause with a rewritten
copy at the end of the relation's clause list, among them the ones that remove empty relations,
partition body literals, fold constant constraints, inline relations and resolve aliases. Clauses
from component instances come after top-level ones. The translator replays these moves
(`souffle_order_key`). The resulting order matches the RAM program printed by
`souffle --show=transformed-ram` for all four `minchoice` relations: VirtualAssign (2
non-recursive and 136 recursive clauses), ParthianVertex (1 and 9), ContextObj (0 and 7) and
LostAssign (8 and 0). VirtualAssign has one more non-recursive clause, which reads the always-empty
`CInsn_ModelAssign`. Soufflé deletes it, and the translator ranks it last.

**Inside recursion.** VirtualAssign, ContextObj and ParthianVertex are read inside their own
recursive stratum.
There, FlowLog's lattice merge emits each key's first minimum and then every improvement, but never
retracts the superseded row (repro `lattice_in_loop`). In-loop reads could therefore see several
witnesses per key, and each witness could derive further facts. Soufflé never revises a choice.

The translator therefore routes in-loop reads through `choice_claimN(inst, rel, ck, keys..)`. This
UDF remembers the first candidate presented for each (component instance, relation, key) and
returns it on every later call. All reads in the loop agree on one witness, and the final relation
is decoded through the same claims.

On CTADL's tests and corpora the results match Soufflé, apart from one tie. `tests/flowlog/choice_mini` and
the benchmark show that they do not depend on the worker count. On the 30 generated programs of
the [sweep](#larger-programs) and on the benchmark's syn25, every result is a valid choice model.
The scheme does rely on an evaluation-order property: for each key, the first candidate a claim
sees must be the minimum of the earliest round that derives that key. `check_choice_model.py` does
not check this property. Soufflé's first insertion also comes from the earliest round, then the
earliest clause, so the priority bits make both engines pick from the same clause (ties within a
clause: [Limitations](#limitations)). FlowLog's semantics do not promise this order. A native
`choice-domain` in FlowLog would remove this risk, together with most of the UDF machinery.

<a id="runtime-errors"></a>
### Runtime errors

Soufflé stops a program with an error in three cases:

- `to_number` of a string that holds no number, or one out of range;
- integer `/` or `%` by zero;
- the smallest `number` divided by -1.

FlowLog's operators panic on a zero divisor too, and FlowLog may evaluate an expression on
bindings that Soufflé never reaches:

- its planner orders a rule's joins, filters and computations its own way. In
  `O(x) :- T(x), S(x, y), x / y > 1.`, it tests `x / y > 1` on every row of `S`, where Soufflé
  tests it only on the rows that join `T`. Two other orders were bugs, B8 and B9, now fixed:
  FlowLog tested a rule's comparisons before its atoms' constants, and computed a negated rule's
  head on the rows the negation removes;
- it evaluates clauses and columns that Soufflé's optimiser deletes (below).

A panic there would stop runs that Soufflé completes. The port's versions (`souffle_to_number`,
`souffle_div`, …) therefore return 0 where Soufflé stops, and check rules reproduce the stops.
Take a partial call `c` in a clause's head, and let `B` be the clause's body without the
literals that contain `c`:

```
FlowlogError(m) :- B, m = souffle_div_error(a, b), m != "".
FlowlogAbort(n) :- FlowlogError(m), n = souffle_raise(m).
```

`souffle_div_error` returns `""`, or Soufflé's complaint. `souffle_raise` prints the complaint
and exits with status 1.

A check is sound only if Soufflé evaluates `c` on every binding of `B`. Soufflé's optimiser
rewrites the program first:

- it deletes the relations that no output reads;
- it drops the columns of a relation read only as `R(_)`, with the expressions that compute them;
- it replaces a copy, `R(x) :- S(x).`, by the relation it copies;
- it deletes the clauses that are redundant or read an empty relation;
- it substitutes a clause's assignments into the clause (ResolveAliases).

The translator replays these steps (`sx_simulate`, `sx_analysis`). It generates a check only
for a head call that surely survives them.

The guarantee, for Soufflé's default options and assuming FlowLog evaluates the translated
program correctly:

- When a check fires, Soufflé stops too.
- When both engines complete, their results are equal.
- When Soufflé stops on a call, so does the port, unless the translator's warning lists the
  call as "not fully checked". These calls are:
  - calls only in a body;
  - calls in a recursive, `choice-domain` or component clause;
  - calls in a clause with an aggregate, record, `range` or inline relation;
  - calls in a clause that Soufflé may delete, or whose columns it may drop;
  - calls whose `B` reads `ord`, or a relation that depends on `ord` or on a `choice-domain`;
    the engines may give these different values;
  - head calls that also appear in the body. Soufflé tests a variable repeated in an atom after
    the rule's constraints, so it may evaluate such a call on rows that `B` rejects;
  - calls of literals only that fail on one of `number` and `unsigned` but not on the other, such
    as `(-9223372036854775807 - 1) / -1`. Soufflé computes them in the type of their context.

A `.pragma` that changes Soufflé's pipeline (`magic-transform`, `disable-transformers`,
`provenance`) turns all checks off, with a warning.

CTADL has three partial calls, all `to_number` of option values in `config.dl`. The translator
cannot show that Soufflé evaluates them, so it lists all three. They cannot fail: the values are
`to_string(v)` of the numbers in `CTADLConfig_Input`.

<a id="integers"></a>
### Integers

Soufflé computes `number` and `unsigned` on 64 bits, with wrap-around. FlowLog gives an integer
expression the type of the column, parameter or typed operand it meets, and `int32` where it meets
none. `1 bshl 31 > 0` holds in Soufflé, but FlowLog shifts an `int32`, gets −2147483648, and the
comparison fails. The translator therefore rewrites integer expressions in three ways:

- **Constants** (`fold_constants`). The translator computes constant expressions as Soufflé does:
  on 64 bits with wrap-around, in the type of the column or parameter they fill, else of a
  declared variable in them, else `number`. FlowLog would compute `2147483647 + 1` on `int32`.
  - A variable equated to a constant takes its value.
  - Comparisons of constants become true or false.
  - An aggregate whose body this makes unsatisfiable is replaced by its value: 0 for `count` and
    `sum`. A `min` or `max` has no value, so its literal becomes false.
- **64-bit anchors** (`anchor_widths`). FlowLog falls back to `int32` in three places:
  - a comparison of only literals, counts and variables bound to them;
  - the argument of `to_string`;
  - an arithmetic argument of a UDF.

  There the translator pins a term to 64 bits with FlowLog's `as`: `as(n, int64)`, or `uint64`.
  This needs B11's fixes, #487 and #492: before them, `as` left a literal `int32`, and FlowLog
  dropped an `as` around an expression, a call or an aggregate.
- **Literals typed by their use** (`alias_constants`, `type_context_partials`). Soufflé replaces a
  variable equated to a constant by the constant before it types the clause, so each copy takes
  the type of its own use.
  - Example: in `A(x) :- x = 3, x - 10 < 0.` with `A` unsigned, `3 - 10 < 0` compares numbers and
    holds. FlowLog types `x` once, as `unsigned`, on which the comparison is false.
  - In rules that may compute on `unsigned`, the translator substitutes such variables as Soufflé
    does. Where the result depends on the order in which Soufflé resolves the equalities, it
    refuses the rule.
  - A `/` or `%` of literals in an `unsigned` context divides unsigned.

`tests/flowlog/semantics/cases/widths.dl` checks these rewrites against Soufflé. CTADL needs none of them:
its translation is the same with or without them.

<a id="components"></a>
### Components and erroneous programs

FlowLog instantiates components itself, so the translator gives a component's clauses only once. A
relation name in them must denote the same relation in every instance. Soufflé, like FlowLog,
resolves a name to the innermost enclosing instance whose component declares it or reaches it
through an `.init`, else to the program. The translator resolves names the same way
(`resolve_decl`). It refuses three cases:

- a name that denotes different relations in different instances (`comp_scope_mixed`);
- a name that denotes an enclosing instance's relation with a `choice-domain` or a type parameter;
- a component with type parameters that declares nested components.

Soufflé rejects an atom or directive of an undefined relation, or an atom of the wrong arity, even
in a clause it never evaluates. The translator drops some clauses, such as never-satisfied rules
and the clauses of unused inline relations, so FlowLog would not see the error. The translator
therefore checks every atom and directive before any rewrite (`check_relations`), so these
programs fail in both engines. Like Soufflé, it skips components that are never instantiated.
`tests/flowlog/semantics/invalid/` has 7 such programs, each with the error it expects.

An `.input` or `.output` inside a component reads or writes, in each instance, the relation its
name resolves to there. Both engines name the relation and its default file after the instance
(`c.A`, `c.A.facts`), and the schema manifest lists each instance's.

### UDFs

| UDF | Purpose |
|---|---|
| `NewCallString`, `CallStringLte`, `CallStringUnder`, `CallStringNonEmpty`, `CallStringSize`, `TopFrame`, `PushFrame`, `PushFrameK`, `PopFrame` | Call strings (contexts) |
| `AccessPathSize`, `AccessPathCycle` | Access-path length and cycle detection |
| `CheckSubstring` | Substring test |
| `AndroidManifestClassId` | Android manifest class ids (unused by taint-front) |
| `choice_key1..4`, `choice_field`, `choice_number` | `minchoice` keys: intern the dependent columns, decode them (`choice_number` for a `number` column) |
| `choice_claim1..6` | In-loop witness claims |
| `souffle_strlen`, `souffle_substr`, `souffle_pow`, `souffle_ord` | Soufflé's `strlen` and `substr` (on bytes), `^` (truncated `std::pow`); FlowLog's `ord` as a `number` |
| `souffle_to_number`, `souffle_div`, `souffle_mod`, `souffle_udiv`, `souffle_umod` | Soufflé's partial functions, made total: 0 where Soufflé stops |
| `souffle_*_error`, `souffle_raise` | The [runtime-error](#runtime-errors) checks: Soufflé's complaint, or `""`; stop the run |

The first four rows are CTADL's functors, in `src/ctadl/souffle-logic/functors.rs`, which ports
`functors.cpp` next to it. The others are the translator's, in `src/ctadl/flowlog/souffle2flowlog.rs`.
FlowLog passes `string` arguments as owned `String`, and `number` (FlowLog's `int64`, as CTADL
builds Soufflé with 64-bit numbers) as `i64`. The choice helpers' priorities, instance and relation
ids, and `souffle_ord`'s argument, are `int32`.

## Validation

`run_taintfront.py` runs `ctadl import`, `index` and `query` (SARIF) for each program with both
backends. It then:

- runs CTADL's checks where the program has expectations;
- diffs the two databases relation by relation (`compare_db.py`);
- diffs the two SARIF reports (`compare_sarif.py`).

The differential results:

| Suite | Programs | Compared runs | Result |
|---|---|---|---|
| CTADL taint-front tests (`ctadl/tests/taint-front`) | 12 | 12 | 12 identical; CTADL's checks pass on both backends |
| `ctadl/taint-front` corpus | 21 | 20 | 20 identical. taint-front's own parser rejects `compositional-ctx3` before either backend runs. |
| ctadl-rs corpus (`ctadl-ascent/tests/tnt`) | 59 | 51 | 51 identical. taint-front's parser rejects 8 programs. |
| 10 option variants × (12 tests + 20 corpus) | | 320 | 319 identical, 1 valid choice-domain witness difference |
| `--all-outputs` × (12 tests + 20 corpus) | | 32 | 32 identical |
| **Total** | | **435** | **all identical, apart from 1 valid choice witness** |

Both backends ran with 32 workers (`-j 32`). That is CTADL's default on the test machine, which
has 64 hardware threads; CTADL uses half the CPUs. The runs used `ctadl --engine flowlog` on FlowLog
`main` at `b99ed29`, with the final translator and engine, and FlowLog's analyses compiled from
scratch. Earlier rounds gave the same results: on `main` at `84b3a1c` plus #477, with 4 and 32
workers, and on `b99ed29` before the `ord` fix ([Setup](#setup)). Each run's line is in
`results/taint-front/summary.txt`, `results/corpus-ctadl/summary.txt`,
`results/corpus-ctadl-rs/summary.txt` and `results/variants/*/summary.txt`; their times include
the compilation of FlowLog's analyses in the first runs. Four more checks of the engine option,
made by `run_engine_checks.sh`, gave Soufflé's results (`results/engine.txt`):

- four taint-front tests whose query ran on the other engine's index, both ways round;
- CTADL's `tests/bin/check-taintfront2`, in a copy for each engine. It indexes and queries with
  Soufflé's interpreter (`--no-compile-analysis`), runs three commands that must fail (an index
  that exists, a missing facts directory, a missing index), then indexes without interprocedural
  data flow, exports the GML and queries for a SARIF report without code flows. Every step exits
  as with Soufflé, except the two interpreter runs, which FlowLog refuses; the databases, SARIF
  reports and GML exports are equal;
- a `--dl` query fragment with file inputs and outputs (records, nullary relations, a component
  instance, a subdirectory, several outputs of a relation, among them one the query writes to its
  database), whose files are the same with either engine;
- two FlowLog runs of that fragment at the same time, which compiled its analysis once.

The option variants are: `--object-tracking`, `--hybrid-inlining-context-bound=2`,
`--no-hybrid-inlining`, `--dynamic-access-paths-max-length=3`, `--no-interprocedural-data-flow`,
`--no-hybrid-inlining-inlining-bound`, `--hybrid-inlining-add-parthians-to-vertex`,
`--star --match-star-fields`, `--cha`, and the query option `--compute-slices=all --format=sarif+all`.
They enable rules that the default configuration never fires. Some of CTADL's expectations were
written for the default options and fail under a variant (kctx2 1, nohi 5, nointer 9, objtrack 2).
They fail identically on both backends, and those runs' databases and SARIF are still identical.
Per-variant tallies are in `results/variants/tally.txt`.

**What "identical" means:**

- **Relations** compare as sets of rows. A few columns hold engine-specific ids and are mapped
  back first: slice vertex ids computed with `ord`, and record ids.
- **choice-domain relations** count as equal when both sides keep exactly one witness for the same
  set of keys. This happened once.
- **SARIF:** `sarif+all` graphs carry engine-specific vertex ids, so they are compared after
  canonical renaming. Rule C0006 is filled by CTADL's own SQL, which uses `GROUP BY` with bare
  columns. SQLite then picks an arbitrary row, so only the candidate set is compared.

<a id="front-ends"></a>
**Front ends.** The suites above use taint-front, CTADL's language for hand-written examples. Its
other tests run the analyses on facts from Ghidra (P-code) and JADX: `check-pcode` and
`check-analysis-options` on curl and nginx, which they build with nix, and
`check-ActivityCommunication4` on a DroidBench app. `run_front_ends.sh` runs their steps with both
engines, on Ubuntu's curl, an nginx built with the flags of CTADL's `nginx-debug`, and the app
([Versions](#versions)), and compares the results (`results/front-ends.txt`). Each engine runs in a
copy of the import, with 32 workers and its analyses compiled beforehand. Times are the wall times
of `ctadl`'s commands, and memory the largest peak RSS among their processes:

| Program | Step | Soufflé | FlowLog | Compared |
|---|---|---|---|---|
| curl (P-code) | index | 38.7 s, 0.57 GB | 36.0 s, 4.89 GB | |
| | query, SARIF | 17.4 s, 0.38 GB | 14.9 s, 2.17 GB | database (210 relations) and SARIF equal; no results |
| | index `--match-star` | 39.8 s, 0.57 GB | 36.6 s, 4.92 GB | database (168) equal |
| | index `--cha --no-hybrid-inlining` | 38.7 s, 0.57 GB | 40.1 s, 4.83 GB | database (168) equal |
| | index `--star` | 65.5 s, 1.76 GB | 45.7 s, 5.47 GB | 167 relations equal; CTADLStats: 1 count differs |
| nginx (P-code) | index | 452.9 s, 9.40 GB | 182.9 s, 19.17 GB | |
| | query, forward slice | 66.8 s, 1.38 GB | 26.9 s, 3.50 GB | 209 relations equal; CTADLStats: 3 counts differ. Slice summaries equal (17 instructions; the check wants more than 10) |
| ActivityCommunication4 (JADX) | index | 1.7 s, 0.04 GB | 2.8 s, 2.29 GB | |
| | query, SARIF | 2.6 s, 0.06 GB | 2.9 s, 1.43 GB | database (114) and SARIF equal; the check's C0001 result and intent assignments |
| | export, GML | 0.2 s | 0.2 s | GML equal (90 nodes, 9 edges) |

- **CTADLStats** counts the rows of some intermediate relations. Those that differ depend on the
  context that `ContextObj`'s `choice-domain` keeps for a key, which the engines pick differently:
  the number of contexts (134 and 135 on nginx) and of the virtual assignments made in them. Each
  engine picks the same contexts on every run: Soufflé's `--star` index of curl, run three times,
  gave the same database each time, and FlowLog's gave the same counts as in an earlier run.
- **Time and memory:** on nginx, FlowLog's index and query phases are each 2.5× as fast as
  Soufflé's, with 2.0× and 2.5× its memory. FlowLog needs about 70 MB per worker whatever the
  program: ActivityCommunication4's index takes 0.15 GB with 1 worker, 0.33 GB with 4 and 2.3 GB
  with 32, against Soufflé's 0.05 GB.
- **DroidBench:** `run_droidbench.sh` runs `check-ActivityCommunication4`'s steps on DroidBench's
  188 apps, with 4 workers per engine (`results/droidbench.txt`). 111 apps give identical
  databases, SARIF reports and GML exports; 11 of them report a leak, with either engine. 4 more
  differ only in the GML, where 1 or 2 nodes get another type: the exporter takes a node's type
  from the last of its database rows, and the engines order the rows differently. The other 73
  fail to index with either engine, on facts that CTADL's JADX plugin writes
  ([Limitations](#limitations)).
- **SARIF validation:** `check-pcode` validates curl's report with `checksarif`, Microsoft's SARIF
  multitool, and fails on its schema violations. `check_sarif_schema.py` validates reports against
  the same schema (SARIF 2.1.0, at the revision that CTADL pins) with Python's `jsonschema`, and
  compares the violations of each FlowLog report with those of Soufflé's report of the same run.
  curl's report is valid with either engine. Of the 1,123 reports of all the runs above, the 373
  with code flows are invalid, and each of the 564 FlowLog reports has the same number of results
  and the same violations in each result as Soufflé's (`results/sarif-schema.txt`). CTADL writes a
  code-flow location's annotations as a message without a position (`startLine`, `charOffset` or
  `byteOffset`), which a SARIF region requires. CTADL's tests validate only curl's report, which
  has no results.

Other checks:

- `tests/flowlog/semantics/run.sh` tests the translation rule by rule against Soufflé, on small programs
  written for the purpose (last run in `results/semantics.txt`):
  - `cases/` (20 programs, 1,201 rows): every output relation is equal with FlowLog at 1, 4 and
    32 workers. They cover rule forms, components and their name scoping, arithmetic and integer
    widths, strings, aggregates and their scoping, records, ranges, `choice-domain`, programs
    without a derived output, several outputs of a relation, `.plan` hints, and Soufflé's
    builtins (`pow`, `substr`, `to_number`, …), including their edge cases. A case can also name
    text its translation must contain (`// expect-fl:`), such as the `.plan`s it keeps.
  - `rejects/` (41): the translator reports an error.
  - `invalid/` (7): erroneous programs. Soufflé and the translator reject each with the same
    error (undefined relation or wrong arity, at the same line).
  - `aborts/` (11): Soufflé stops on a partial call, and the port stops with the same complaint.
  - `warns/` (3): Soufflé stops on a call the checks cannot cover, and the translator's warning
    lists it.
- `tests/flowlog/choice_mini/run.sh` translates, compiles and runs a recursive choice-domain program with
  1, 4 and 32 workers and compares the output with expected results and with Soufflé's
  (`results/choice_mini.txt`).
- `tests/flowlog/adapter/run.sh` checks the engine's sqlite and file I/O against Soufflé
  (`results/adapter.txt`):
  - sqlite: Soufflé writes a DB in which record columns are only ids. An input no rule reads is
    created empty, and one a rule reads is refused with an explanation. Records the port writes
    still round-trip, a nil record id is refused, without FlowLog's output DB every output is
    created empty, and an input missing from the DB is left out.
  - files, on a program with component instances: each instance's inputs and outputs keep
    Soufflé's file names (`i.A.facts`, `o.in.A.csv`). Record columns are read and written as
    Soufflé reads and writes them, compared with Soufflé's own files (spaces, quotes and escapes,
    nested records, carriage returns). A nullary fact is written as Soufflé writes it, `()`. A
    malformed record is refused. An input no rule reads is created empty, a missing one is left
    out, and the outputs FlowLog prunes are created empty.
- The benchmark runs each program with several backends and worker counts and diffs all runs
  against the first.

<a id="larger-programs"></a>
**Larger programs.** CTADL's tests are small. In them, a `choice-domain` key almost never gets two
candidates from the same clause in the same round. `run_sweep.sh` generates 30 larger programs
with `gen_tnt.py`, on which such ties are common: 6 to 15 functions of 12 statements, with field
accesses, calls through function-valued fields and recursion. For each program, it:

- runs the index phase with Soufflé `-j 1` (deterministic, the reference), and with Soufflé and
  FlowLog on 32 workers;
- compares the databases on full rows;
- runs `check_choice_model.py` on FlowLog's `--all-outputs` database.

Results are in `results/sweep/results.txt`:

| Compared with Soufflé `-j 1` | FlowLog | Soufflé `-j 32` |
|---|---|---|
| Derived relations equal, some `VirtualAssign` witnesses differ | 19 | 23 |
| Only the context count in `CTADLStats` differs | 2 | 1 |
| Downstream relations differ (`CallEdge`, `SummaryFlow`, `VirtualAlloc`, `VirtualAssign`, …) | 9 | 6 |
| Valid choice model (`check_choice_model.py`) | 30 | |

Where FlowLog's `VirtualAssign` has Soufflé's keys (21 programs), 27 to 3,911 of them have another
witness. Between Soufflé's own `-j 32` and `-j 1` runs, 15 to 3,293 keys do in 24 programs. In the
other 6 (f8_s4, f12_s4, f12_s6, f15_s1, f15_s4, f15_s6), downstream relations differ as well, as
they do for FlowLog: with several threads, Soufflé's first insertion depends on scheduling, and an
earlier sweep had 3 such programs instead. FlowLog's results do not depend on the worker count or
the run: its comparisons are identical to those of two earlier sweeps, with 2 and 32 workers.
In the 2-worker sweep, Soufflé `-j 8` differed from `-j 1` only in `VirtualAssign` witnesses.

Where downstream relations differ, `ContextObj` also has different witnesses. Its dependent column
is a call-string context, and a different context for an object changes which indirect calls are
resolved, and everything derived from them.

All 30 FlowLog databases are choice models of the program. Could Soufflé have produced them? Two
experiments show that both engines' results depend on how ties are broken
(`results/sweep/souffle-threads.txt`, `results/sweep/tiebreak.txt`):

- **Soufflé's derived relations also vary between runs.** On the 9 programs, 9 to 12 Soufflé runs
  each, at 1 to 32 threads:
  - on f8_s4, five runs at `-j 8` and `-j 16` produced FlowLog's exact database;
  - on f12_s4, one run did;
  - on 3 more programs, some runs produced a third database.
- **The port's tie-break decides as much.** Reversing it (longer fields first, reverse hash
  order: two lines in `choice_key`) changes the derived relations of 16 of the 30 programs. Then
  16 programs match Soufflé (19 with the default tie-break). Among them are f12_s5 and f6_s1, which
  differ with the default.

On 3 programs (f10_s4, f12_s1, f15_s4), no run of either engine reproduced the other's result.
There the choice-model check is the only evidence.

To reproduce (`--prune` deletes the databases of identical runs):

```bash
P=ctadl/flowlog-backend; S="--schema $P/index_schema.json --schema $P/query_schema.json --prune -j 32"
python3 $P/run_taintfront.py $S --out runs/taint-front
python3 $P/run_taintfront.py $S --testdir ctadl/taint-front --out runs/corpus-ctadl
python3 $P/run_taintfront.py $S --testdir ctadl-rs/ctadl-ascent/tests/tnt --out runs/corpus-ctadl-rs
JOBS=32 bash $P/run_variants.sh    # or: run_variants.sh star allout ...
JOBS=32 bash $P/run_sweep.sh -P 2  # FUNCS="6 8" SEEDS="1 2" for a subset
JOBS=32 bash $P/run_engine_checks.sh
WORKERS="1 4 32" ctadl/tests/flowlog/choice_mini/run.sh
WORKERS="1 4 32" ctadl/tests/flowlog/semantics/run.sh   # or: run.sh arith div_zero ...
ctadl/tests/flowlog/adapter/run.sh
$P/repros/check.sh -s; $P/repros/check.sh; $P/repros/check.sh -t
```

## Performance

`bench.py` timed five backends with 1 and 32 workers on two programs generated by
`gen_tnt.py --funcs N --stmts 12 --seed 1`:

- Soufflé on CTADL's program, as CTADL runs it;
- FlowLog on its translation, which keeps the first version's order of CTADL's `.plan` hints;
- Soufflé and FlowLog on the same program without the hints, Soufflé in two variants
  ([Join order](#join-order)).

The query wrote CTADL's text summary. The analyses were compiled beforehand, and the runs were one
at a time on the otherwise idle machine. Full results are in `results/bench/summary.txt`. Times
are in seconds:

- **wall:** the whole `ctadl` command;
- **engine:** Soufflé's compiled analysis, or FlowLog's binary;
- **conv.:** FlowLog only, the analysis's time outside FlowLog's binary: the port's conversion
  between CTADL's sqlite layout and FlowLog's, for the query's inputs and results, plus up to 0.1 s
  of other work (the index's takes 0.1–1.2 s);
- **RSS:** the engine's peak memory.

The runs in this section predate the engine option: the port's standalone driver ran the same
FlowLog programs and conversions. With the engine, on FlowLog `main` at `b03d2fd`, a run on syn25
with 32 workers took about as long as the runs below (`results/bench/engine.txt`): FlowLog's index
engine 13.3 s instead of 13.0 s, its query's computation 10.3 s instead of 10.8 s, and the
conversion 19.1 s instead of 19.2 s. Soufflé's index and query engines took 12.8 s and 36.9 s
instead of 12.8 s and 36.7 s.

Soufflé as CTADL runs it, and FlowLog:

| Program | Backend | Workers | Index wall | Index engine | Index RSS | Query wall | Query engine | Query conv. | Query RSS |
|---|---|---|---|---|---|---|---|---|---|
| syn10 | Soufflé | 1 | 2.8 | 1.1 | 34 MB | 3.6 | 1.0 | | 40 MB |
| syn10 | Soufflé | 32 | 2.3 | 0.6 | 32 MB | 3.4 | 0.8 | | 38 MB |
| syn10 | FlowLog | 1 | 3.5 | 1.5 | 174 MB | 4.1 | 1.1 | 0.3 | 147 MB |
| syn10 | FlowLog | 32 | 3.6 | 1.6 | 2.0 GB | 5.7 | 2.6 | 0.3 | 1.4 GB |
| syn25 | Soufflé | 1 | 46.5 | 44.3 | 350 MB | 89.1 | 54.1 | | 1.4 GB |
| syn25 | Soufflé | 32 | 15.1 | 12.8 | 357 MB | 72.4 | 36.7 | | 1.3 GB |
| syn25 | FlowLog | 1 | 25.6 | 22.2 | 795 MB | 104.4 | 48.0 | 18.8 | 2.2 GB |
| syn25 | FlowLog | 32 | 16.7 | 13.0 | 2.6 GB | 95.6 | 28.1\* | 19.2 | 3.5 GB |

\* Writing the query's results to sqlite took 17.3 s in this run, against 9.3 s in the
`flowlog-noplan` run with 32 workers, whose query engine took 20.1 s. Both run the same query
program on the same index ([Join order](#join-order)), and computing the results took 10.8 s in
both. The figures below for FlowLog's query on syn25 are from the `flowlog-noplan` runs.

- **Engine time:** on syn25, FlowLog's index takes half of Soufflé's time with 1 worker and 2% more
  with 32. Its query is 1.1× as fast with 1 worker and 1.8× with 32. Both times include writing the
  results to sqlite. With 32 workers, contention in FlowLog's string interner limits both; its
  fix, [#499](https://github.com/flowlog-rs/flowlog/pull/499), now in `main` (`b99ed29`), brings
  the index down to 3.3 s and the query to 16.9 s ([With the interner
  fix](#with-the-interner-fix)). On syn10, a
  small input, FlowLog's index takes 0.3–1.0 s longer, and its query takes 2.6 s with 32 workers
  against 1.1 s with 1: there the workers cost more than they gain.
- **End to end**, FlowLog's query phase takes 1.2× Soufflé's wall time with 1 worker and with 32.
  syn25's query phase, step by step (`bench.py` logs each step's time):

  | Step (seconds) | Soufflé, 1 | FlowLog, 1 | Soufflé, 32 | FlowLog, 32 |
  |---|---|---|---|---|
  | Convert the inputs to FlowLog's layout | | 0.4 | | 0.5 |
  | Engine: load the inputs and compute | 54.1 | 37.8 | 36.7 | 10.8 |
  | Engine: write the results to sqlite | (above) | 11.2 | (above) | 9.3 |
  | Convert the results to Soufflé's layout | | 18.8 | | 19.0 |
  | CTADL indexes the results (`after_query`) | 20.1 | 24.3 | 21.0 | 25.3 |
  | CTADL writes the report | 13.2 | 11.9 | 13.1 | 18.3 |
  | Rest (startup, CTADL's setup) | 1.7 | 1.9 | 1.6 | 1.9 |
  | **Wall** | 89.1 | 106.2 | 72.4 | 84.9 |

- **Conversion:** FlowLog writes plain sqlite tables. CTADL's tools read Soufflé's layout, a
  symbol table plus views, and `sqlite_layout.py` converts between the two inside SQLite
  ([Pipeline](#pipeline)). syn25's query writes 18.4 million rows, 18.0 million of them in
  `forward_flow.ReachableEdge`, and converting them takes 18–19 s on one thread. (The port's
  first version converted them row by row in Python, in 56 s.) With 1 worker, the conversion
  outweighs the engine's gain: FlowLog's engine takes 5 s less than Soufflé's, and the
  conversion 19.3 s. With 32, FlowLog's engine takes 16.6 s less, but the conversion and CTADL's
  slower steps on its database (next item) take 29 s more. FlowLog writing Soufflé's layout
  itself would save most of the conversion ([Suggestions](#suggestions)).
- **Row order:** with 32 workers, FlowLog writes a relation's rows in another order than with 1.
  In `forward_flow.ReachableEdge`, the rows that share a value of the first column, one of the
  three columns that CTADL indexes, form 8.5 million runs for 407,832 values. Soufflé's databases
  and FlowLog's with 1 worker have one run per value. FlowLog's databases with 1 and 32 workers
  have the same contents, but CTADL's indexing and report take 7.3 s longer on the second.
- **Memory:** FlowLog's index uses 2.3× Soufflé's peak memory with 1 worker and 7.5× with 32. Its
  query uses 1.6× and 2.7×. FlowLog's memory grows with the worker count; Soufflé's does not.
- **Results:** FlowLog's databases with 1 and 32 workers are identical, on both programs. CTADL's
  query report, which counts the sources, sinks and slices found, is identical in all runs: every
  backend, hint variant and worker count. The databases differ from Soufflé `-j 1`'s, as do
  Soufflé's own with 32 workers or without the hints ([Join order](#join-order)). The comparisons
  are in `results/bench/`:
  - **Soufflé `-j 32`:** 4 relations differ on each program. With several workers,
    Soufflé's result varies between runs ([Larger programs](#larger-programs)).
  - **FlowLog on syn10:** some `choice-domain` witnesses and the query's three `ReachableEdge`
    relations differ.
  - **FlowLog on syn25:** 12 relations differ, as in 9 of the [sweep](#larger-programs)'s
    programs. 8 of 340 call edges differ, as do 7–8% of `SummaryFlow` rows and the query
    relations built on them. FlowLog's index is still a choice model of the program:
    `check_choice_model.py` makes Soufflé take FlowLog's 266,469 witnesses, and Soufflé then
    derives FlowLog's database exactly. The check needs an `--all-outputs` index, which also
    writes the internal choice relations; every other relation equals the benchmarked index's,
    apart from the `ALL_OUTPUTS` setting.
  - **FlowLog's query on Soufflé's index** (syn25): the database equals Soufflé's in 119 of 120
    relations. These include the query's `ReachableVertex` relations, which are `choice-domain`
    relations too, and the three `ReachableEdge` relations of 18 million rows each. The 120th,
    `LostAssign`, is a `choice-domain` relation that nothing reads. It keeps another witness for
    159 of its 163 keys; each is a tie between candidates from the same clause. The other 11
    relations that differ in FlowLog's own run therefore differ because its index keeps other
    witnesses.
- **Size:** the databases grow quickly with the program: syn10's are 32 MB, syn25's 1.07–1.10 GB,
  with three `ReachableEdge` relations of 18 million rows each. No larger program was benchmarked.

### Join order

CTADL gives Soufflé hand-written join orders: 67 `.plan` hints in the index and 6 in the query.
Soufflé evaluates a recursive rule semi-naively, in one version per body atom of the rule's own
recursion: version *i* joins the new tuples (the delta) of the *i*-th such atom with the full
relations of the others. A hint orders each version separately, `0:(…)` the first; Soufflé
rejects `.plan` on other rules. FlowLog's `.plan` takes one order per rule: differential dataflow
joins the new tuples of every input through the same join tree. The translation keeps each
hint's version-0 order and drops the others:

- **4 hints order version 0**, all in the index: `SummaryFlow` (`graph/dataflow.dl`),
  `SummaryAlloc` (`graph/callgraph.dl`), and two rules of `graph/star.dl` that run only with
  `--star`. The translation writes each star rule as two rules, one per clause of its
  disjunction, so the index gets 6 FlowLog `.plan`s.
- **The other 69 order later versions only.** Soufflé runs version 0 of these rules, like every
  rule without a hint, in about the written order, as FlowLog does (below).

So both engines join the first version of every rule in about the same order, and only Soufflé
orders the later versions of 73 rules. Without the hints, both engines join in the written order:

- **FlowLog** has no SIPS. Its planner joins a rule's positive atoms left-deep in the written order.
  It deviates only to avoid a cross product (cyclic bodies) or to join join-tree neighbours
  (acyclic bodies). Before joining, it folds an atom whose variables all occur in another atom
  into a semijoin. Its planner trace for the index without the hints has 362 rules with two or
  more positive atoms:
  - 332 are joined in the written order, 160 of them after folding atoms into semijoins;
  - the other 30 differ only where an atom is linked through a computed binding
    (`lift_t = strlen(ap) - 1`, `full_ap = actual_ap`), which is joined after the atoms it depends
    on, or where the next atom would be a cross product.
- **Soufflé** orders each rule version with a SIPS, by default `all-bound`. That is the written
  order, except that an atom whose arguments are all bound moves forward. Such an atom becomes an
  existence check, which Soufflé hoists to the outermost loop that binds its variables in any
  case. So `all-bound` and `strict`, the written order, give the same loop nests (checked with
  `souffle --show=transformed-ram`). The exceptions are two non-recursive rules, which `all-bound`
  starts from the constant atom `CisAccessPath("")`.

`bench.py` therefore has three more backends, which run CTADL's program without its hints:

- `souffle-noplan` uses Soufflé's default SIPS;
- `souffle-strict` adds `.pragma "RamSIPS" "strict"`;
- `flowlog-noplan` translates the program without them.

Engine times in seconds, with 1 / 32 workers:

| Program | Phase | Soufflé, CTADL's hints | FlowLog, their version 0 | Soufflé, no hints | Soufflé, strict | FlowLog, no hints |
|---|---|---|---|---|---|---|
| syn10 | index | 1.1 / 0.6 | 1.5 / 1.6 | 3.7 / 0.7 | 3.7 / 0.7 | 2.6 / 1.6 |
| syn10 | query | 1.0 / 0.8 | 1.1 / 2.6 | 1.0 / 0.8 | 1.0 / 0.8 | 1.1 / 2.5 |
| syn25 | index | 44.3 / 12.8 | 22.2 / 13.0 | 121.8 / 20.8 | 123.1 / 20.4 | 45.0 / 13.8 |
| syn25 | query | 54.1 / 36.7 | 48.0 / 28.1\* | 55.5 / 36.6 | 54.1 / 36.4 | 49.0 / 20.1 |

\* See [Performance](#performance): writing the results took 8 s longer in this run.

- **The hints matter for the index only.** Without them, Soufflé's index takes 2.7× as long on
  syn25 with 1 worker and 1.6× with 32, and FlowLog's 2.0× as long with 1 worker and 1.06× with 32.
  FlowLog's gain comes from `SummaryFlow` and `SummaryAlloc`. Their version-0 orders join the
  first `LocallyReachable` atom with `ComputedAccessPathNonEmptySplit(GH, G, H)` before the second
  `LocallyReachable`, which then joins on two columns, `a` and `GH`, instead of `a` alone. They
  also cut FlowLog's peak memory, by 55% with 1 worker and 35% with 32. The query's hints, none of
  which orders version 0, make no difference to Soufflé, and without the hints Soufflé's peak
  memory stays within 11 MB of its value with them.
- **On the same join orders**, without any hints, FlowLog's index is 2.7× as fast as Soufflé's on
  syn25 with 1 worker and 1.5× with 32 (4.5× with #499, [below](#with-the-interner-fix)). With the
  hints, FlowLog's index is 2.0× as fast with 1 worker and 2% slower with 32, though only Soufflé
  orders the later versions. Both FlowLog columns run the same query program. Its engine is 1.1×
  as fast as Soufflé's with 1 worker and 1.8× with 32 (the `flowlog-noplan` runs), with or without
  Soufflé's hints. On syn10, FlowLog's index is 1.4× as fast with 1 worker and takes 2.2× as long
  with 32, a fixed cost of its 32 workers on a small input.
- **The same join order is not the same evaluation.** Soufflé runs each version of a recursive rule
  as nested loops over indexes, with the version's delta relation at its written position. When
  the delta is an inner loop, each iteration scans the full outer relation again. The index's
  hints change 399 of its 667 recursive rule versions, 363 of them by moving the delta to the
  outermost loop: 613 versions start from the delta with the hints, 250 without. The query's
  hints change 5 of 103 versions, none of them the delta's position. FlowLog's differential-dataflow
  joins keep both inputs, and intermediate results, arranged (indexed). New tuples on any input
  are joined against the other side's arrangement, whatever the order. This is also where its
  extra memory goes.
- **Results:** a `.plan` changes only the evaluation order. But for each `choice-domain` key,
  Soufflé keeps the first candidate it derives, so its result depends on that order. Without the
  hints, 9–10 relations differ on syn25 and 3–4 on syn10. FlowLog's databases with and without the
  hints are identical, on both programs. Compared with 1 worker, where both engines are
  deterministic:

  | Program | Soufflé with hints, against `strict` | FlowLog, against Soufflé `strict` |
  |---|---|---|
  | syn10 | 3 `ReachableEdge` relations; 104 keys with another witness | the same 3; 130 keys |
  | syn25 | 9 relations; 1 of 340 call edges, 1.8% of `SummaryFlow` rows | 12 relations; 7 of 340 call edges, 4.8–6.5% of `SummaryFlow` rows |

  The same join order does not bring FlowLog's witnesses closer to Soufflé's: FlowLog differs from
  Soufflé `strict` in the same relations as from Soufflé with the hints. Among tied candidates,
  Soufflé keeps the first its loops reach, and the port the one with the smaller choice key
  ([Limitations](#limitations)).

**Compiling** each analysis once (both backends cache the binary), on the idle machine:

| Analysis | Soufflé compile | Soufflé binary | FlowLog compile | FlowLog binary |
|---|---|---|---|---|
| index | 2 min 43 s | 8.6 MB | 3 min 8 s | 168 MB |
| query | 1 min 10 s | 4.2 MB | 2 min 3 s | 116 MB |

Soufflé compiles one generated C++ file with `g++`. FlowLog compiles a generated Rust crate in a
cargo target dir where its runtime and dependencies are already built. Soufflé's variants without
hints compile in the same time.

To reproduce the benchmark:

```bash
P=ctadl/flowlog-backend; mkdir -p bench runs/bench
for n in 10 25; do python3 $P/gen_tnt.py --funcs $n --stmts 12 --seed 1 -o bench/syn$n.tnt; done
python3 $P/bench.py --jobs 1,32 \
    --backend souffle,flowlog,flowlog-noplan,souffle-noplan,souffle-strict \
    bench/syn10.tnt bench/syn25.tnt > runs/bench/summary.txt
K="--keys VirtualAssign=0,1,2,3,4,5,6 --schema $P/index_schema.json --schema $P/query_schema.json"
for s in syn10 syn25; do
  B=runs/bench/$s
  python3 $P/compare_db.py $K $B/souffle-j1/ctadlir.db $B/flowlog-j1/ctadlir.db
  python3 $P/compare_db.py $K $B/souffle-j1/ctadlir.db $B/souffle-strict-j1/ctadlir.db
  python3 $P/compare_db.py $K $B/souffle-strict-j1/ctadlir.db $B/flowlog-j1/ctadlir.db
  python3 $P/compare_db.py $K $B/flowlog-j1/ctadlir.db $B/flowlog-j32/ctadlir.db
  python3 $P/compare_db.py $K $B/flowlog-j1/ctadlir.db $B/flowlog-noplan-j1/ctadlir.db
done
```

`bench.py` diffs every run against the first one. The `compare_db.py` calls give the pairwise
comparisons in `results/bench/`, from the databases that `bench.py` keeps: syn25's take 1.07–1.10
GB each, and on syn25 each comparison takes 10–15 minutes. `--drop-dbs` deletes each database once it
has been compared, and `--prune` keeps only those that differ from the first run's. `--format
sarif` also writes and compares SARIF reports. On these programs the reports are hundreds of MB
(195 MB for syn10), and writing one takes longer than the analysis.

The checks on syn25, from the benchmark's databases: the choice-model check, the `--all-outputs`
index against the benchmark's, and FlowLog's query on Soufflé's index (`ctadl` resolves a relative
`-o` inside `--directory`):

```bash
D=runs/check; T=bench/syn25.tnt; B=runs/bench/syn25
for v in fa fq; do ctadl import taint-front $T -o $D/$v -f; done
ctadl --directory $D/fa index --engine flowlog --all-outputs -j 32
python3 $P/check_choice_model.py $D/fa/index.dl $D/fa/facts $D/fa/ctadlir.db $D/model -j 32 \
    --schema $P/index_schema.json
python3 $P/compare_db.py --schema $P/index_schema.json $D/fa/ctadlir.db $B/flowlog-j1/ctadlir.db
ctadl --directory $D/fq index -j 1
ctadl --directory $D/fq query --engine flowlog -j 32 -o "$PWD/$D/fq-summary.txt"
python3 $P/compare_db.py $K $B/souffle-j1/ctadlir.db $D/fq/ctadlir.db
cmp $B/souffle-j1/summary.txt $D/fq-summary.txt
```

### With the interner fix

With 32 workers, contention in FlowLog's string interner limits the runs above
([Suggestions](#suggestions), item 3). Its fix,
[#499](https://github.com/flowlog-rs/flowlog/pull/499), since merged (`b99ed29`), caches the
interner's lookups in each thread. `bench_reps.py` timed the backends again with FlowLog `main` at
`f14db3d` plus #499
(`87ed8e8`), in two sets: Soufflé and FlowLog with the hints, then both without. Each set has three
reps; a rep runs every backend in turn, in reverse order on even reps. Engine time in seconds,
medians of the three reps, with 1 / 32 workers:

| Program | Phase | Soufflé, CTADL's hints | FlowLog, their version 0 | Soufflé, no hints | FlowLog, no hints |
|---|---|---|---|---|---|
| syn10 | index | 1.1 / 0.6 | 1.5 / 1.3 | 3.7 / 0.7 | 2.6 / 1.4 |
| syn10 | query | 1.0 / 0.8 | 1.1 / 2.5 | 1.0 / 0.8 | 1.1 / 2.4 |
| syn25 | index | 44.4 / 12.9 | 20.9 / 3.3 | 122.0 / 20.8 | 44.3 / 4.7 |
| syn25 | query | 54.5 / 36.7 | 47.4 / 16.9 | 54.3 / 36.2 | 46.9 / 16.8 |

How many times as fast FlowLog's engine is as Soufflé's on syn25, with 1 / 32 workers:

| Hints | Index | Query |
|---|---|---|
| None, so both engines join in about the same order | 2.8× / 4.5× | 1.2× / 2.2× |
| CTADL's for Soufflé, their version 0 for FlowLog | 2.1× / 3.9× | 1.1× / 2.2× |
| CTADL's for Soufflé, none for FlowLog | 1.0× / 2.8× | 1.2× / 2.2× |

On syn25:

- **#499** speeds FlowLog up with 32 workers: its index computes in 2.9 s instead of 12.6 s (4.2 s
  instead of 13.3 s without the hints), and its query in 5.4 s instead of 10.8 s, writing to
  sqlite aside. `f14db3d` without #499 takes as long as the runs above (#499's own measurements).
  Soufflé's engine times, and FlowLog's with 1 worker, are within 3% and 6% of the runs above.
- **Scaling:** from 1 to 32 workers, FlowLog's index gets 6.3× faster with the hints and 9.5×
  without, and its query 2.8×. Soufflé's index gets 3.4× and 5.9× faster, and its query 1.5×.
- **End to end,** with 32 workers, FlowLog's index phase takes 7.1 s against Soufflé's 15.4 s, and
  7.9 s against 22.9 s without the hints. Its query phase still takes 1.1× as long as Soufflé's,
  82.8 s against 72.4 s. FlowLog computes the query's results in 5.4 s, but writes them to sqlite
  in 11.5 s, the port converts them in 18.6 s, and CTADL's indexing and report take 10.1 s longer
  than on Soufflé's database (row order, [Performance](#performance)). Item 6 of the
  [Suggestions](#suggestions) addresses these three.
- **Memory:** #499 changes FlowLog's peak memory by less than 9%. With 32 workers, FlowLog's index
  needs 7.5× Soufflé's memory, 12× without the hints, and its query 2.9×.
- **Results:** FlowLog's databases are the same as in the runs above, with and without the hints,
  and with 1 and 32 workers. Soufflé's with 1 worker are the same as in the runs above too, so
  FlowLog's differ from them in the same relations as above. With the hints and 32 workers,
  Soufflé's differ from its 1-worker ones in 3 relations on syn10 and 4 on syn25. CTADL's query
  report is the same in every run.

`results/bench-499/` has each rep's `summary.txt`; `medians.txt`, which also gives the wall times,
peak memory, FlowLog's time split into computing, writing and converting, and CTADL's steps after
the analysis; and `comparisons.txt`, the comparisons of the first rep's databases, and the
checksums of all reports. To reproduce, after the runs above:

```bash
git -C flowlog fetch origin pull/499/head && git -C flowlog worktree add ../flowlog-499 87ed8e8
(cd flowlog-499 && cargo build --release --target-dir target)
export FLOWLOG_COMPILER=$PWD/flowlog-499/target/release/flowlog-compiler \
       FLOWLOG_RUNTIME_PATH=$PWD/flowlog-499/flowlog-runtime
P=ctadl/flowlog-backend; PROGS="bench/syn10.tnt bench/syn25.tnt"
python3 $P/bench_reps.py run --out runs/warm --reps 1 --jobs 1 \
    --backend flowlog,flowlog-noplan bench/syn10.tnt
python3 $P/bench_reps.py run --out runs/bench-499/hints --backend souffle,flowlog $PROGS
python3 $P/bench_reps.py run --out runs/bench-499/no-hints \
    --backend souffle-noplan,flowlog-noplan $PROGS
python3 $P/bench_reps.py medians --before runs/bench runs/bench-499/hints runs/bench-499/no-hints \
    > runs/bench-499/medians.txt
```

The first run compiles FlowLog's analyses with #499; Soufflé's are cached from the runs above.
Each comparison in `comparisons.txt` is `compare_db.py -q $K` on the two databases it names.

## FlowLog findings

The programs in `repros/` are minimal. `repros/check.sh` compiles and runs them with FlowLog, and
`repros/check.sh -s` runs them with Soufflé. It finds FlowLog as the engine does, through
`FLOWLOG_COMPILER` and `FLOWLOG_RUNTIME_PATH` ([Usage](#usage)). The table compares the FlowLog results with Soufflé
2.5 on the same program. "`main`" is FlowLog's `main` at `84b3a1c`; "with #477" adds the B1 fix.
The outputs are in `results/repros/`: `souffle.txt`, `main.txt` and `with-477.txt`.
`main-b03d2fd.txt` has the results on `main` at `b03d2fd`, which has the fixes for B1, B2, B4, B6,
B7 and B11: those repros give Soufflé's results there, except `count_all_wild` (B2; [Behaviour
differences](#behaviour-differences)) and B7's `strlen` and `substr`. `main-8ce7b5e.txt` has the
results on `main` at `8ce7b5e`, which also has B8's fix: only `filter_order` changes, to Soufflé's
result. `main-b99ed29.txt` has the results on `main` at `b99ed29`, which has the fixes for B1–B13:
every repro of those bugs gives Soufflé's result there, except `count_all_wild` (by design), B7's
`strlen` and `substr`, and `empty_dataflow`, for which FlowLog writes no file. B15's
`neg_only_body` and the gap `dup_output` were found on `b99ed29`, and only its results have them.
`repros/same_name.sh` reproduces B14, which takes two compilations at once.

**Bugs**

| Repro | Soufflé 2.5 | FlowLog | Port |
|---|---|---|---|
| **B1** `string_constraint_literal`: integer literals inside a computed `contains`/`match` operand, e.g. `!contains(".*", substr(s, i, strlen(s) - i + 1))` | `O={abc}` | `main`: internal compiler error in codegen, `polymorphic literal Constant { text: "1", ty: IntLit } reached codegen; typechecker should have pinned it`. With #477: `O={abc}`. | Fixed by #477. CTADL's analysis has this shape. |
| **B2** `count_all_wild`: `n = count : { R(_, _) }` | `C={2}` | `main`: the generated crate fails rustc with E0308: `flowlog_aggregate` expects an `OrdValBatch` arrangement but gets an `OrdKeyBatch`. With #478: `C={1}`, by design ([Behaviour differences](#behaviour-differences)). | The translator names the placeholders, which gives Soufflé's count. |
| **B3** `lattice_min_string`: `min<string>` lattice column | (no lattices) | The front end accepts it, then rustc fails with E0277: `MinAccumulator<String>` is not `Clone`. | Choice keys are interned `int64`s. |
| **B4** `agg_body_binding`: an aggregate body with a constraint on an outer variable, such as a value computed from it, `n = count : { t = cat(a, "y"), S(t) }` | `O={x 1}` | rustc fails: ``unused variable: `t_…_arr` ``. The generator builds an arrangement that nothing reads, and generated crates build with `-Dwarnings`. | The translator relies on the fix: it binds `agg_computed`'s computed argument inside the aggregate body. No CTADL aggregate has these shapes. |
| **B5** `udf_strlen_arg`, `udf_grouped_arg`: `strlen` or `ord` as a UDF argument, `O(id(strlen(s))) :- S(s).`, or a variable that FlowLog replaces by its arithmetic definition, `O(id(y)) :- P(a), y = a + 1.` (also as a shift operand) | (no UDFs) | rustc fails: `unnecessary parentheses around function argument` (`-Dwarnings` again). | The translator relies on the fix. |
| **B6** `agg_shared_join`: two aggregates over the same join, one with `_` where the other names the column. `agg_many_readers`: one aggregate over a relation that twelve other rules read. | `O={b 1, m 2}`; `W={1 … 12, 100}` | Internal compiler error in the planner's dedup step: `dedup: map sum[](k) … reads keyed k …, which only a join or a map built here may read`. | Not worked around: CTADL's aggregates have neither shape. |
| **B7** `souffle_builtins`: `strlen("é")`, `substr("aéb", 1, 2)`, `1 ^ -1`, `(-1) ^ -1` | `2`, `é`, `1`, `-1` | `1`, `éb`, `0`, `0`: `strlen` and `substr` count characters, not bytes, and `^` is 0 for every negative exponent. | `souffle_strlen`, `souffle_substr` and `souffle_pow`. With the fix, FlowLog's `^` is exact, where Soufflé's truncates a `double`. |
| **B8** `filter_order`: `O(a, b) :- R("div", a, b), a / b > 1.` with `R("skip", 1, 0)` | `O={6 2}` | Panics: `attempt to divide by zero`. A rule's filter tests its comparisons before its atoms' constants. | Total ports of `/`, `%` and `to_number`, and check rules ([Runtime errors](#runtime-errors)). |
| **B9** `antijoin_projection`: `A(100 / x) :- N(x), !Z(x).` with `N(0)`, `N(4)`, `Z(0)` | `A={25}` | Panics: `attempt to divide by zero`. An antijoin computes the head on the rows the negation removes. | As for B8. |
| **B10** `literal_overflow` (FlowLog syntax): `O(9223372036854775807 + 1) :- K(_).` | With `number`: `O={-9223372036854775808}` | rustc fails: `this arithmetic operation will overflow`. A literal division by zero fails the same way (`unconditional_panic`). | The translator computes constant expressions itself, on 64 bits ([Integers](#integers)). |
| **B11** `cast_literal` (FlowLog syntax): `as(1, int64) bshl 31 > 0` and `as(-1, int64) bshru 1 = 2147483647` | With `number`: `O={shl}` | `O={shru}`: `as` leaves the literal `int32`. | The translator pins widths with `as` and relies on the fix ([Integers](#integers)). |
| **B12** `empty_dataflow`: no rule, fact or input derives an output | `A={}` | rustc fails: ``unused import: `super::*` ``. FlowLog prunes every relation. | The translator relies on the fix. FlowLog then writes no file for the output, and the engine creates it empty ([Pipeline](#pipeline)). |
| **B13** `type_limit_cmp`: `A(u) :- U(u), u >= 0.` on `unsigned` | `A={3}` | rustc fails: `comparison is useless due to type limits`. | The translator relies on the fix. |
| **B14** `same_name.sh`: two programs compiled at the same time to executables of the same name, `-o a/bin` and `-o b/bin`, with the same target dir (`-T`) | (no shared build) | `b` runs `a`'s program: `a: O={1}`, `b: O={1}`, every time. FlowLog names the crate after the executable, and each crate is a cargo workspace of its own, so the two share cargo's build of the crate, whose freshness cargo judges by mtimes. `b`'s sources are written while `a` builds, cargo finds `b` up to date, and flowlog-compiler copies `a`'s binary. | The engine names each analysis' crate after its cache key, and the tests name theirs after the run. |
| **B15** `neg_only_body`: a body whose only atoms are negated, `A(1) :- !B(1).` (also `A(x) :- x = 1, !B(x).`) | `A={1}` | Internal compiler error in the planner: `optimizer needs at least two core atoms, found 0 in a(1) :- !π[](b)().` Bodies with only an aggregate or only comparisons compile. | The translator anchors a body with no positive atom or aggregate on `FlowlogUnit`. |

B8's report also mentions a variable repeated in an atom, `O(x) :- R(x, x, b), x / b > 1.`.
FlowLog tests the repetition after the comparison too, but this is not a difference from Soufflé:
Soufflé 2.5 also tests such variables after the comparisons, and stops on `R(1, 2, 0)`.

B1–B13 are filed on flowlog-rs/flowlog, with their repros rechecked on `84b3a1c`, and each has a
fix PR. B14 was found later, on `b03d2fd`, and B15 on `b99ed29`.
The status is as of `main` at `b99ed29`: all of B1–B13 are fixed there.

| Bug | Issue | Fix | Status |
|---|---|---|---|
| B1 | [#464](https://github.com/flowlog-rs/flowlog/issues/464) | [#477](https://github.com/flowlog-rs/flowlog/pull/477) | merged |
| B2 | [#465](https://github.com/flowlog-rs/flowlog/issues/465) | [#478](https://github.com/flowlog-rs/flowlog/pull/478) | merged |
| B3 | [#466](https://github.com/flowlog-rs/flowlog/issues/466) | [#485](https://github.com/flowlog-rs/flowlog/pull/485) | merged |
| B4 | [#467](https://github.com/flowlog-rs/flowlog/issues/467) | [#488](https://github.com/flowlog-rs/flowlog/pull/488), on top of #479 | merged |
| B5 | [#468](https://github.com/flowlog-rs/flowlog/issues/468) | [#483](https://github.com/flowlog-rs/flowlog/pull/483) fixes the repros, and #486 is closed. [#500](https://github.com/flowlog-rs/flowlog/pull/500) removes a last pair of parentheses, around `substr`'s index, which only clippy rejects | #483 merged; #500 open |
| B6 | [#469](https://github.com/flowlog-rs/flowlog/issues/469) | [#479](https://github.com/flowlog-rs/flowlog/pull/479) | merged |
| B7 | [#470](https://github.com/flowlog-rs/flowlog/issues/470), for `^` only | [#480](https://github.com/flowlog-rs/flowlog/pull/480); [#489](https://github.com/flowlog-rs/flowlog/pull/489) also makes `^` exact for an exponent past `u32` | merged |
| B8 | [#471](https://github.com/flowlog-rs/flowlog/issues/471) | [#481](https://github.com/flowlog-rs/flowlog/pull/481) | merged |
| B9 | [#472](https://github.com/flowlog-rs/flowlog/issues/472) | [#482](https://github.com/flowlog-rs/flowlog/pull/482) | merged |
| B10 | [#473](https://github.com/flowlog-rs/flowlog/issues/473) | [#483](https://github.com/flowlog-rs/flowlog/pull/483): FlowLog's runtime defines integer arithmetic, with wrap-around | merged |
| B11 | [#474](https://github.com/flowlog-rs/flowlog/issues/474) | [#487](https://github.com/flowlog-rs/flowlog/pull/487); [#492](https://github.com/flowlog-rs/flowlog/pull/492) does the same for an `as` whose operand is not a literal ([#490](https://github.com/flowlog-rs/flowlog/issues/490)) | merged |
| B12 | [#475](https://github.com/flowlog-rs/flowlog/issues/475) | [#484](https://github.com/flowlog-rs/flowlog/pull/484) | merged |
| B13 | [#476](https://github.com/flowlog-rs/flowlog/issues/476) | [#483](https://github.com/flowlog-rs/flowlog/pull/483) | merged |

B7's `strlen` and `substr`, which count characters where Soufflé counts bytes, are not filed, nor
are B14, B15 and the gap `dup_output`.

**Gaps** (Soufflé features FlowLog does not support)

| Repro | Soufflé 2.5 | FlowLog | Port |
|---|---|---|---|
| `choice_domain` | `C={1 2}` | syntax error | policies (above); a different valid witness, `C={1 3}` |
| `nested_comp` | | ``unknown component `Inner` `` | hoisting |
| `record_type` | | expected `type_ref` | flattening |
| `functor_decl` | (needs a C++ library) | syntax error | `.extern fn` + Rust UDFs |
| `inline_rel` | | syntax error | expansion at use sites |
| `range_gen` | | ``call to undeclared UDF `range` `` | precomputed `FlowlogRange` table |
| `variadic_cat` | | `` `cat` expects 2 argument(s) but got 3 `` | nested binary `cat` |
| `agg_computed` | `O={x 1}` | syntax error: expected a constant, variable or `_` argument | binding inside the aggregate body |
| `agg_computed_lifted` | `O={x 1, z 0}` | ``assignment-bound variable `t` cannot be used in an atom with a computed value`` | not supported (CTADL does not need it) |
| `neg_computed` | `O={}` | syntax error: expected constant | `NegPre` helper relation |
| `neg_computed_lifted` | `O={z}` | same as `agg_computed_lifted` | `NegPre` helper relation |
| `computed_fact` | `C={ab}` | rule body reduces to nothing but its head is not a constant fact | `FlowlogUnit` anchor |
| `computed_body_only` | `C={ab}` | same as `computed_fact` | `FlowlogUnit` anchor |
| `true_false` | | expected `arithmetic_op` or `compare_op` | simplification |
| `keyword_fn` | | expected variable | renaming |
| `pragma` | | syntax error | dropped |
| `agg_min_string` | rejected too | ``aggregation `Min` requires a numeric input`` | int64 choice keys |
| `dup_output`: `.output R` and `.output R(filename="R2.csv")` | `R={1} R2={1}` | ``duplicate .output directive for relation `r` `` | a copy of the relation for each further output, at top level |

Where the Soufflé column is empty, both engines compute the obvious result once the program is
accepted.

`repros/check.sh -t` passes each repro through the translator, then compiles and runs it the way the
engine does. With FlowLog `main` at `b99ed29` (`results/repros/translated-b99ed29.txt`):

- **Twenty-four** rewritten repros give exactly Soufflé's result, among them B1, B2, B4, B6–B9,
  B13, B15 and the gap `dup_output`. `antijoin_projection` also writes the check relation
  `FlowlogAbort`, empty.
- **Other results:** `functor_decl` computes `O={x}`. `choice_domain` keeps `C(1, 3)` where
  Soufflé keeps `C(1, 2)`. Both are valid choices: the two candidates tie in the same clause and
  round ([Limitations](#limitations)).
- **Unsupported:** `agg_computed_lifted`, which the translator rejects. CTADL does not need it.
- **The rest:** both engines reject `agg_min_string`. `check.sh` runs the binary alone, which
  writes nothing for the outputs of `empty_dataflow` and `empty_output` that FlowLog prunes; the
  engine creates them empty, as Soufflé does. `cast_literal`, `literal_overflow`,
  `udf_grouped_arg`, `udf_strlen_arg` and the `lattice_*` repros are FlowLog programs. The
  semantics suite's `widths.dl` tests the integer rewrites against Soufflé.

With `main` at `84b3a1c` plus #477 (`results/repros/translated.txt`), the translator worked around
B2, B5 and B10–B13, and nineteen repros gave Soufflé's result. It rejected `agg_computed` and
`agg_body_binding` (B4), and FlowLog failed on `agg_shared_join` and `agg_many_readers` (B6).
`translated-b03d2fd.txt` has the results on `b03d2fd`.

<a id="behaviour-differences"></a>
**Behaviour differences**

- **`count_all_wild`:** with #478, FlowLog answers `C={1}` and Soufflé `C={2}`. FlowLog aggregates
  over the distinct bindings of the body's named variables, and `_` names nothing. Soufflé
  aggregates over the tuples of a single atom, and over the distinct bindings when it outlines the
  body into a relation, as it does for several atoms
  ([FlowLog's design notes](https://github.com/flowlog-rs/flowlog/blob/main/docs/design/aggregation.md)).
  The translator names the wildcards where Soufflé reads an atom's tuples, so the port counts as
  Soufflé does.
- **`empty_output`:** FlowLog writes no table or file for an output relation that it prunes, such
  as one with no rules. Soufflé creates an empty one, and so does the engine: the sqlite adapter
  creates the missing tables, and `facts.place_outputs` the missing files.
- **Nullary output files:** for a nullary relation that holds, FlowLog writes the line `True`,
  and Soufflé `()`. For one that does not hold, both write an empty file. The engine writes
  Soufflé's `()`. Both read a nullary fact file as holding when it has any line. Soufflé cannot
  write a nullary relation to sqlite (`CREATE TABLE '_R' ()` fails).
- **Fact files:** FlowLog skips blank lines, except in the files of nullary relations. Soufflé reads
  a blank line as a row of one empty field: an empty symbol in a one-column symbol relation, an
  error in any other. FlowLog also stops on a line with more columns than the relation (`line 1 has
  more than 1 columns`), where Soufflé ignores the extra ones; the engine's conversion of record
  files does the same. CTADL's fact files have neither.
- **Output files in a missing directory:** FlowLog fails, and Soufflé 2.5 silently writes nothing.
  The engine creates the directory, so the port writes the file.
- **`lattice_in_loop`:** a `min` lattice relation that is read inside its own recursion also
  exposes superseded rows to the loop. The minimal `Dist` is right, but `Via` also holds node 2's
  first distance (`2 5`). This is what makes `choice-domain` inside recursion hard.

<a id="suggestions"></a>
### Suggestions for FlowLog, by value to a CTADL port

1. **Native `choice-domain`**, or an aggregate that picks one witness per key and never revises it
   inside recursion. It is the only non-syntactic gap, and it would replace the stateful UDFs.
2. **B14 and B15**, the open bugs. For B14, a crate name that also depends on the output path,
   through a hash of it, would do; for B15, the planner could read a body with no positive atom
   from a one-row relation, as the translator does. The fixes for B1–B13 are merged, and the
   translator now relies on them instead of working around B2, B4, B5 and B10–B13.
3. **String interning that scales with workers**
   ([#498](https://github.com/flowlog-rs/flowlog/issues/498)). With `--str-intern`, the generated
   code interns or resolves strings on every row, at 859 `intern` and 1,024 `resolve` call sites in
   the code for CTADL's index, and every call takes a read lock on a map that all workers share.
   With 32 workers, the locks are contended: perf puts 67% of the index's CPU time and 51% of
   the query's in the interner, and the index computes only 1.7× as fast as with 1 worker.
   [#499](https://github.com/flowlog-rs/flowlog/pull/499) caches the lookups in each thread. With
   it, on syn25 with 32 workers, the index computes in 2.9 s, 7.1× as fast as with 1 worker, and
   the query in 5.4 s, against 12.6 s and 10.8 s without it. FlowLog's index is then 3.9× as fast
   as Soufflé's, and its query 2.2× ([With the interner fix](#with-the-interner-fix)). Code
   generation could avoid most of the calls: intern a rule's string literals once rather than per
   row, and pass strings to UDFs as `&str`.
4. **64-bit literals:** a literal that no column or parameter types is an `int32`. The translator
   pins such terms to 64 bits with `as`, which types them since #487 and #492. An option to
   default to `int64` would make these anchors unnecessary ([Integers](#integers)), and an `int64`
   `ord`, `souffle_ord`.
5. **Soufflé-compatible syntax**: nested components; `inline`; records (flattening would be enough);
   `range`; variadic `cat`; expression arguments of atoms; negation and aggregation over computed
   values; computed facts and bodies; `true`/`false`; ignoring unknown `.pragma`s;
   `.functor`/`@F` mapping to `.extern fn`.
6. **Output parity:** create empty tables and files for the outputs it prunes, as Soufflé does, and
   write every `.output` of a relation (`dup_output`); the engine and the translator do both now.
   Writing Soufflé's sqlite layout (a symbol table, shadow tables of ids,
   and views) as an option would remove most of the port's conversion. On syn25 with 32 workers,
   FlowLog computes the query's results in 10.8 s, then takes 9.3 s to write them as plain tables,
   and the port 19.0 s to convert them; with #499, 5.4 s, 11.5 s and 18.6 s. With several workers,
   FlowLog should also write each relation in the order it uses with 1 worker: CTADL's indexing
   and report take 7.3 s longer on the 32-worker database ([Performance](#performance)).
7. **Compile time and binary size.** CTADL's index and query take 3 min 8 s and 2 min 3 s to
   compile, against Soufflé's 2 min 43 s and 1 min 10 s. The binaries are 19–28× larger (168 and
   116 MB). Both engines cache the binary, so this cost is paid once per version of the analysis,
   but it slows down work on the analysis itself.
8. **Memory.** FlowLog needs about 70 MB per worker whatever the program: CTADL's index of a small
   Android app takes 0.15 GB with 1 worker, 0.33 GB with 4 and 2.3 GB with 32, where Soufflé takes
   0.05 GB. On large inputs it needs 2–9× Soufflé's peak memory, for example 19 GB against 9.4 GB
   for nginx's index ([Front ends](#front-ends)).

## Limitations

- **Choice ties.** A key can get several candidates from the same clause in the same round.
  Soufflé keeps the one it inserts first. With one thread, that is the first in its scan order,
  which depends on symbol numbering. With several threads, it is whichever thread gets there
  first, so the choice changes between runs; CTADL runs Soufflé with half the CPUs by default. The
  port keeps the smaller choice key: shorter dependent fields, then hash. Every choice is valid,
  but downstream relations depend on it.
  - On CTADL's tests and corpora this happened once in 435 runs: in the `star` variant on
    `alias4`, `LostAssign` for `f/assign/9` gets `.manager.manager.thing` in Soufflé and
    `.manager.manager.*` in FlowLog. Soufflé derives both candidates when the `choice-domain` is
    removed.
  - On larger programs ties are common. In 9 of 30 synthetic programs they changed downstream
    relations ([Larger programs](#larger-programs)).
  - Reproducing one particular Soufflé result would need Soufflé's tie order. That depends on its
    join order, indexes, symbol numbering and thread scheduling.
  - The `choice_domain` repro and `tests/flowlog/choice_mini` document such ties.
- **In-recursion choice** relies on evaluation order ([above](#choice-domain)).
- **Runtime errors:** where Soufflé stops on a partial call that the translator cannot check, the
  port completes, with the call's result 0. The translator lists these calls in a warning
  ([Runtime errors](#runtime-errors)). In CTADL there are three, and they cannot fail.
- **Translator coverage:** only the Soufflé subset that CTADL uses is translated. Everything else is
  rejected with an error, not mistranslated. A `choice-domain` relation that the policy file does
  not name is encoded as `minchoice`, like CTADL's four relations whose dependent columns are not
  functions of the key, and with the same caveats ([above](#choice-domain)). The translator also
  refuses some valid Soufflé programs that it could not translate faithfully:
  - arithmetic whose type depends on the order of Soufflé's alias resolution (`alias_order`,
    `alias_several`);
  - a variable name that Soufflé types differently in two disjuncts or sibling aggregates of a
    rule (`disjunct_types`, `agg_local_types`), since the translator types a rule's names once;
  - `^` and `range` over `unsigned`, and `min`/`max` of a target that reads no local variable;
  - component names that denote different relations in different instances, or an enclosing
    instance's `choice-domain` or type-parameter relation; components with type parameters and
    nested components ([Components](#components)).
- **File I/O:** the engine needs relative file names, as CTADL's are. Record columns in a file need
  the tab delimiter. Several `.output`s of a relation work at top level, at most one of them not to
  a file; FlowLog rejects a second one that comes from a component (a component's `.output` of an
  outer relation, instantiated twice, or `.output inst.R` next to the component's own). Fact files
  are read as FlowLog reads them, for blank lines and extra columns ([Behaviour
  differences](#behaviour-differences)).
- **Symbol numbering:** CTADL's P-code type propagation (`typeprop.dl`) orders its constraint
  edges by `ord` and represents each class by its varnode of least `ord`, so `CVar_Type` and
  `VarnodePointerToAggregate`, and on larger programs the data-flow IR, depend on how the engine
  numbers symbols. FlowLog numbers the symbols of the facts in the order it loads them, which on
  curl and nginx is Soufflé's order; nothing guarantees this. `ord_oracle.sh` checks FlowLog's
  results against Soufflé's under FlowLog's numbering. Strings that rules compute are numbered in
  evaluation order, by either engine; the comparisons rename the ids that CTADL derives from them.
- **Front ends:** CTADL's P-code and JADX front ends were run on curl, nginx and DroidBench's apps
  ([Front ends](#front-ends)). With CTADL's JADX plugin 0.7.0, apps that index an array with a
  variable cannot be analysed with either engine: the plugin writes such an index of an array read
  or write (`AGet`, `APut`) as the variable, where CTADL 0.14.1 declares a `number`; CTADL's own
  check uses an app that does not.

<a id="disk-usage"></a>
## Disk usage

- **Compiled analyses:** each takes about 110–250 MB in CTADL's state directory
  (`~/.local/state/ctadl/analysis/<version>/flowlog/`). Delete the entries you no longer need; any
  change to the translator, policy, UDFs or compiler produces new keys.
- **Shared target dir:** the cargo target dir (`CARGO_TARGET_DIR`, by default `target/` in the
  cache) keeps the compiled FlowLog runtime and dependencies, about 0.8 GB. Since each analysis'
  crate is named after its key (B14), it also keeps cargo's copy of each compiled analysis
  (`release/deps/ctadl_<key>-*`). Deleting the target dir is safe: the next compilation first
  rebuilds the runtime. The tests use it too when it is set, or `ctadl-test-output/flowlog-target`,
  and remove their programs' builds from it when they exit.
- **Build dirs:** FlowLog enables rustc incremental compilation when given a build dir (`-B`). The
  engine would never reuse one, and each would keep about 1 GB, so it passes none: the compiler
  then builds in a scratch directory next to its output and deletes it after a successful build.
- **Run databases:** the runners' `--prune` deletes the databases of identical runs.

## Versions

| Component | Version |
|---|---|
| FlowLog | `main` at `b99ed29` (perf(runtime): cache interner lookups per thread #499), which has the fixes for B1–B13: the validation runs and the repros. The benchmarks and earlier runs: `main` at `84b3a1c` (fix(deps): update rust crate rusqlite to 0.41 #461) + PR #477 (`fed6293`, the B1 fix); also checked on `main` at `bad0ff2` (#493) + #477, and on `main` at `b03d2fd` (#488) and `8ce7b5e` (#481). The runs [with the interner fix](#with-the-interner-fix): `main` at `f14db3d` (#479) + PR #499 (`87ed8e8`) |
| CTADL | 0.14.1, `4f6f8cb` |
| ctadl-rs | `7c98b73e` |
| Soufflé | 2.5 |
| Front ends | Ghidra 11.2 with CTADL's Ghidra plugin 0.1.0, CTADL's JADX plugin 0.7.0, on Temurin JDK 21.0.13+9 |
| Front-end inputs | curl 7.81.0 (Ubuntu 22.04's `/usr/bin/curl`); nginx 1.24.0, built with `--with-debug --with-cc-opt=-gdwarf-4 --without-http_gzip_module --without-http_rewrite_module`; DroidBench's 188 apps (`master` at `a57fa6f`) |
| g++ | 11.4.0 (compiles Soufflé's analyses) |
| Rust | 1.95.0 |
| Python | 3.10.12 |

Machine: 64 hardware threads (32 cores of an AMD EPYC 7763, 2 threads per core), 503 GB RAM,
Ubuntu 22.04. The runs use 32 workers, CTADL's default on it.

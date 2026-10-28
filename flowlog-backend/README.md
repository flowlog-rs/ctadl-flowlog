# CTADL on FlowLog

This experiment runs [CTADL](https://github.com/sandialabs/ctadl)'s taint analysis on
[FlowLog](https://github.com/flowlog-rs/flowlog) instead of Soufflé. CTADL's analysis is written
as Soufflé Datalog programs. The questions:

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
- **CTADL's own test expectations** pass on both backends.
- **Semantics suite:** small programs check each translated construct against Soufflé. 18 programs
  give Soufflé's rows with 1, 4 and 32 workers; on 11 more, Soufflé stops with an error and so does
  FlowLog. The translator refuses 41 programs it cannot translate faithfully, rejects 7 erroneous
  programs as Soufflé does, and warns about 3 ([Validation](#validation)).
- **Integers:** Soufflé computes on 64 bits with wrap-around. FlowLog types a literal `int32`
  where no column types it, and rustc rejects some constant arithmetic in the crate FlowLog
  generates. The translator computes constant expressions itself and pins the rest to 64 bits
  ([Integers](#integers)). CTADL's program needs none of this.
- **Runtime errors:** Soufflé stops a program on `to_number` of a non-number and on division by
  zero. The port stops too wherever it can prove that Soufflé evaluates the call, and warns about
  the other calls. CTADL's three such calls parse option values that cannot fail
  ([Runtime errors](#runtime-errors)).
- **Larger programs:** on 30 generated programs, `choice-domain` keys often get tied candidates.
  Soufflé breaks such ties by thread scheduling and the port by a hash. In 9 programs this changes
  downstream relations. Every FlowLog result is a valid choice model of the program. Soufflé's
  own results also vary: with 32 threads, its downstream relations differ from its 1-thread
  results in 3 of the 30 programs ([Larger programs](#larger-programs)).

FlowLog does not accept CTADL's program as is. It took four things:

1. **A Soufflé→FlowLog translator** (`souffle2flowlog.py`). It rewrites the Soufflé features
   FlowLog lacks or spells differently ([table](#translation)). Most rewrites are syntactic.
   `choice-domain` is the one that needs real design work.
2. **Rust UDFs** (`ctadl_udf.rs`), loaded through FlowLog's `.extern fn` and `--udf-file`:
   - Ports of CTADL's 13 C++ functors (call strings, access paths, substring checks). Soufflé
     loads these from a C++ shared library.
   - Soufflé's semantics for builtins that FlowLog computes differently (`strlen`, `substr`,
     `to_number`, `^`, `ord`), and for `/` and `%`, which panic in FlowLog on a zero divisor even
     where Soufflé never divides (bugs B7–B9).
   - Identity functions `flowlog_int64` and `flowlog_uint64`, which make FlowLog compute an
     expression on 64 bits where it would use `int32` (bugs B5, B11).
   - Stateful helpers that emulate `choice-domain`:
     - `choice_keyN` and `choice_field` intern a candidate's dependent columns into an `int64`
       key, so that a FlowLog `min` lattice keeps one deterministic witness per key.
     - `choice_claimN` fixes the witness that reads see inside recursion.

   UDFs fit the functors naturally. For `choice-domain` they are a workaround: the claims are
   mutable state outside the dataflow ([details](#choice-domain)).
3. **One FlowLog compiler fix**, for an internal compiler error that CTADL's string constraints
   trigger (B1, [flowlog-rs/flowlog#464](https://github.com/flowlog-rs/flowlog/issues/464)). The
   fix is [flowlog-rs/flowlog#477](https://github.com/flowlog-rs/flowlog/pull/477), not yet merged
   into FlowLog's `main` ([Setup](#setup)).
4. **Glue:**
   - a drop-in `ctadl` command;
   - sqlite layout adapters between FlowLog's plain tables and the Soufflé symbol-table layout
     that CTADL's Python tooling reads;
   - a fact-file converter.

**Performance:** on the larger generated program, FlowLog's engine time is close to Soufflé's as
CTADL runs it: the index is 3–11% slower, and the query 1.1× as fast with 1 worker and 1.8× with 32.
CTADL gives Soufflé hand-written join orders (`.plan`: 67 in the index, 6 in the query), which the
translation drops. Without them both engines join in the order the rules are written (FlowLog has
no SIPS), and FlowLog's index is 2.7× as fast as Soufflé's with 1 worker and 1.5× with 32
([Join order](#join-order)). FlowLog's index needs 5–11× Soufflé's memory. End to end, FlowLog's
query phase takes 1.1–1.2× as long as Soufflé's: the port converts its results into CTADL's
sqlite layout, which takes 18–19 s. Compiling takes 1.2–1.8× as long, and the binaries are 19–28×
larger ([Performance](#performance)).

**FlowLog bugs:** the port found thirteen (B1–B13), each with a minimal repro in `repros/`. Each is
filed on FlowLog, with a fix PR ([FlowLog findings](#flowlog-findings)). CTADL's program needs the
fix for B1. The translation works around the others, except B6 and some shapes of B4, which
CTADL's program does not have.

## Layout

The port is `flowlog-backend/` in this fork of CTADL. The rest of the fork is CTADL 0.14.1
(`4f6f8cb`), unchanged. The scripts expect a workspace that holds the fork, FlowLog and ctadl-rs
side by side. Run them from the workspace:

```
workspace/
  ctadl/                this fork; taint-front front end built with dune
  flowlog/              FlowLog main (84b3a1c) plus the B1 fix, PR #477
  ctadl-rs/             ctadl-rs (7c98b73e), for its .tnt test corpus
  bin/taintfront        symlink to ctadl/taint-front/_build/default/taint.exe
  .venv/                Python env with ctadl and its taint-front plugin
  fltarget/             cargo target dir shared by all FlowLog compilations
  bench/                synthetic benchmark programs (gen_tnt.py)
  runs/                 output of the test and benchmark runs
```

The port, `ctadl/flowlog-backend/`:

```
flowlog_ctadl.py      drop-in `ctadl` command that uses FlowLog as the backend
souffle2flowlog.py    Soufflé -> FlowLog translator (also a CLI)
policy.json           per-relation choice-domain policies
ctadl_udf.rs          Rust UDFs: functor ports, Soufflé builtins, choice helpers
fl_db_adapter.py      sqlite layout conversion (FlowLog <-> Soufflé)
convert_facts.py      fact directory preparation (record flattening)
index_tf.fl, query_tf.fl,            the translated taint-front analyses and their
index_schema.json, query_schema.json   schema manifests (for reference; the driver
                                       regenerates them)
run_taintfront.py     differential runner (CTADL tests or any .tnt corpus)
run_variants.sh       differential runs under non-default CTADL options
run_sweep.sh          differential runs on generated programs: Soufflé -j 1 against Soufflé
                      and FlowLog on JOBS workers, and the choice-model check
compare_db.py         relation-by-relation DB comparison
compare_sarif.py      SARIF comparison
check_choice_model.py checks that a FlowLog DB is a valid choice model (Soufflé on the
                      program restricted to FlowLog's choice-domain witnesses)
bench.py, gen_tnt.py  benchmark harness, synthetic program generator
repros/               minimal programs for each FlowLog bug/gap, check.sh
tests/choice_mini/    regression test for the recursive choice-domain emulation
tests/semantics/      semantics suite: the translator and UDFs against Soufflé
tests/adapter/        sqlite record columns, with a DB that Soufflé writes
results/              summaries of the runs reported here
```

## Setup

You need Soufflé 2.5 (the reference backend; CTADL needs it anyway), stable Rust (1.95 was used),
Python 3.10 or later, OCaml with dune (for CTADL's taint-front front end), and sqlite3.

```bash
mkdir workspace && cd workspace
git clone -b flowlog-backend https://github.com/flowlog-rs/ctadl-flowlog ctadl
git clone https://github.com/flowlog-rs/flowlog
(cd flowlog && git fetch origin pull/477/head && git checkout -b ctadl-port FETCH_HEAD &&
 cargo build --release)
git clone https://github.com/sandialabs/ctadl-rs && git -C ctadl-rs checkout 7c98b73e
python3 -m venv .venv && . .venv/bin/activate
pip install -e ctadl -e ctadl/plugins/taint-front
(cd ctadl/taint-front && dune build)
mkdir -p bin && ln -s "$PWD/ctadl/taint-front/_build/default/taint.exe" bin/taintfront
export PATH=$PWD/bin:$PATH FLOWLOG_CARGO_TARGET=$PWD/fltarget
```

`ctadl-port` is FlowLog's `main` at `84b3a1c` plus the B1 fix: PR
[#477](https://github.com/flowlog-rs/flowlog/pull/477), at `fed6293`. All the runs reported here
used it. On the next `main` commit, `bad0ff2`, with #477 merged in, CTADL's taint-front tests, the
semantics suite and the `choice-domain` test give the same results. Once #477 is merged, FlowLog's
`main` works as is. The port needs none of FlowLog's other open fixes.

The runs write large databases (1.07–1.10 GB each on the larger benchmark program), and the compiled
analyses and the cargo target dir take a few GB ([Disk usage](#disk-usage)). Put the workspace,
`FLOWLOG_CTADL_CACHE` and `TMPDIR` on a disk with room.

## Usage

`flowlog_ctadl.py` takes the same arguments as `ctadl`:

```bash
P=ctadl/flowlog-backend
python3 $P/flowlog_ctadl.py import taint-front prog.tnt -o work
python3 $P/flowlog_ctadl.py --directory work index -j 8
python3 $P/flowlog_ctadl.py --directory work query --format sarif -o prog.sarif
```

- **First compile:** the first `index` or `query` with a given analysis program translates and
  compiles it, which takes a few minutes. The result is cached by content hash in
  `~/.cache/ctadl-flowlog/<key>`, the same way CTADL caches its Soufflé binaries.
- **Cache key:** it covers the translator, `policy.json`, `ctadl_udf.rs`, the program and the
  compiler binary. Editing any of them triggers a recompile.
- **Environment variables:** `CTADL_CLI`, `FLOWLOG_COMPILER`, `FLOWLOG_RUNTIME_PATH`,
  `FLOWLOG_CTADL_CACHE`, `FLOWLOG_CTADL_POLICY` (an alternative `policy.json`),
  `FLOWLOG_CARGO_TARGET` and `FLOWLOG_CTADL_TIMING` (per-step timings). See the docstring in
  `flowlog_ctadl.py`.

The translator also works standalone:

```bash
python3 $P/souffle2flowlog.py analysis.dl -o analysis.fl --policy $P/policy.json --schema schema.json
flowlog/target/release/flowlog-compiler analysis.fl --str-intern --udf-file $P/ctadl_udf.rs -o analysis
```

## How it works

### Pipeline

CTADL's own command-line driver does all the work except running the Datalog analysis:

- importing,
- model generation,
- macro preprocessing of the `.dl` sources,
- index metadata,
- query views,
- SARIF export.

`flowlog_ctadl.py` loads CTADL's `ctadl` script as a module and replaces its `build_analysis` hook.
`index` and `query` then go through `FlowLogAnalysis`, a stand-in for CTADL's
`SouffleCompiledAnalysis`, which works in three steps:

1. **Translate.** `souffle2flowlog.py` translates the preprocessed Soufflé program into
   `program.fl`. It also writes `schema.json`, a manifest of the sqlite inputs and outputs with
   their original attribute names and flattened record columns.
2. **Compile.** `flowlog-compiler --str-intern --udf-file ctadl_udf.rs` compiles the program. It
   runs with `CARGO_INCREMENTAL=0`; see [Disk usage](#disk-usage).
3. **Run.** The binary runs as `-F in -D out -w <jobs>`. Around the run:
   - **Inputs:** fact files are prepared by `convert_facts.py` (missing files read as empty;
     record columns are flattened). sqlite inputs, which the query phase reads from the index
     DB, are converted to FlowLog's layout by `fl_db_adapter.py`. Inputs that no rule reads are
     created empty. CTADL's query declares 46 of them, including three with record columns.
   - **Outputs:** FlowLog writes each relation as a plain table. `fl_db_adapter.py` converts the
     outputs to Soufflé's layout (a `__SymbolTable`, shadow tables `_R` holding ids, and views
     `R`) and appends them to CTADL's database. CTADL's Python tooling and SQL views therefore
     work unchanged.

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
| Record types `[a: T, b: U]` | One column per field (`attr_field`). In sqlite, records become synthetic ids, with the fields kept in `__FlowLogRecord`. A DB that Soufflé writes keeps only the ids, so a query that reads a record column needs a FlowLog index. CTADL's query reads none, so it also runs on a Soufflé index. | `record_type`, `tests/adapter` |
| `.functor F(..)`, `@F(..)` | `.extern fn F(..)`, `F(..)`; the implementations are Rust UDFs | `functor_decl` |
| Variadic `cat(a, b, c)` | `cat(cat(a, b), c)` | `variadic_cat` |
| `x = range(lo, hi)` | `FlowlogRange(hi, x), x >= lo`. `FlowlogRange` is a precomputed table of `0 <= x < hi < 1024`, built without recursion. Other bounds use a demand-driven recursive `RangeN` relation. | `range_gen` |
| `choice-domain` | Per-relation policy ([below](#choice-domain)) | `choice_domain` |
| `inline` relations | Expanded at their positive uses, as Soufflé does. Materialised when used negated or in an aggregate, or when their clauses are given per component instance. Unused ones are dropped. | `inline_rel` |
| `true`, `false`, `!(a < b)`, `x = x` | Simplified; rules with unsatisfiable bodies are dropped | `true_false` |
| Expression arguments of atoms, `S(cat(a, "y"))` | `t = cat(a, "y"), S(t)`. In an aggregate, the binding goes inside the aggregate body. FlowLog cannot compile it there when it reads only outer variables and constants (bug B4), so the translator rejects that case. CTADL has no such aggregate. | `agg_computed` |
| Negation of a computed value, `!S(t)` with `t = cat(..)` | A `NegPreN` helper relation materialises the positive part, then `NegPreN(.., t), !S(t)` | `neg_computed`, `neg_computed_lifted` |
| Facts with computed arguments, `C(cat("a", "b")).`, and bodies that bind only by computation, `C(s) :- s = cat("a", "b").` | `C(cat("a", "b")) :- FlowlogUnit(_).`, `C(s) :- FlowlogUnit(_), s = cat("a", "b").` | `computed_fact`, `computed_body_only` |
| `_` inside an aggregate body | Named fresh variables (works around bug B2) | `count_all_wild` |
| Variables named like FlowLog keywords (`fn`) | Renamed | `keyword_fn` |
| `.plan`, `.pragma`, representation qualifiers | Dropped (`.plan`: [Join order](#join-order)). `.limitsize`, which truncates a relation, is rejected. | `pragma` |
| Output relations that no fact, input or rule derives | A rule that never fires, `A(0) :- FlowlogUnit(u), u != 0.`. Otherwise FlowLog prunes every relation and the generated crate does not compile (bug B12). | `empty_dataflow` |
| `number`, `unsigned` | `int64`, `uint64`: CTADL needs Soufflé's 64-bit domain. Constant expressions are computed with Soufflé's wrap-around, and expressions FlowLog would compute on `int32` are pinned to 64 bits (bugs B5, B10, B11, B13; [below](#integers)). `float` is rejected (CTADL has none). | `literal_overflow`, `cast_literal`, `type_limit_cmp`, `udf_grouped_arg` |
| `strlen`, `substr`, `^`, `ord` | Calls to UDFs with Soufflé's semantics (`souffle_strlen`, …). FlowLog's `strlen` and `substr` count characters, not bytes, and its `^` is 0 for every negative exponent (bug B7). Its `ord` is an `int32`, which FlowLog cannot widen. | `souffle_builtins` |
| `to_number`, `/`, `%` | Total UDFs with Soufflé's semantics, plus check rules for Soufflé's runtime errors ([below](#runtime-errors)). FlowLog's `to_number` parses decimal only. Its `/` and `%` panic on a zero divisor, also on rows Soufflé never evaluates (bugs B8, B9). | `filter_order`, `antijoin_projection` |
| sqlite `.input`/`.output` | `IO` and `filename` kept, recorded in the schema manifest. An `.output` inside a component gets one entry per instance (`inst.R`). | |

The translator reports anything it does not handle as an error rather than mistranslating it. That
includes `eqrel`, `float`, ADTs, component inheritance, `autoinc`, `.limitsize`, numeric
conversions, `bnot`, `lnot` and `land`, `^` and `range` over `unsigned`, negation or disjunction
inside an aggregate, and an aggregate correlated through a computed value. It also refuses
arithmetic whose type depends on the order of Soufflé's alias resolution
([Integers](#integers)), and component clauses whose names resolve to different relations in
different instances ([Components](#components)). `tests/semantics/rejects/` has 41 such programs.
CTADL uses none of these.

Soufflé rejects an atom of an undefined relation or of the wrong arity, and so does the
translator, before any rewrite can drop the clause that holds it ([Components](#components)).
`tests/semantics/invalid/` has 7 such programs.

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
equivalent, so `policy.json` assigns each relation an encoding:

| Policy | Relations | Encoding |
|---|---|---|
| `drop` | AliasedBy, IntraObjectAliasedBy, ReachableVertex | The `choice-domain` is removed. The dependent columns are functions of the key (always `""`, or `ord` of the key and `0`), so there is one tuple per key anyway. |
| `minchoice` | VirtualAssign, ParthianVertex, ContextObj, LostAssign | `R_choice(keys.., ck: min<int64>)` with `ck = choice_keyN(prio, deps..)`. `R` is decoded with `choice_field(ck, i)`. |

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

On CTADL's tests and corpora the results match Soufflé, apart from one tie. `tests/choice_mini` and
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

FlowLog's operators panic on a zero divisor too, but FlowLog also evaluates expressions on
bindings Soufflé never reaches:

- it tests a rule's comparisons before its atoms' constants (bug B8);
- it computes a negated rule's head on the rows the negation removes (B9).

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
comparison fails. FlowLog also copies literals into the Rust crate it generates, and rustc rejects
some of the resulting code. The translator therefore rewrites integer expressions in four ways:

- **Constants** (`fold_constants`; bugs B10, B13). The translator computes constant expressions
  as Soufflé does: on 64 bits with wrap-around, in the type of the column or parameter they fill,
  else of a declared variable in them, else `number`.
  - A variable equated to a constant takes its value.
  - Comparisons of constants become true or false. So do comparisons that the limits of their
    type decide, such as `u >= 0` on `unsigned`.
  - An aggregate whose body this makes unsatisfiable is replaced by its value: 0 for `count` and
    `sum`. A `min` or `max` has no value, so its literal becomes false.

  rustc rejects literal arithmetic that overflows or divides by zero (B10), and comparisons that
  type limits decide (B13).
- **64-bit anchors** (`anchor_widths`; B11). FlowLog falls back to `int32` in three places:
  - a comparison of only literals, counts and variables bound to them;
  - the argument of `to_string`;
  - an arithmetic argument of a UDF.

  There the translator wraps a term in the identity UDF `flowlog_int64` or `flowlog_uint64`.
  FlowLog's own `as(1, int64)` leaves the literal `int32` (B11).
- **Parentheses** (`paren_safe`; B5). FlowLog writes a variable that it replaces by its arithmetic
  definition in parentheses, which rustc rejects around the argument of a UDF or of a shift. The
  translator wraps the definition in an identity UDF, which FlowLog writes as a call.
- **Literals typed by their use** (`alias_constants`, `type_context_partials`). Soufflé replaces a
  variable equated to a constant by the constant before it types the clause, so each copy takes
  the type of its own use.
  - Example: in `A(x) :- x = 3, x - 10 < 0.` with `A` unsigned, `3 - 10 < 0` compares numbers and
    holds. FlowLog types `x` once, as `unsigned`, on which the comparison is false.
  - In rules that may compute on `unsigned`, the translator substitutes such variables as Soufflé
    does. Where the result depends on the order in which Soufflé resolves the equalities, it
    refuses the rule.
  - A `/` or `%` of literals in an `unsigned` context divides unsigned.

`tests/semantics/cases/widths.dl` checks these rewrites against Soufflé. CTADL needs none of them:
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
`tests/semantics/invalid/` has 7 such programs, each with the error it expects.

The never-firing rule for programs without a derived output (B12, [Translation](#translation))
also covers outputs declared in components: it is given at top level, as `c.A(0) :- ...`.

### UDFs (`ctadl_udf.rs`)

| UDF | Purpose |
|---|---|
| `NewCallString`, `CallStringLte`, `CallStringUnder`, `CallStringNonEmpty`, `CallStringSize`, `TopFrame`, `PushFrame`, `PushFrameK`, `PopFrame` | Call strings (contexts) |
| `AccessPathSize`, `AccessPathCycle` | Access-path length and cycle detection |
| `CheckSubstring` | Substring test |
| `AndroidManifestClassId` | Android manifest class ids (unused by taint-front) |
| `choice_key1..4`, `choice_field`, `choice_number` | `minchoice` keys: intern the dependent columns, decode them (`choice_number` for a `number` column) |
| `choice_claim1..6` | In-loop witness claims |
| `souffle_strlen`, `souffle_substr`, `souffle_pow`, `souffle_ord` | Soufflé's `strlen` and `substr` (on bytes), `^` (truncated `std::pow`) and `ord` (as a `number`) |
| `souffle_to_number`, `souffle_div`, `souffle_mod`, `souffle_udiv`, `souffle_umod` | Soufflé's partial functions, made total: 0 where Soufflé stops |
| `souffle_*_error`, `souffle_raise` | The [runtime-error](#runtime-errors) checks: Soufflé's complaint, or `""`; stop the run |
| `flowlog_int64`, `flowlog_uint64` | Identities that make FlowLog compute an expression on 64 bits ([Integers](#integers)) |

The first four rows port `ctadl/src/ctadl/souffle-logic/functors.cpp`. FlowLog passes `string`
arguments as owned `String`, and `number` (FlowLog's `int64`, as CTADL builds Soufflé with
64-bit numbers) as `i64`. The choice helpers' priorities, instance and relation ids are `int32`.

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
has 64 hardware threads; CTADL uses half the CPUs. The runs used a fresh clone of this branch and
FlowLog `main` plus #477, with FlowLog's analyses compiled from scratch. An earlier round with 4
workers gave the same results. Each run's line is in `results/taint-front/summary.txt`,
`results/corpus-ctadl/summary.txt`, `results/corpus-ctadl-rs/summary.txt` and
`results/variants/*/summary.txt`.

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

Other checks:

- `tests/semantics/run.sh` tests the translation rule by rule against Soufflé, on small programs
  written for the purpose (last run in `results/semantics.txt`):
  - `cases/` (18 programs, 1,069 rows): every output relation is equal with FlowLog at 1, 4 and
    32 workers. They cover rule forms, components and their name scoping, arithmetic and integer
    widths, strings, aggregates and their scoping, records, ranges, `choice-domain`, programs
    without a derived output, and Soufflé's builtins (`pow`, `substr`, `to_number`, …),
    including their edge cases.
  - `rejects/` (41): the translator reports an error.
  - `invalid/` (7): erroneous programs. Soufflé and the translator reject each with the same
    error (undefined relation or wrong arity, at the same line).
  - `aborts/` (11): Soufflé stops on a partial call, and the port stops with the same complaint.
  - `warns/` (3): Soufflé stops on a call the checks cannot cover, and the translator's warning
    lists it.
- `tests/choice_mini/run.sh` translates, compiles and runs a recursive choice-domain program with
  1, 4 and 32 workers and compares the output with expected results and with Soufflé's
  (`results/choice_mini.txt`).
- `tests/adapter/run.sh` checks the sqlite conversion of record columns. Soufflé writes a DB in
  which they are only ids. An input no rule reads is created empty, and one a rule reads is
  refused with an explanation. Records the port writes still round-trip, a nil record id is
  refused, and without FlowLog's output DB every output is created empty (`results/adapter.txt`).
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
WORKERS="1 4 32" $P/tests/choice_mini/run.sh
WORKERS="1 4 32" $P/tests/semantics/run.sh   # or: run.sh arith div_zero ...
$P/tests/adapter/run.sh
$P/repros/check.sh -s; $P/repros/check.sh; $P/repros/check.sh -t
```

## Performance

`bench.py` timed four backends with 1 and 32 workers on two programs generated by
`gen_tnt.py --funcs N --stmts 12 --seed 1`:

- Soufflé on CTADL's program, as CTADL runs it;
- Soufflé on the same program without its `.plan` hints, in two variants
  ([Join order](#join-order));
- FlowLog.

The query wrote CTADL's text summary. The analyses were compiled beforehand, and the runs were one
at a time on the otherwise idle machine. Full results are in `results/bench/summary.txt`. Times
are in seconds:

- **wall:** the whole `ctadl` command;
- **engine:** Soufflé's compiled analysis, or FlowLog's binary;
- **conv.:** FlowLog only, the port's conversion between CTADL's sqlite layout and FlowLog's, for
  the query's inputs and results (the index's takes 0.1–1.2 s);
- **RSS:** the engine's peak memory.

Soufflé as CTADL runs it, and FlowLog:

| Program | Backend | Workers | Index wall | Index engine | Index RSS | Query wall | Query engine | Query conv. | Query RSS |
|---|---|---|---|---|---|---|---|---|---|
| syn10 | Soufflé | 1 | 2.8 | 1.1 | 34 MB | 3.4 | 1.0 | | 40 MB |
| syn10 | Soufflé | 32 | 2.3 | 0.6 | 32 MB | 3.3 | 0.8 | | 36 MB |
| syn10 | FlowLog | 1 | 4.3 | 2.6 | 248 MB | 4.4 | 1.1 | 0.3 | 146 MB |
| syn10 | FlowLog | 32 | 3.5 | 1.6 | 2.1 GB | 5.4 | 2.5 | 0.3 | 1.4 GB |
| syn25 | Soufflé | 1 | 46.6 | 44.5 | 350 MB | 90.3 | 55.5 | | 1.4 GB |
| syn25 | Soufflé | 32 | 15.0 | 12.9 | 357 MB | 71.7 | 36.8 | | 1.3 GB |
| syn25 | FlowLog | 1 | 49.2 | 45.8 | 1.7 GB | 102.7 | 48.8 | 18.5 | 2.2 GB |
| syn25 | FlowLog | 32 | 17.7 | 14.3 | 4.0 GB | 84.3 | 20.2 | 19.3 | 3.4 GB |

- **Engine time** is similar on syn25. FlowLog's index takes 3–11% longer than Soufflé's. Its query
  is 1.1× as fast with 1 worker and 1.8× with 32. Both times include writing the results to sqlite.
  On syn10, a small input, FlowLog's index takes 1.0–1.5 s longer, and its query takes 2.5 s with
  32 workers against 1.1 s with 1: there the workers cost more than they gain.
- **End to end**, FlowLog's query phase takes 1.1× Soufflé's wall time with 1 worker and 1.2× with
  32. syn25's query phase, step by step (`bench.py` logs each step's time):

  | Step (seconds) | Soufflé, 1 | FlowLog, 1 | Soufflé, 32 | FlowLog, 32 |
  |---|---|---|---|---|
  | Convert the inputs to FlowLog's layout | | 0.4 | | 0.5 |
  | Engine: load the inputs and compute | 55.5 | 37.5 | 36.8 | 10.6 |
  | Engine: write the results to sqlite | (above) | 11.3 | (above) | 9.5 |
  | Convert the results to Soufflé's layout | | 18.0 | | 18.8 |
  | CTADL indexes the results (`after_query`) | 20.5 | 21.9 | 20.3 | 25.0 |
  | CTADL writes the report | 12.9 | 11.9 | 13.1 | 18.3 |
  | Rest (startup, CTADL's setup) | 1.4 | 1.6 | 1.4 | 1.6 |
  | **Wall** | 90.3 | 102.7 | 71.7 | 84.3 |

- **Conversion:** FlowLog writes plain sqlite tables. CTADL's tools read Soufflé's layout, a
  symbol table plus views, and `fl_db_adapter.py` converts between the two inside SQLite
  ([Pipeline](#pipeline)). syn25's query writes 18.4 million rows, 18.0 million of them in
  `forward_flow.ReachableEdge`, and converting them takes 18–19 s on one thread. (The port's
  first version converted them row by row in Python, in 56 s.) With 1 worker, the conversion
  accounts for the difference: FlowLog's engine takes 6.7 s less than Soufflé's, and the
  conversion 18.5 s. With 32, FlowLog's engine takes 16.6 s less, but the conversion and CTADL's
  slower steps on its database (next item) take 29 s more. FlowLog writing Soufflé's layout
  itself would save most of the conversion ([Suggestions](#suggestions)).
- **Row order:** with 32 workers, FlowLog writes a relation's rows in another order than with 1.
  In `forward_flow.ReachableEdge`, the rows that share a value of the first column, one of the
  three columns that CTADL indexes, form 8.5 million runs for 407,832 values. Soufflé's databases
  and FlowLog's with 1 worker have one run per value. FlowLog's databases with 1 and 32 workers
  have the same contents, but CTADL's indexing and report take 9.5 s longer on the second.
- **Memory:** FlowLog's index uses 4.9× Soufflé's peak memory with 1 worker and 11× with 32. Its
  query uses 1.6× and 2.6×. FlowLog's memory grows with the worker count; Soufflé's does not.
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

The runs above do not use the same join orders. Soufflé runs CTADL's `.plan` hints, which the
translation drops: 67 in the index and 6 in the query. They are hand-written orders for the
semi-naive versions of recursive rules; Soufflé rejects `.plan` on other rules. FlowLog's `.plan`
takes one order per rule, not one per version. Without the hints, both engines join in the
written order:

- **FlowLog** has no SIPS. Its planner joins a rule's positive atoms left-deep in the written order.
  It deviates only to avoid a cross product (cyclic bodies) or to join join-tree neighbours
  (acyclic bodies). Before joining, it folds an atom whose variables all occur in another atom
  into a semijoin. Its planner trace for the index has 362 rules with two or more positive atoms:
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

`bench.py` therefore has two more backends, which run CTADL's program without its hints:

- `souffle-noplan` uses Soufflé's default SIPS;
- `souffle-strict` adds `.pragma "RamSIPS" "strict"`.

Engine times in seconds, with 1 / 32 workers:

| Program | Phase | Soufflé, CTADL's hints | Soufflé, no hints | Soufflé, strict | FlowLog |
|---|---|---|---|---|---|
| syn10 | index | 1.1 / 0.6 | 3.7 / 0.7 | 3.7 / 0.7 | 2.6 / 1.6 |
| syn10 | query | 1.0 / 0.8 | 1.0 / 0.8 | 1.0 / 0.8 | 1.1 / 2.5 |
| syn25 | index | 44.5 / 12.9 | 120.9 / 20.9 | 122.9 / 20.9 | 45.8 / 14.3 |
| syn25 | query | 55.5 / 36.8 | 54.4 / 36.3 | 54.0 / 37.1 | 48.8 / 20.2 |

- **The hints matter for the index only.** Without them, Soufflé's index takes 2.7–2.8× as long on
  syn25 with 1 worker and 1.6× with 32. The query's 6 hints make no difference, and peak memory
  does not change.
- **On the same join orders**, FlowLog's index is 2.7× as fast as Soufflé's on syn25 with 1
  worker and 1.5× with 32. Its query engine is 1.1× as fast with 1 worker and 1.8× with 32.
  On syn10, FlowLog's index is 1.4× as fast with 1 worker and 2.3× slower with 32, a fixed cost
  of its 32 workers on a small input.
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
  hints, 9–12 relations differ on syn25 and 3–4 on syn10. Compared with 1 worker, where both
  engines are deterministic:

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
python3 $P/bench.py --jobs 1,32 --backend souffle,souffle-noplan,souffle-strict,flowlog \
    bench/syn10.tnt bench/syn25.tnt > runs/bench/summary.txt
K="--keys VirtualAssign=0,1,2,3,4,5,6 --schema $P/index_schema.json --schema $P/query_schema.json"
for s in syn10 syn25; do
  B=runs/bench/$s
  python3 $P/compare_db.py $K $B/souffle-j1/ctadlir.db $B/flowlog-j1/ctadlir.db
  python3 $P/compare_db.py $K $B/souffle-j1/ctadlir.db $B/souffle-strict-j1/ctadlir.db
  python3 $P/compare_db.py $K $B/souffle-strict-j1/ctadlir.db $B/flowlog-j1/ctadlir.db
  python3 $P/compare_db.py $K $B/flowlog-j1/ctadlir.db $B/flowlog-j32/ctadlir.db
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
python3 $P/flowlog_ctadl.py --directory $D/fa index --all-outputs -j 32
python3 $P/check_choice_model.py $D/fa/index.dl $D/fa/facts $D/fa/ctadlir.db $D/model -j 32 \
    --schema $P/index_schema.json
python3 $P/compare_db.py --schema $P/index_schema.json $D/fa/ctadlir.db $B/flowlog-j1/ctadlir.db
ctadl --directory $D/fq index -j 1
python3 $P/flowlog_ctadl.py --directory $D/fq query -j 32 -o "$PWD/$D/fq-summary.txt"
python3 $P/compare_db.py $K $B/souffle-j1/ctadlir.db $D/fq/ctadlir.db
cmp $B/souffle-j1/summary.txt $D/fq-summary.txt
```

## FlowLog findings

The programs in `repros/` are minimal. `repros/check.sh` compiles and runs them with FlowLog, and
`repros/check.sh -s` runs them with Soufflé. `FLOWLOG_COMPILER` and `FLOWLOG_RUNTIME_PATH` select
another FlowLog checkout than the workspace's. The table compares the FlowLog results with Soufflé
2.5 on the same program. "`main`" is FlowLog's `main` at `84b3a1c`; "with #477" adds the B1 fix.
The outputs are in `results/repros/`: `souffle.txt`, `main.txt` and `with-477.txt`.

**Bugs**

| Repro | Soufflé 2.5 | FlowLog | Port |
|---|---|---|---|
| **B1** `string_constraint_literal`: integer literals inside a computed `contains`/`match` operand, e.g. `!contains(".*", substr(s, i, strlen(s) - i + 1))` | `O={abc}` | `main`: internal compiler error in codegen, `polymorphic literal Constant { text: "1", ty: IntLit } reached codegen; typechecker should have pinned it`. With #477: `O={abc}`. | Fixed by #477. CTADL's analysis has this shape. |
| **B2** `count_all_wild`: `n = count : { R(_, _) }` | `C={2}` | The generated crate fails rustc with E0308: `flowlog_aggregate` expects an `OrdValBatch` arrangement but gets an `OrdKeyBatch`. | The translator names the placeholders. |
| **B3** `lattice_min_string`: `min<string>` lattice column | (no lattices) | The front end accepts it, then rustc fails with E0277: `MinAccumulator<String>` is not `Clone`. | Choice keys are interned `int64`s. |
| **B4** `agg_body_binding`: an aggregate body with a constraint on an outer variable, such as a value computed from it, `n = count : { t = cat(a, "y"), S(t) }` | `O={x 1}` | rustc fails: ``unused variable: `t_…_arr` ``. The generator builds an arrangement that nothing reads, and generated crates build with `-Dwarnings`. | The translator substitutes an equality with an outer variable into the atoms. It rejects the other shapes with an error naming B4, as it does `agg_computed`, which it would rewrite into one. No CTADL aggregate has them. |
| **B5** `udf_strlen_arg`, `udf_grouped_arg`: `strlen` or `ord` as a UDF argument, `O(id(strlen(s))) :- S(s).`, or a variable that FlowLog replaces by its arithmetic definition, `O(id(y)) :- P(a), y = a + 1.` (also as a shift operand) | (no UDFs) | rustc fails: `unnecessary parentheses around function argument` (`-Dwarnings` again). | The port computes `strlen` and `ord` with UDFs (B7), and wraps such definitions in an identity UDF ([Integers](#integers)). |
| **B6** `agg_shared_join`: two aggregates over the same join, one with `_` where the other names the column. `agg_many_readers`: one aggregate over a relation that twelve other rules read. | `O={b 1, m 2}`; `W={1 … 12, 100}` | Internal compiler error in the planner's dedup step: `dedup: map sum[](k) … reads keyed k …, which only a join or a map built here may read`. | Not worked around: CTADL's aggregates have neither shape. The report gives the cause and a planner change that fixes both repros and passes FlowLog's planner tests. |
| **B7** `souffle_builtins`: `strlen("é")`, `substr("aéb", 1, 2)`, `1 ^ -1`, `(-1) ^ -1` | `2`, `é`, `1`, `-1` | `1`, `éb`, `0`, `0`: `strlen` and `substr` count characters, not bytes, and `^` is 0 for every negative exponent. | `souffle_strlen`, `souffle_substr` and `souffle_pow`. |
| **B8** `filter_order`: `O(a, b) :- R("div", a, b), a / b > 1.` with `R("skip", 1, 0)` | `O={6 2}` | Panics: `attempt to divide by zero`. A rule's filter tests its comparisons before its atoms' constants. | Total ports of `/`, `%` and `to_number`, and check rules ([Runtime errors](#runtime-errors)). |
| **B9** `antijoin_projection`: `A(100 / x) :- N(x), !Z(x).` with `N(0)`, `N(4)`, `Z(0)` | `A={25}` | Panics: `attempt to divide by zero`. An antijoin computes the head on the rows the negation removes. | As for B8. |
| **B10** `literal_overflow` (FlowLog syntax): `O(9223372036854775807 + 1) :- K(_).` | With `number`: `O={-9223372036854775808}` | rustc fails: `this arithmetic operation will overflow`. A literal division by zero fails the same way (`unconditional_panic`). | The translator computes constant expressions itself ([Integers](#integers)). |
| **B11** `cast_literal` (FlowLog syntax): `as(1, int64) bshl 31 > 0` and `as(-1, int64) bshru 1 = 2147483647` | With `number`: `O={shl}` | `O={shru}`: `as` leaves the literal `int32`. | Identity UDFs `flowlog_int64` and `flowlog_uint64` ([Integers](#integers)). |
| **B12** `empty_dataflow`: no rule, fact or input derives an output | `A={}` | rustc fails: ``unused import: `super::*` ``. FlowLog prunes every relation. | A rule that never fires ([Translation](#translation)). |
| **B13** `type_limit_cmp`: `A(u) :- U(u), u >= 0.` on `unsigned` | `A={3}` | rustc fails: `comparison is useless due to type limits`. | The translator folds such comparisons ([Integers](#integers)). |

B8's report also mentions a variable repeated in an atom, `O(x) :- R(x, x, b), x / b > 1.`.
FlowLog tests the repetition after the comparison too, but this is not a difference from Soufflé:
Soufflé 2.5 also tests such variables after the comparisons, and stops on `R(1, 2, 0)`.

Each bug is filed on flowlog-rs/flowlog, with its repro rechecked on `84b3a1c`, and has a fix PR.
None of the fixes was merged when this was written.

| Bug | Issue | Fix |
|---|---|---|
| B1 | [#464](https://github.com/flowlog-rs/flowlog/issues/464) | [#477](https://github.com/flowlog-rs/flowlog/pull/477) |
| B2 | [#465](https://github.com/flowlog-rs/flowlog/issues/465) | [#478](https://github.com/flowlog-rs/flowlog/pull/478) |
| B3 | [#466](https://github.com/flowlog-rs/flowlog/issues/466) | [#485](https://github.com/flowlog-rs/flowlog/pull/485) |
| B4 | [#467](https://github.com/flowlog-rs/flowlog/issues/467) | [#488](https://github.com/flowlog-rs/flowlog/pull/488), on top of #479 |
| B5 | [#468](https://github.com/flowlog-rs/flowlog/issues/468) | [#486](https://github.com/flowlog-rs/flowlog/pull/486) |
| B6 | [#469](https://github.com/flowlog-rs/flowlog/issues/469) | [#479](https://github.com/flowlog-rs/flowlog/pull/479) |
| B7 | [#470](https://github.com/flowlog-rs/flowlog/issues/470), for `^` only | [#480](https://github.com/flowlog-rs/flowlog/pull/480); [#489](https://github.com/flowlog-rs/flowlog/pull/489) also makes `^` exact for an exponent past `u32` |
| B8 | [#471](https://github.com/flowlog-rs/flowlog/issues/471) | [#481](https://github.com/flowlog-rs/flowlog/pull/481) |
| B9 | [#472](https://github.com/flowlog-rs/flowlog/issues/472) | [#482](https://github.com/flowlog-rs/flowlog/pull/482) |
| B10 | [#473](https://github.com/flowlog-rs/flowlog/issues/473) | [#483](https://github.com/flowlog-rs/flowlog/pull/483) |
| B11 | [#474](https://github.com/flowlog-rs/flowlog/issues/474) | [#487](https://github.com/flowlog-rs/flowlog/pull/487); [#492](https://github.com/flowlog-rs/flowlog/pull/492) does the same for an `as` whose operand is not a literal ([#490](https://github.com/flowlog-rs/flowlog/issues/490)) |
| B12 | [#475](https://github.com/flowlog-rs/flowlog/issues/475) | [#484](https://github.com/flowlog-rs/flowlog/pull/484) |
| B13 | [#476](https://github.com/flowlog-rs/flowlog/issues/476) | [#483](https://github.com/flowlog-rs/flowlog/pull/483) |

B7's `strlen` and `substr`, which count characters where Soufflé counts bytes, are not filed.

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
| `agg_computed` | `O={x 1}` | syntax error: expected a constant, variable or `_` argument | rejected: the rewrite would hit B4 |
| `agg_computed_lifted` | `O={x 1, z 0}` | ``assignment-bound variable `t` cannot be used in an atom with a computed value`` | not supported (CTADL does not need it) |
| `neg_computed` | `O={}` | syntax error: expected constant | `NegPre` helper relation |
| `neg_computed_lifted` | `O={z}` | same as `agg_computed_lifted` | `NegPre` helper relation |
| `computed_fact` | `C={ab}` | rule body reduces to nothing but its head is not a constant fact | `FlowlogUnit` anchor |
| `computed_body_only` | `C={ab}` | same as `computed_fact` | `FlowlogUnit` anchor |
| `true_false` | | expected `arithmetic_op` or `compare_op` | simplification |
| `keyword_fn` | | expected variable | renaming |
| `pragma` | | syntax error | dropped |
| `agg_min_string` | rejected too | ``aggregation `Min` requires a numeric input`` | int64 choice keys |

Where the Soufflé column is empty, both engines compute the obvious result once the program is
accepted.

`repros/check.sh -t` passes each repro through the translator, then compiles and runs it the way the
driver does, with FlowLog `main` plus #477 (`results/repros/translated.txt`):

- **Nineteen** rewritten repros give exactly Soufflé's result, among them B2, B7–B9, B12 and B13,
  and B1 thanks to #477. `antijoin_projection` also writes the check relation
  `FlowlogAbort`, empty.
- **Other results:** `functor_decl` computes `O={x}`. `choice_domain` keeps `C(1, 3)` where
  Soufflé keeps `C(1, 2)`. Both are valid choices: the two candidates tie in the same clause and
  round ([Limitations](#limitations)).
- **Unsupported:** five repros, none of which CTADL needs. The translator rejects
  `agg_computed`, `agg_body_binding` (B4) and `agg_computed_lifted`. FlowLog fails on
  `agg_shared_join` and `agg_many_readers` (B6).
- **The rest:** both engines reject `agg_min_string`, and the driver handles `empty_output`.
  `cast_literal`, `literal_overflow`, `udf_grouped_arg`, `udf_strlen_arg` and the `lattice_*`
  repros are FlowLog programs. The semantics suite's `widths.dl` tests the rewrites for B5, B10,
  B11 and B13 against Soufflé.

**Behaviour differences**

- **`empty_output`:** FlowLog writes no table for an output relation that has no rules. Soufflé
  creates an empty one. The adapter creates the missing tables.
- **`lattice_in_loop`:** a `min` lattice relation that is read inside its own recursion also
  exposes superseded rows to the loop. The minimal `Dist` is right, but `Via` also holds node 2's
  first distance (`2 5`). This is what makes `choice-domain` inside recursion hard.

<a id="suggestions"></a>
### Suggestions for FlowLog, by value to a CTADL port

1. **Native `choice-domain`**, or an aggregate that picks one witness per key and never revises it
   inside recursion. It is the only non-syntactic gap, and it would replace the stateful UDFs.
2. **Merge the fixes** for B1 (#477; CTADL needs it) and B2–B13. B8 and B9 make FlowLog
   evaluate expressions on rows that the rule excludes. A port must then make every function
   total, and stop the run itself where Soufflé would ([Runtime errors](#runtime-errors)). B4,
   B5, B10, B12 and B13 are rustc lints on the generated code, which the generated crate could
   allow.
3. **64-bit literals:** a literal that no column or parameter types is an `int32`, and `as` does
   not change it (B11). An option to default to `int64`, or an `as` that types the literal (#487),
   would make the translator's anchors unnecessary ([Integers](#integers)).
4. **Soufflé-compatible syntax**: nested components; `inline`; records (flattening would be enough);
   `range`; variadic `cat`; expression arguments of atoms; negation and aggregation over computed
   values; computed facts and bodies; `true`/`false`; ignoring unknown `.pragma`s;
   `.functor`/`@F` mapping to `.extern fn`.
5. **Output parity:** create empty tables/files for output relations that have no rules. Writing
   Soufflé's sqlite layout (a symbol table, shadow tables of ids, and views) as an option would
   remove most of the port's conversion. On syn25 with 32 workers, FlowLog computes the query's
   results in 10.6 s, then takes 9.5 s to write them as plain tables, and the port 18.8 s to
   convert them. With several workers, FlowLog should also write each relation in the order it
   uses with 1 worker: CTADL's indexing and report take 9.5 s longer on the 32-worker database
   ([Performance](#performance)).
6. **Compile time and binary size.** CTADL's index and query take 3 min 8 s and 2 min 3 s to
   compile, against Soufflé's 2 min 43 s and 1 min 10 s. The binaries are 19–28× larger (168 and
   116 MB). Both engines cache the binary, so this cost is paid once per version of the analysis,
   but it slows down work on the analysis itself.

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
  - The `choice_domain` repro and `tests/choice_mini` document such ties.
- **In-recursion choice** relies on evaluation order ([above](#choice-domain)).
- **Runtime errors:** where Soufflé stops on a partial call that the translator cannot check, the
  port completes, with the call's result 0. The translator lists these calls in a warning
  ([Runtime errors](#runtime-errors)). In CTADL there are three, and they cannot fail.
- **Translator coverage:** only the Soufflé subset that CTADL uses is translated. Everything else is
  rejected with an error, not mistranslated. The choice policies in `policy.json` are written for
  CTADL's relations; a new `choice-domain` relation needs a policy (the translator refuses
  otherwise). The translator also refuses some valid Soufflé programs that it could not translate
  faithfully:
  - arithmetic whose type depends on the order of Soufflé's alias resolution (`alias_order`,
    `alias_several`);
  - a variable name that Soufflé types differently in two disjuncts or sibling aggregates of a
    rule (`disjunct_types`, `agg_local_types`), since the translator types a rule's names once;
  - `^` and `range` over `unsigned`, and `min`/`max` of a target that reads no local variable;
  - component names that denote different relations in different instances, or an enclosing
    instance's `choice-domain` or type-parameter relation; components with type parameters and
    nested components ([Components](#components));
  - aggregate shapes FlowLog cannot compile (B4).
- **B6:** FlowLog's planner fails on some aggregates over an arrangement that other rules also
  use. The translator does not avoid this; CTADL's aggregates do not trigger it.
- **Front ends:** only the taint-front front end was exercised. Ghidra (P-code) and JADX (Android)
  imports use the same analysis programs but were not run.

<a id="disk-usage"></a>
## Disk usage

- **Compiled analyses:** each takes about 100–190 MB in `~/.cache/ctadl-flowlog`. Delete the
  entries you no longer need; any change to the translator, policy, UDFs or compiler produces new
  keys.
- **Shared target dir:** the cargo target dir (`FLOWLOG_CARGO_TARGET`) keeps the compiled FlowLog
  runtime and dependencies, about 0.8 GB.
- **Incremental compilation:** FlowLog enables rustc incremental compilation when given a build dir
  (`-B`). Each analysis gets its own content-addressed build dir and never reuses it, which would
  leave about 1 GB per program. The driver therefore sets `CARGO_INCREMENTAL=0`.
- **Run databases:** the runners' `--prune` deletes the databases of identical runs.

## Versions

| Component | Version |
|---|---|
| FlowLog | `main` at `84b3a1c` (fix(deps): update rust crate rusqlite to 0.41 #461) + PR #477 (`fed6293`, the B1 fix); also checked on `main` at `bad0ff2` (#493) + #477 |
| CTADL | 0.14.1, `4f6f8cb` |
| ctadl-rs | `7c98b73e` |
| Soufflé | 2.5 |
| g++ | 11.4.0 (compiles Soufflé's analyses) |
| Rust | 1.95.0 |
| Python | 3.10.12 |

Machine: 64 hardware threads (32 cores of an AMD EPYC 7763, 2 threads per core), 503 GB RAM,
Ubuntu 22.04. The runs use 32 workers, CTADL's default on it.

# Soufflé and FlowLog on CTADL's workloads, 16 workers

The 29 workloads of `benchmark/run.py --suite all`, run with both engines on 2026-11-07, 07:05–16:59 UTC,
on ctadl-flowlog `8e2887d` and FlowLog `main` at `6d9a2b8` (differential-dataflow 0.26.0, timely 0.32.0).
They supersede the results of [#7](https://github.com/flowlog-rs/ctadl-flowlog/pull/7), measured with 32
workers on FlowLog `03b534b` and `49d9999`, which stay under the tag
[`archive/flowlog-backend-bench`](https://github.com/flowlog-rs/ctadl-flowlog/tree/archive/flowlog-backend-bench/flowlog-backend/results).

## Setup

| | |
|---|---|
| Workloads | 2 taint-front programs (syn10, syn25); 14 Ubuntu 22.04 executables through Ghidra 11.2 (P-code); 13 F-Droid apps through JADX (plugin 0.6.2, JDK 21.0.13); inputs as in [`../workloads.json`](../workloads.json) |
| Soufflé | 2.5, compiled analyses, `-j 16` |
| FlowLog | compiled analyses, 16 workers, string interning (`--str-intern`, which `ctadl.flowlog` always passes to `flowlog-compiler`), SQLite I/O `native-shared-dictionary-v1`, rustc 1.97.1 |
| Runs | `bash benchmark/run.sh --suite all --workloads W --workers 16 --backends souffle,flowlog`, one workload at a time: for each engine, index then query, 1 warm-up and 3 measured runs, the engines' order swapped every other run |
| Machine | AMD EPYC 7V13, 24 cores, 1 NUMA node, 216 GiB, Linux 6.8; a VM with light background load |

Times are the medians of the 3 measured runs; 13 of the 348 measured runs are more than 5% off their median,
none more than 12%. `wall` is the whole `ctadl index` or `ctadl query` command; `engine` is the analysis
binary, with its SQLite reads and writes. Peak RSS is the process tree's (GNU time), the larger of index and
query. x is Soufflé's time over FlowLog's: above 1, FlowLog is faster. Both engines compile each analysis once
and cache it, so these times exclude compiling.

## Suites

Geometric means of the workloads' ratios; the totals add up the medians of index and query.

| Suite | Workloads | Index wall x | Query wall x | Index engine x | Query engine x | Total wall, Soufflé / FlowLog (s) | x | Peak RSS, FlowLog / Soufflé |
|---|---|---|---|---|---|---|---|---|
| Synthetic | 2 | 1.58 | 1.15 | 1.63 | 1.99 | 93 / 68 | 1.37 | 7.2 |
| P-code | 14 | 2.69 | 2.69 | 3.94 | 8.49 | 2,630 / 855 | 3.08 | 4.1 |
| JADX | 13 | 1.84 | 1.67 | 2.49 | 2.61 | 1,826 / 887 | 2.06 | 4.1 |
| All | 29 | 2.19 | 2.05 | 3.02 | 4.53 | 4,549 / 1,809 | 2.51 | 4.3 |

- FlowLog is faster in every phase of every workload but the index of the three smallest: syn10 (0.82x),
  Diary (0.99x) and Editor (1.00x).
- It takes 4.3x Soufflé's peak memory (2.2x to 17.1x), at most 11.8 GiB, on sqlite3, against Soufflé's
  3.6 GiB. Its floor with 16 workers is about 1 GiB
  ([flowlog-rs/flowlog#521](https://github.com/flowlog-rs/flowlog/issues/521)).

## Workloads

Each suite is ordered by Soufflé's index time. Relations: equal / different, of the databases compared
relation by relation; equal includes those equal up to choice-domain witnesses. SARIF: equal, or the same
results with other code flows or statistics; the taint-front workloads have no SARIF step.

| Suite | Workload | Index wall, S / F (s) | x | Query wall, S / F (s) | x | Engine x, index / query | Peak RSS, S / F (GiB) | Relations, equal / different | SARIF |
|---|---|---|---|---|---|---|---|---|---|
| Synthetic | syn10 | 2.1 / 2.5 | 0.82 | 2.5 / 2.4 | 1.06 | 0.63 / 1.44 | 0.1 / 1.0 | 117 / 3 | - |
| Synthetic | syn25 | 16.8 / 5.5 | 3.04 | 71.3 / 57.3 | 1.24 | 4.17 / 2.76 | 1.3 / 4.0 | 108 / 12 | - |
| P-code | libz | 11.4 / 6.9 | 1.66 | 5.7 / 3.2 | 1.78 | 2.29 / 4.70 | 0.2 / 1.6 | 210 / 0 | equal |
| P-code | nc | 16.2 / 8.6 | 1.89 | 8.2 / 4.0 | 2.07 | 2.54 / 6.03 | 0.2 / 1.6 | 209 / 1 | same results |
| P-code | sed | 21.5 / 9.2 | 2.34 | 8.2 / 4.4 | 1.85 | 3.56 / 5.85 | 0.2 / 1.6 | 210 / 0 | equal |
| P-code | gzip | 25.2 / 10.2 | 2.47 | 9.2 / 4.1 | 2.23 | 3.78 / 7.50 | 0.3 / 1.6 | 210 / 0 | equal |
| P-code | dropbear | 44.6 / 15.3 | 2.92 | 14.1 / 5.8 | 2.45 | 4.53 / 8.08 | 0.4 / 1.9 | 210 / 0 | equal |
| P-code | sudo | 55.9 / 19.7 | 2.83 | 20.0 / 8.1 | 2.47 | 4.29 / 8.38 | 0.6 / 2.3 | 210 / 0 | equal |
| P-code | lighttpd | 58.4 / 21.7 | 2.69 | 15.7 / 8.6 | 1.81 | 4.18 / 5.45 | 0.6 / 2.3 | 210 / 0 | equal |
| P-code | dnsmasq | 137.1 / 47.0 | 2.92 | 57.6 / 16.7 | 3.46 | 4.23 / 9.67 | 1.4 / 5.3 | 209 / 1 | equal |
| P-code | wget | 151.9 / 48.5 | 3.13 | 51.5 / 17.3 | 2.97 | 4.78 / 11.27 | 1.4 / 3.6 | 206 / 4 | same results |
| P-code | tar | 168.3 / 54.2 | 3.11 | 61.0 / 19.4 | 3.14 | 4.53 / 10.82 | 1.6 / 4.7 | 209 / 1 | same results |
| P-code | openssl | 246.6 / 76.0 | 3.24 | 78.3 / 20.9 | 3.76 | 4.79 / 13.20 | 2.0 / 5.0 | 210 / 0 | equal |
| P-code | ssh | 287.6 / 98.2 | 2.93 | 146.2 / 29.9 | 4.89 | 3.89 / 13.03 | 2.7 / 9.6 | 210 / 0 | equal |
| P-code | bash | 337.2 / 104.7 | 3.22 | 108.3 / 36.1 | 3.00 | 4.67 / 11.60 | 2.9 / 6.3 | 210 / 0 | equal |
| P-code | sqlite3 | 353.4 / 118.2 | 2.99 | 130.7 / 38.1 | 3.43 | 4.23 / 9.36 | 3.6 / 11.8 | 209 / 1 | same results |
| JADX | diary | 4.2 / 4.2 | 0.99 | 2.6 / 2.4 | 1.08 | 0.98 / 1.29 | 0.1 / 1.2 | 160 / 6 | same results |
| JADX | editor | 4.3 / 4.3 | 1.00 | 2.6 / 2.4 | 1.08 | 1.09 / 1.49 | 0.1 / 1.3 | 156 / 10 | same results |
| JADX | netguard | 24.8 / 13.4 | 1.86 | 7.7 / 5.4 | 1.43 | 2.53 / 2.15 | 0.3 / 1.8 | 156 / 10 | same results |
| JADX | vanilla | 35.7 / 18.2 | 1.96 | 9.9 / 6.8 | 1.47 | 2.58 / 2.27 | 0.5 / 2.4 | 159 / 7 | same results |
| JADX | binaryeye | 46.4 / 23.0 | 2.02 | 13.7 / 8.7 | 1.58 | 2.79 / 2.65 | 0.8 / 3.1 | 154 / 12 | same results |
| JADX | opencamera | 69.8 / 36.8 | 1.90 | 18.2 / 11.4 | 1.59 | 2.74 / 2.44 | 1.0 / 2.9 | 159 / 7 | same results |
| JADX | notes | 117.4 / 59.2 | 1.98 | 32.6 / 16.9 | 1.93 | 2.73 / 2.71 | 1.6 / 4.9 | 154 / 12 | same results |
| JADX | connectbot | 130.5 / 59.6 | 2.19 | 35.2 / 17.8 | 1.98 | 3.22 / 3.53 | 1.9 / 5.8 | 154 / 12 | same results |
| JADX | antennapod | 141.4 / 68.5 | 2.06 | 34.6 / 18.5 | 1.87 | 2.88 / 2.86 | 1.9 / 7.7 | 154 / 12 | same results |
| JADX | amaze | 141.6 / 64.3 | 2.20 | 38.0 / 18.7 | 2.04 | 3.08 / 3.32 | 2.2 / 6.6 | 153 / 13 | same results |
| JADX | newpipe | 195.8 / 89.3 | 2.19 | 52.2 / 23.9 | 2.18 | 3.20 / 3.60 | 2.8 / 7.4 | 152 / 14 | same results |
| JADX | k9 | 227.0 / 102.6 | 2.21 | 64.1 / 32.2 | 1.99 | 3.35 / 3.57 | 3.2 / 8.2 | 153 / 13 | same results |
| JADX | wikipedia | 286.4 / 132.8 | 2.16 | 89.6 / 45.3 | 1.98 | 3.25 / 3.70 | 4.0 / 10.2 | 153 / 13 | same results |

## Correctness

- As in #7, the engines differ only in `choice-domain` relations, where each keeps another valid witness
  (`VirtualAssign`, `LostAssign`, `ContextObj`), and in what CTADL derives from the witnesses: call edges,
  reachable edges, statistics. sqlite3's one difference, for instance, is `CTADLStats`' `ctx`, the number of
  contexts among `ContextObj`'s witnesses: 28 with Soufflé, 27 with FlowLog.
- The numbers of equal and different relations are #7's on 28 of the 29 workloads. On dnsmasq, `LostAssign`
  is `choice-domain (insn)` and takes `insn` from `VirtualAssign`'s witnesses; one of these differs, and with
  it one `LostAssign` key on each side, where #7's keys were the same.
- The SARIF reports have the same results on all 27 realistic workloads, and are equal on 10 of the
  executables. In #7's runs, NewPipe's and Wikipedia's results differed; CTADL now orders the paths by the
  vertices' contents ([#3](https://github.com/flowlog-rs/ctadl-flowlog/pull/3), in `main` since
  [#8](https://github.com/flowlog-rs/ctadl-flowlog/pull/8)).
- FlowLog's index is a choice model of CTADL's index analysis on Diary, Editor, NetGuard and Binary Eye:
  Soufflé, with each choice-domain relation restricted to FlowLog's witnesses, derives FlowLog's database,
  164 relations of 164 (`choice-model.txt`).

## Files

| File | |
|---|---|
| `measurements.csv` | all 464 runs (29 workloads, 2 engines, index and query, 1 warm-up and 3 measured runs each), with the fields of `run.py`'s `measurements.csv` (see [`../README.md`](../README.md)) but the log paths |
| `comparisons.txt` | the database and SARIF comparisons of the warm-up runs, without the equal relations, with 2 example rows per side of a difference |
| `choice-model.txt` | the choice-model checks of the 4 apps |

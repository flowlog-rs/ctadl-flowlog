# Unreleased

- Added FlowLog as an alternative engine for the analyses: `ctadl index` and
  `ctadl query` take `--engine flowlog` (or `CTADL_ENGINE=flowlog`). The
  FlowLog engine (`ctadl.flowlog`) translates the Souffle analyses to FlowLog,
  compiles them with `flowlog-compiler` and caches them like the Souffle
  binaries. Both engines use CTADL's index database.
- Added optional `--timings` logs to normal analysis commands and reproducible
  benchmark scripts under `benchmark/`, including pinned workload inputs.
- Use FlowLog's native shared SQLite dictionary for ordinary analyses and
  benchmarks, removing the intermediate database converter. Restore public
  column names, SQL indexes and empty outputs after native writes; retain
  flattened record storage with record IDs exposed through public views.
  Timing and benchmark records identify the SQLite I/O mode.
- Stabilized SARIF path traversal, retained local entry points for P-code
  functions, and reduced SQLite overhead in model writes and index reads.
- Included local FlowLog runtime sources in analysis cache invalidation and
  fixed qualified output coverage in choice-model verification.

# 0.14.1

- Exit before plugin run if artifact is not found.

# 0.14.0

- Raised the minimum `ctadl-ghidra-fact-generator-plugin` version to `0.1.0`
  so pip installs use the Ghidra integration that invokes the local Ghidra
  headless launcher instead of a packaged script with environment-specific
  shebangs.

# v0.11.2

- Improved all documentation and hosted on readthedocs.

# v0.11.1

- Exported relations for JADX queries.
- Fixed python 3.10 compatibility.

# v0.11.0

- First release!

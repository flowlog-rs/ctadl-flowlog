# Unreleased

- Added FlowLog as an alternative engine for the analyses: `ctadl index` and
  `ctadl query` take `--engine flowlog` (or `CTADL_ENGINE=flowlog`). The
  FlowLog engine (`ctadl.flowlog`) translates the Souffle analyses to FlowLog,
  compiles them with `flowlog-compiler` and caches them like the Souffle
  binaries. The index database has the same layout with either engine.

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

#!/usr/bin/env python3
"""Validates SARIF files against SARIF 2.1.0's JSON schema. CTADL's
tests/bin/test makes this check with checksarif (Microsoft's SARIF multitool):
it fails on the multitool's JSON* results, the schema violations.
  check_sarif_schema.py SCHEMA SARIF...
SCHEMA is Schemata/sarif-schema-2.1.0.json of oasis-tcs/sarif-spec, at the
revision that CTADL's nix/sarif-multitool/checksarif.nix pins (53296fa). Needs
jsonschema. Prints a line per file, with its number of results and, if it is
invalid, its number of violations and the first; then a tally. Then it pairs
each report of a run with FlowLog (a directory flowlog, flowlogN,
souffle-flowlog or flowlog-souffle in its path) with the same run's report
with Soufflé alone (souffle or souffle-souffle there), prints the pairs that
differ in their number of results or in the violations of a result (results
in any order), and a tally. The exit status is 1 if a file is invalid or a
pair differs."""

import json
import re
import sys
from collections import Counter

from jsonschema.validators import validator_for

ENGINE = re.compile(r"(souffle|flowlog)\d*(-(souffle|flowlog)\d*)?")


def reference(path):
    """The path of the report with Soufflé alone, of the run of path's report."""
    parts = path.split("/")
    for i, part in enumerate(parts):
        m = ENGINE.fullmatch(part)
        if m:
            parts[i] = "souffle-souffle" if m[2] else "souffle"
    return "/".join(parts)


def violations(errors):
    """Each result's violations, as places in the result, and the others."""
    results, other = {}, []
    for e in errors:
        p = list(e.absolute_path)
        if len(p) >= 4 and p[0] == "runs" and p[2] == "results":
            results.setdefault((p[1], p[3]), []).append((p[4:], e.validator))
        else:
            other.append((p, e.validator))
    return Counter(json.dumps(sorted(v)) for v in results.values()), json.dumps(
        sorted(other)
    )


def main(argv):
    if len(argv) < 3:
        sys.exit(__doc__)
    with open(argv[1]) as f:
        schema = json.load(f)
    cls = validator_for(schema)
    cls.check_schema(schema)
    validator = cls(schema, format_checker=cls.FORMAT_CHECKER)
    invalid = 0
    seen = {}
    for path in argv[2:]:
        with open(path) as f:
            log = json.load(f)
        errors = list(validator.iter_errors(log))
        results = sum(len(run.get("results") or []) for run in log.get("runs") or [])
        seen[path] = results, violations(errors)
        if errors:
            invalid += 1
            first = min(errors, key=lambda e: list(e.absolute_path))
            where = "/".join(map(str, first.absolute_path))
            print(
                f"INVALID  {path} ({results} results): {len(errors)} violations, "
                f"the first at /{where}: {first.message[:160]}"
            )
        else:
            print(f"valid    {path} ({results} results)")
    print(f"{len(argv) - 2 - invalid} valid, {invalid} invalid")
    pairs, different, unpaired = 0, 0, []
    for path in seen:
        ref = reference(path)
        if ref == path:
            continue
        if ref not in seen:
            unpaired.append(path)
            continue
        pairs += 1
        if seen[path] != seen[ref]:
            different += 1
            print(f"DIFFERENT {path} and {ref}")
    if pairs or unpaired:
        print(
            f"pairs of reports, with FlowLog and with Soufflé alone: {pairs}, "
            f"{pairs - different} the same, {different} different; "
            f"FlowLog's without Soufflé's: {len(unpaired)} {' '.join(unpaired)}".rstrip()
        )
    return 1 if invalid or different or unpaired else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

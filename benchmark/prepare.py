#!/usr/bin/env python3
"""Download or generate pinned workload inputs, without running analyses."""

import argparse
import os
import sys
import time
from pathlib import Path

from inputs import prepare_input, select

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--suite",
    default="synthetic",
    choices=["smoke", "synthetic", "pcode", "jadx", "realistic", "all"],
)
parser.add_argument("--workloads", help="comma-separated IDs from workloads.json")
args = parser.parse_args()
try:
    logs = (
        Path(__file__).resolve().parent
        / "runs"
        / f"prepare-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{os.getpid()}"
    )
    for row in select(args.suite, args.workloads):
        print(prepare_input(row, os.environ.copy(), logs / row["id"], False))
except (RuntimeError, ValueError, OSError) as exc:
    print(f"ERROR: {exc}", file=sys.stderr)
    sys.exit(1)

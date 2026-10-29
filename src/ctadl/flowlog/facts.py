#!/usr/bin/env python3
"""Prepare a Souffle fact directory for a FlowLog program emitted by
souffle2flowlog.py.

* Every file-based `.input` relation of the FlowLog program gets a file in
  the output directory (empty when the Souffle fact directory has none,
  matching Souffle, which treats a missing fact file as empty); files that
  need no conversion are symlinked. Absolute file names are rejected: they
  do not name a file in the fact directory.
* Record-typed columns (listed in the translator's schema manifest) are
  flattened: a field `[0,foo]` becomes the two fields `0<TAB>foo`.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

IO_RE = re.compile(r"^\s*\.(input|output)\s+([A-Za-z_][A-Za-z0-9_.]*)\s*(?:\((.*)\))?\s*$")
PARAM_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')
DEFAULT_SUFFIX = {"input": ".facts", "output": ".csv"}


def file_io(program: Path, kind: str = "input") -> dict[str, str]:
    """Maps each file-based `.input` (or, with kind "output", `.output`)
    relation to its file name."""
    files: dict[str, str] = {}
    for line in program.read_text().splitlines():
        m = IO_RE.match(line)
        if not m or m.group(1) != kind:
            continue
        rel, params = m.group(2), dict(PARAM_RE.findall(m.group(3) or ""))
        if params.get("IO", "file").lower() != "file":
            continue
        files[rel] = params.get("filename", rel + DEFAULT_SUFFIX[kind])
    return files


def split_record(text: str) -> list[str]:
    """Splits a flat Souffle record literal `[a, "b", c]` into its fields."""
    text = text.strip()
    if not (text.startswith("[") and text.endswith("]")):
        raise ValueError(f"not a record literal: {text!r}")
    body = text[1:-1]
    fields, cur, quoted, i = [], [], False, 0
    while i < len(body):
        c = body[i]
        if quoted:
            if c == "\\" and i + 1 < len(body):
                cur.append(body[i + 1])
                i += 2
                continue
            if c == '"':
                quoted = False
            else:
                cur.append(c)
        elif c == '"':
            quoted = True
        elif c == "[":
            raise ValueError(f"nested records are not supported: {text!r}")
        elif c == ",":
            fields.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
        i += 1
    fields.append("".join(cur).strip())
    return fields


def convert_line(line: str, layout: list) -> str:
    cols = line.split("\t")
    if len(cols) != len(layout):
        raise ValueError(f"expected {len(layout)} columns, got {len(cols)}: {line!r}")
    out: list[str] = []
    for col, (_attr, width) in zip(cols, layout):
        if width == 1:
            out.append(col)
            continue
        fields = split_record(col)
        if len(fields) != width:
            raise ValueError(f"record {col!r} has {len(fields)} fields, expected {width}")
        out.extend(fields)
    return "\t".join(out)


def convert(program: Path, src_dir: Path, dst_dir: Path, schema: Path | None = None) -> None:
    """Fill dst_dir with one fact file per file-based `.input` of program.
    Files without record columns are symlinked; raises ValueError on
    malformed records."""
    records = json.loads(schema.read_text()).get("records", {}) if schema else {}
    inputs = file_io(program)
    dst_dir.mkdir(parents=True, exist_ok=True)
    for rel, fname in sorted(inputs.items()):
        if Path(fname).is_absolute():
            raise ValueError(f"{rel}: absolute fact file names are not supported: '{fname}'")
        src, dst = src_dir / fname, dst_dir / fname
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() or dst.is_symlink():
            dst.unlink()
        if not src.exists():
            dst.write_text("")
            continue
        layout = records.get(rel)
        if layout is None:
            try:
                dst.symlink_to(src.resolve())
            except OSError:
                shutil.copyfile(src, dst)
            continue
        with src.open() as fin, dst.open("w") as fout:
            for n, line in enumerate(fin, 1):
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    fout.write(convert_line(line, layout) + "\n")
                except ValueError as e:
                    raise ValueError(f"{src}:{n}: {e}") from None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("program", type=Path, help="FlowLog program (.fl/.dl)")
    ap.add_argument("src", type=Path, help="Souffle fact directory")
    ap.add_argument("dst", type=Path, help="output fact directory")
    ap.add_argument("--schema", type=Path, help="schema manifest written by souffle2flowlog.py")
    args = ap.parse_args()
    try:
        convert(args.program, args.src, args.dst, args.schema)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Prepare a Souffle fact directory for a FlowLog program emitted by
souffle2flowlog.py, and place the program's output files as Souffle does.
Both follow the "files" entries of the translator's schema manifest, which
list every file-based `.input` and `.output`, in each component instance,
with its file name and columns.

* convert: every input gets its file in FlowLog's input directory,
  symlinked, or converted where it has record columns, which the
  translation flattens (a record `[0, "a b"]` is read as Souffle reads it,
  into the two fields `0<TAB>a b`). An input that no rule reads is created
  empty, as Souffle does not load it. A missing file is left out: like
  Souffle, FlowLog fails if it loads the relation.
* place_outputs: after the run, every output gets its file where Souffle
  writes it, also one FlowLog prunes, with its record columns written as
  Souffle writes them (`[0, a b]`), and a nullary fact as Souffle writes it
  (`()`, where FlowLog writes `True`).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

NUMBER = re.compile(r"[+-]?[0-9]+")
QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"', re.S)
ESCAPE = re.compile(r"\\(.)", re.S)


def read_record(text: str, fmt: str, kinds: list) -> list[str]:
    """Reads a record literal as Souffle's ReadStream::readRecord does, given
    its format and its fields' kinds from the manifest; returns the fields.
    A symbol is either double-quoted, with backslash escapes, or the text up
    to the next `,` or `]`, as is."""
    fields: list[str] = []
    kinds_left = iter(kinds)
    pos = 0
    for token in re.findall(r"\[|\]|,|\{\}", fmt):
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if token != "{}":
            if not text.startswith(token, pos):
                raise ValueError(f"expected '{token}' at offset {pos} of record {text!r}")
            pos += 1
        elif next(kinds_left) != "symbol":
            m = NUMBER.match(text, pos)
            if not m:
                raise ValueError(f"expected a number at offset {pos} of record {text!r}")
            fields.append(m.group())
            pos = m.end()
        elif text.startswith('"', pos):
            m = QUOTED.match(text, pos)
            if not m:
                raise ValueError(f"unterminated symbol in record {text!r}")
            fields.append(ESCAPE.sub(r"\1", m.group(1)))
            pos = m.end()
        else:
            end = min((i for i in (text.find(",", pos), text.find("]", pos)) if i >= 0), default=-1)
            if end < 0:
                raise ValueError(f"unterminated record {text!r}")
            fields.append(text[pos:end])
            pos = end
    if pos != len(text):
        raise ValueError(f"unexpected text after record {text!r}")
    return fields


def flat_line(line: str, columns: list) -> str:
    """A Souffle fact line, with the record columns flattened. The carriage
    return of a CRLF line end is dropped, as Souffle and FlowLog drop it."""
    if line.endswith("\r"):
        line = line[:-1]
    texts = line.split("\t")
    if len(texts) != len(columns):
        raise ValueError(f"expected {len(columns)} columns, got {len(texts)}: {line!r}")
    out: list[str] = []
    for text, col in zip(texts, columns):
        out.extend(read_record(text, col["format"], col["fl_kinds"]) if col["kind"] == "record" else [text])
    return "\t".join(out)


def souffle_line(line: str, columns: list) -> str:
    """A line FlowLog wrote, with the record columns flattened, as Souffle
    writes it."""
    fields = line.split("\t")
    width = sum(len(col["fl"]) for col in columns)
    if len(fields) != width:
        raise ValueError(f"expected {width} fields, got {len(fields)}: {line!r}")
    out: list[str] = []
    for col in columns:
        n = len(col["fl"])
        out.append(col["format"].format(*fields[:n]) if col["kind"] == "record" else fields[0])
        fields = fields[n:]
    return "\t".join(out)


def rewrite(src: Path, dst: Path, convert_line, columns: list) -> None:
    """Converts src to dst line by line. As Souffle and FlowLog read a file,
    lines end at a newline only, and the bytes pass through as they are."""
    opts = {"encoding": "utf-8", "errors": "surrogateescape", "newline": "\n"}
    with src.open(**opts) as fin, dst.open("w", **opts) as fout:
        for n, line in enumerate(fin, 1):
            try:
                fout.write(convert_line(line.rstrip("\n"), columns) + "\n")
            except ValueError as e:
                raise ValueError(f"{src}:{n}: {e}") from None


def has_records(columns: list) -> bool:
    return any(col["kind"] == "record" for col in columns)


def convert(inputs: list, src_dir: Path, dst_dir: Path) -> None:
    """Fills dst_dir with the files of `inputs` (the manifest's file inputs)
    that the Souffle fact directory src_dir has. Raises ValueError on an
    absolute file name (it does not name a file in the fact directory) or a
    malformed record."""
    for rel in inputs:
        fname = rel["file"]
        if Path(fname).is_absolute():
            raise ValueError(f"{rel['name']}: absolute fact file names are not supported: '{fname}'")
        src, dst = src_dir / fname, dst_dir / fname
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() or dst.is_symlink():
            dst.unlink()
        if not rel.get("read", True):
            dst.write_text("")
        elif not src.is_file():
            continue
        elif has_records(rel["columns"]):
            rewrite(src, dst, flat_line, rel["columns"])
        else:
            try:
                dst.symlink_to(src.resolve())
            except OSError:
                shutil.copyfile(src, dst)


def place_outputs(outputs: list, out_dir: Path) -> None:
    """Writes the files of `outputs` (the manifest's file outputs) that
    FlowLog wrote in out_dir where Souffle writes them, relative to the
    working directory. Souffle writes every output, but FlowLog writes no
    file for one it prunes (nothing derives it), so such a file is created
    empty. A nullary relation that holds has a line in FlowLog's file, and
    `()` in Souffle's. Raises ValueError on a line it cannot convert."""
    for fname, columns in {rel["file"]: rel["columns"] for rel in outputs}.items():
        src, dst = out_dir / fname, Path(fname)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not src.is_file():
            dst.write_text("")
        elif not columns:
            with src.open("rb") as f:
                holds = bool(f.readline())
            dst.write_text("()\n" if holds else "")
        elif has_records(columns):
            rewrite(src, dst, souffle_line, columns)
        else:
            shutil.move(str(src), dst)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("schema", type=Path, help="schema manifest written by souffle2flowlog.py --schema")
    ap.add_argument("src", type=Path, help="Souffle fact directory")
    ap.add_argument("dst", type=Path, help="FlowLog fact directory")
    args = ap.parse_args()
    try:
        convert(json.loads(args.schema.read_text()).get("files", {}).get("inputs", []), args.src, args.dst)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

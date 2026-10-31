"""The FlowLog engine: runs CTADL's analyses with FlowLog instead of Souffle.

With `--engine flowlog`, `ctadl index` and `ctadl query` run their analysis
with FlowLog (https://github.com/flowlog-rs/flowlog). Everything else --
import, models, preprocessing, the index database and the query results --
is the same as with Souffle. The preprocessed Souffle program is translated
to FlowLog (souffle2flowlog.py) and compiled with flowlog-compiler. Like the
Souffle binaries, the compiled analyses are cached in CTADL's state
directory, keyed by the program, the translation and the compiler. A run
converts the analysis' inputs to the layouts FlowLog reads, and its outputs
back to Souffle's (facts.py for files, sqlite_layout.py for sqlite), so the
index database and the output files are the same whichever engine wrote
them. Input and output files must have relative names, as CTADL's do.

The translation is generic; CTADL's part of it is in souffle-logic/:
functors.rs, the Rust port of functors.cpp, and flowlog-policy.json, which
drops the choice-domains whose dependent columns are functions of the key
(the other choice-domains are encoded as min-choice).

Environment:
  FLOWLOG_COMPILER      flowlog-compiler (default: on PATH)
  FLOWLOG_RUNTIME_PATH  flowlog-runtime source that flowlog-compiler builds
                        against (read by flowlog-compiler; default: the
                        crates.io release it was built with)
  CARGO_TARGET_DIR      cargo target directory, shared by the compilations
                        (default: in CTADL's analysis cache)
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections import defaultdict
from contextlib import suppress
from importlib import resources
from pathlib import Path

import ctadl
from ctadl import analysiscachedir, error, status

from . import facts, souffle2flowlog, sqlite_layout

PACKAGE = resources.files(ctadl)
TRANSLATOR = PACKAGE / "flowlog" / "souffle2flowlog.py"
TRANSLATOR_UDFS = PACKAGE / "flowlog" / "souffle2flowlog.rs"
POLICY = PACKAGE / "souffle-logic" / "flowlog-policy.json"
FUNCTORS = PACKAGE / "souffle-logic" / "functors.rs"
CACHE = analysiscachedir / "flowlog"


def compiler() -> Path:
    path = os.environ.get("FLOWLOG_COMPILER") or shutil.which("flowlog-compiler")
    if not path or not Path(path).is_file():
        error(
            f"cannot find flowlog-compiler{f' at {path!r}' if path else ' in PATH'}",
            remediations=[
                "Install FlowLog (https://github.com/flowlog-rs/flowlog)",
                "Set environment variable FLOWLOG_COMPILER to the flowlog-compiler binary",
            ],
        )
        sys.exit(1)
    return Path(path).resolve()


class FlowLogAnalysis:
    """A CTADL analysis compiled with FlowLog (cached), and its I/O."""

    def __init__(self, src: Path):
        flc = compiler()
        runtime = os.environ.get("FLOWLOG_RUNTIME_PATH")
        key = hashlib.sha256()
        for p in (src, POLICY, FUNCTORS, TRANSLATOR, TRANSLATOR_UDFS):
            key.update(p.read_bytes() + b"\0")
        st = flc.stat()
        runtime = str(Path(runtime).resolve()) if runtime else ""
        key.update(f"{flc}:{st.st_size}:{st.st_mtime_ns}:{runtime}".encode())
        self.dir = CACHE / key.hexdigest()[:32]
        self.bin = self.dir / "analysis"
        self.dir.mkdir(parents=True, exist_ok=True)
        # concurrent CTADL runs compile a program once
        with open(self.dir / ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not self.bin.exists():
                self.build(src, flc)
        self.schema = json.loads((self.dir / "schema.json").read_text())
        self.files = self.schema.get("files", {})
        for rel in self.inputs() + self.outputs():
            name = rel["file"]
            # the run reads and writes FlowLog's layout in a scratch directory
            if Path(name).is_absolute() or name.startswith(("file:", ":memory:")):
                error(
                    f"the FlowLog engine needs relative input and output file names: '{name}' ({rel['name']})"
                )
                sys.exit(1)

    def inputs(self) -> list:
        """The manifest's sqlite and file inputs"""
        return self.schema.get("inputs", []) + self.files.get("inputs", [])

    def outputs(self) -> list:
        """The manifest's sqlite and file outputs"""
        return self.schema.get("outputs", []) + self.files.get("outputs", [])

    def build(self, src: Path, flc: Path) -> None:
        try:
            items = souffle2flowlog.Parser(
                souffle2flowlog.lex(src.read_text())
            ).parse_program()
            tr = souffle2flowlog.Translator(items, json.loads(POLICY.read_text()))
            text = "\n".join(souffle2flowlog.print_items(tr.run())) + "\n"
        except (souffle2flowlog.TranslateError, souffle2flowlog.ParseFail) as e:
            error(f"cannot translate '{src}' to FlowLog: {e}")
            sys.exit(1)
        for w in tr.warnings:
            status(f"souffle2flowlog: {w}", verb=1)
        program, udfs = self.dir / "program.fl", self.dir / "udf.rs"
        program.write_text(
            f"// Generated by souffle2flowlog.py from {src.resolve()}\n" + text
        )
        (self.dir / "schema.json").write_text(json.dumps(tr.schema, indent=1))
        (self.dir / "translate.log").write_text(
            "".join(f"warning: {w}\n" for w in tr.warnings)
        )
        udfs.write_text(TRANSLATOR_UDFS.read_text() + "\n" + FUNCTORS.read_text())
        target = os.environ.get("CARGO_TARGET_DIR") or str(CACHE / "target")
        # flowlog-compiler names the crate after the executable. In a shared
        # target dir, cargo can take a build of another crate of the same name
        # for this one's (flowlog-rs/flowlog#507; FlowLog's fix adds a hash of
        # the build dir to the name), and the cleanup below, by name, would
        # remove a build still in progress: so each cache entry (built by one
        # process at a time) gets a name of its own, even for the same
        # analysis in another cache.
        name = hashlib.sha256(str(self.dir.resolve()).encode()).hexdigest()[:32]
        tmp, log = self.dir / f"ctadl_{name}", self.dir / "compile.log"
        cmd = [
            str(flc),
            str(program),
            "--str-intern",
            "--udf-file",
            str(udfs),
            "-o",
            str(tmp),
            "-T",
            target,
        ]
        status(f"compiling FlowLog analysis to '{self.bin}' (takes a few minutes)...")
        with open(log, "w") as out:
            res = subprocess.run(cmd, stdout=out, stderr=subprocess.STDOUT)
        if res.returncode != 0 or not tmp.exists():
            tail = log.read_text(errors="replace").splitlines()[-30:]
            error(f"FlowLog compilation failed (log: '{log}'):\n" + "\n".join(tail))
            sys.exit(1)
        os.replace(tmp, self.bin)
        # The binary is cached here: of its build, the target dir need only
        # keep what the analyses share (the runtime and dependencies). The
        # crate's products start with its name, the executable's (suffixed
        # with FlowLog's hash, after the fix), which no other entry's starts
        # with.
        release = Path(target) / "release"
        for d in ("", "deps/", ".fingerprint/"):
            for p in release.glob(f"{d}{tmp.name}*"):
                with suppress(OSError):
                    if p.is_dir():
                        shutil.rmtree(p)
                    else:
                        p.unlink()

    def read_inputs(self, fact_dir, tmp: Path) -> tuple[Path, Path]:
        """Writes the analysis' inputs, from the Souffle fact directory
        FACT_DIR (default: the working directory, as for Souffle) and the
        sqlite databases its program reads, to a fresh directory in TMP, in
        FlowLog's layout. An input missing from them is left out: like
        Souffle, FlowLog fails if it reads it. Returns the input and output
        directories for the FlowLog run."""
        indir, outdir = tmp / "in", tmp / "out"
        src = Path(fact_dir or ".")
        for d, rels in ((indir, self.inputs()), (outdir, self.outputs())):
            d.mkdir()
            for rel in rels:
                (d / rel["file"]).parent.mkdir(parents=True, exist_ok=True)
        try:
            facts.convert(self.files.get("inputs", []), src, indir)
            for fname, rels in by_file(self.schema.get("inputs", [])).items():
                # resolved as Souffle does
                db = src / fname
                if not db.exists():
                    error(f"input database does not exist: '{db}'")
                    sys.exit(1)
                sqlite_layout.to_flowlog(str(db), str(indir / fname), {"inputs": rels})
        except ValueError as e:
            error(f"cannot read the analysis inputs: {e}")
            sys.exit(1)
        return indir, outdir

    def write_outputs(self, outdir: Path) -> None:
        """Writes the outputs of the run, in OUTDIR, where Souffle writes them
        (CTADL runs it in the output directory), in Souffle's layout: the
        sqlite outputs are added to their databases, the output files placed
        (facts.place_outputs)."""
        try:
            for fname, rels in by_file(self.schema.get("outputs", [])).items():
                sqlite_layout.to_souffle(
                    str(outdir / fname), fname, {"outputs": rels}, append=True
                )
            facts.place_outputs(self.files.get("outputs", []), outdir)
        except ValueError as e:
            error(f"cannot write the analysis outputs: {e}")
            sys.exit(1)


def by_file(rels: list) -> dict:
    out: dict = defaultdict(list)
    for r in rels:
        out[r["file"]].append(r)
    return out

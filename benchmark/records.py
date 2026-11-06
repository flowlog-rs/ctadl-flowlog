"""Process records shared by setup, preparation and measured benchmark runs."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import time
from pathlib import Path


def utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def event(path: Path | None, **fields) -> None:
    if path is not None:
        with path.open("a") as stream:
            stream.write(
                json.dumps({"time": utc(), **fields}, ensure_ascii=False) + "\n"
            )


def stop_group(process) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    # Descendants may still be alive even if the direct child has exited.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def command(
    argv,
    directory: Path,
    *,
    cwd: Path,
    env: dict,
    timeout: float,
    events: Path | None = None,
) -> dict:
    """Preserve raw streams and metadata even for launch failures or timeouts."""
    directory.mkdir(parents=True, exist_ok=False)
    argv = list(map(str, argv))
    record = {
        "schema_version": 1,
        "argv": argv,
        "cwd": str(cwd),
        "started_at": utc(),
        "timeout_seconds": timeout,
        "load_average_before": list(os.getloadavg()),
        "status": "running",
        "exit_code": None,
        "wall_seconds": None,
    }
    write_json(directory / "command.json", record)
    event(events, kind="command_started", directory=str(directory), argv=argv)
    print(f"[{record['started_at']}] {directory}", flush=True)
    started = time.perf_counter()
    interrupted = False
    with (directory / "stdout.log").open("wb") as stdout, (
        directory / "stderr.log"
    ).open("wb") as stderr:
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            try:
                record["exit_code"] = process.wait(timeout=timeout)
                record["status"] = "ok" if process.returncode == 0 else "failed"
            except subprocess.TimeoutExpired:
                stop_group(process)
                record.update(status="timeout", exit_code=process.returncode)
            except KeyboardInterrupt:
                stop_group(process)
                record.update(status="interrupted", exit_code=process.returncode)
                interrupted = True
        except OSError as exc:
            stderr.write(str(exc).encode())
            record.update(status="launch_failed", error=str(exc))
    record.update(finished_at=utc(), wall_seconds=time.perf_counter() - started)
    write_json(directory / "command.json", record)
    event(events, kind="command_finished", directory=str(directory), **record)
    if interrupted:
        raise KeyboardInterrupt
    return record


def require_ok(record: dict, directory: Path) -> None:
    if record["status"] != "ok":
        raise RuntimeError(f"{record['status']}: see {directory}/stderr.log")


def phase_metrics(
    directory: Path,
    record: dict,
    *,
    measured: bool = False,
    expected_sqlite_io: str | None = None,
) -> dict:
    """Missing or malformed measurements are errors, never zero durations."""
    result = {
        "wall_seconds": record["wall_seconds"],
        "analyze_seconds": None,
        "engine_seconds": None,
        "process_peak_rss_mib": None,
    }
    if record["status"] != "ok":
        return result
    phases = [
        json.loads(line)
        for line in (directory / "timings.jsonl").read_text().splitlines()
    ]
    phases = [r for r in phases if r["kind"] == "phase"]
    if expected_sqlite_io:
        tagged = [
            r
            for r in phases
            if r["phase"] in ("prepare_inputs", "engine", "finalize_outputs")
        ]
        if {r["phase"] for r in tagged} != {
            "prepare_inputs",
            "engine",
            "finalize_outputs",
        } or any(r.get("sqlite_io") != expected_sqlite_io for r in tagged):
            raise ValueError(
                "Missing native SQLite I/O provenance; do not mix legacy adapter measurements"
            )
    if measured and any(r["phase"] == "compile" for r in phases):
        raise ValueError(
            "Compilation occurred during measurement; this sample is invalid"
        )
    engines = [r for r in phases if r["phase"] == "engine" and r["status"] == "ok"]
    analyses = [r for r in phases if r["phase"] == "analysis" and r["status"] == "ok"]
    if len(engines) != 1 or len(analyses) != 1:
        raise ValueError(
            "Expected exactly one engine measurement and one analysis measurement"
        )
    result["engine_seconds"] = engines[0]["seconds"]
    result["analyze_seconds"] = analyses[0]["seconds"]
    resources = (directory / "resources.txt").read_text()
    rss = re.search(r"Maximum resident set size \(kbytes\): (\d+)", resources)
    result["process_peak_rss_mib"] = int(rss[1]) / 1024 if rss else None
    for key in (
        "wall_seconds",
        "analyze_seconds",
        "engine_seconds",
        "process_peak_rss_mib",
    ):
        value = result.get(key)
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"Invalid {key}: {value!r}")
    return result

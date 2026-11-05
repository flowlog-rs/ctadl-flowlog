"""Optional phase timings and subprocess output for normal CTADL commands."""

import contextlib
import datetime
import json
import time
from pathlib import Path

_output = None
_directory = None
_command_number = 0


@contextlib.contextmanager
def record_to(path):
    """Record JSON lines while the command runs; disabled when path is None."""
    global _output, _directory, _command_number
    if path is None:
        yield
        return
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = _output, _directory, _command_number
    with path.open("w") as stream:
        directory = path.with_name(path.name + ".commands")
        directory.mkdir(exist_ok=True)
        _output, _directory, _command_number = stream, directory, 0
        try:
            yield
        finally:
            _output, _directory, _command_number = previous


def emit(**fields):
    if _output is not None:
        json.dump({"schema_version": 1, **fields}, _output)
        _output.write("\n")
        _output.flush()


@contextlib.contextmanager
def span(phase, **fields):
    """Time a phase without changing its return value or exception handling."""
    if _output is None:
        yield
        return
    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    started = time.perf_counter()
    state = "ok"
    try:
        yield
    except BaseException:
        state = "failed"
        raise
    finally:
        emit(
            kind="phase",
            phase=phase,
            started_at=started_at,
            seconds=time.perf_counter() - started,
            status=state,
            **fields,
        )


def subprocess_result(result, seconds):
    """Keep captured subprocess output even when the subprocess succeeded."""
    global _command_number
    if _output is None:
        return
    _command_number += 1
    prefix = f"command-{_command_number:03d}"
    streams = {}
    for name in ("stdout", "stderr"):
        value = getattr(result, name)
        if value is not None:
            filename = f"{prefix}.{name}.log"
            (_directory / filename).write_bytes(
                value.encode() if isinstance(value, str) else value
            )
            streams[name] = f"{_directory.name}/{filename}"
    emit(
        kind="subprocess",
        argv=list(map(str, result.args)),
        seconds=seconds,
        exit_code=result.returncode,
        streams=streams,
    )

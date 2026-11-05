"""Versioned workload inputs; no analysis logic lives here."""

import json
import shutil
import sys
from pathlib import Path

from records import command, digest, require_ok, utc, write_json

ROOT = Path(__file__).resolve().parent.parent
HERE = ROOT / "benchmark"
MANIFEST = HERE / "workloads.json"


def select(suite, names=None):
    rows = json.loads(MANIFEST.read_text())["workloads"]
    if not names:
        return [r for r in rows if suite in r["suites"]]
    names = names.split(",")
    lookup = {r["id"]: r for r in rows}
    if len(set(names)) != len(names) or set(names) - lookup.keys():
        raise ValueError("Unknown or duplicate workload; see benchmark/workloads.json")
    return [lookup[n] for n in names]


def prepare_input(row: dict, env: dict, logs: Path, offline: bool) -> Path:
    destination = HERE / "inputs" / row["id"] / row["filename"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    expected = row.get("sha256")
    if destination.exists():
        if expected and digest(destination) != expected:
            raise RuntimeError(
                f"Checksum mismatch: {destination}; preserve and inspect this file"
            )
        return destination
    if "source_file" in row:
        shutil.copyfile(ROOT / row["source_file"], destination)
    elif "generate" in row:
        d = logs / "generate"
        r = command(
            [
                sys.executable,
                HERE / "gen_tnt.py",
                *row["generate"],
                "-o",
                destination,
            ],
            d,
            cwd=ROOT,
            env=env,
            timeout=60,
        )
        require_ok(r, d)
    else:
        if offline:
            raise RuntimeError(
                f"Input {row['id']} is not cached; run python3 benchmark/prepare.py without --offline"
            )
        archive = destination.parent / (
            row.get("deb") or (row["filename"] + ".download")
        )
        for index, url in enumerate(row["urls"]):
            d = logs / f"download-{index + 1}"
            r = command(
                [
                    "curl",
                    "--fail",
                    "--location",
                    "--retry",
                    "2",
                    "--connect-timeout",
                    "30",
                    "--max-time",
                    "900",
                    "--output",
                    str(archive) + ".part",
                    url,
                ],
                d,
                cwd=ROOT,
                env=env,
                timeout=1000,
            )
            if r["status"] == "ok":
                Path(str(archive) + ".part").replace(archive)
                break
        else:
            raise RuntimeError(
                f"Could not download pinned input {row['id']}; logs: {logs}"
            )
        if "deb" in row:
            extracted = destination.parent / "extracted"
            d = logs / "extract"
            r = command(
                ["dpkg-deb", "-x", archive, extracted],
                d,
                cwd=ROOT,
                env=env,
                timeout=120,
            )
            require_ok(r, d)
            candidates = [
                p
                for p in extracted.rglob(row["member"])
                if p.is_file() and not p.is_symlink()
            ]
            if len(candidates) != 1:
                raise RuntimeError(
                    f"Expected one {row['member']} in {archive}; got {candidates}"
                )
            shutil.copyfile(candidates[0], destination)
            shutil.rmtree(extracted)
        else:
            archive.replace(destination)
    if expected and digest(destination) != expected:
        raise RuntimeError(
            f"Checksum mismatch: {destination}; downloaded bytes preserved"
        )
    write_json(
        destination.parent / "input.json",
        {**row, "actual_sha256": digest(destination), "prepared_at": utc()},
    )
    return destination

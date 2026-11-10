"""Download the LogChunks dataset from Zenodo and verify its checksums.

LogChunks (Brandt, Panichella, Zaidman, Beller, MSR 2020) is published under
CC-BY-4.0 at https://doi.org/10.5281/zenodo.3632351. It is not vendored in this
repository; this script fetches it into a local directory (``.data/logchunks`` by
default) and unpacks any zip archives. Re-running it is a no-op once the files
are present and their checksums match.

Usage: python scripts/fetch_logchunks.py [--dest DIR] [--record 3632351]
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import zipfile
from pathlib import Path

import httpx

DEFAULT_RECORD = "3632351"
API = "https://zenodo.org/api/records/{record}"


def _digest(path: Path, algorithm: str) -> str:
    h = hashlib.new(algorithm)
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(dest: Path, record: str) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        meta = client.get(API.format(record=record))
        meta.raise_for_status()
        files = meta.json()["files"]
        paths: list[Path] = []
        for entry in files:
            name = entry["key"]
            algorithm, expected = entry["checksum"].split(":", 1)
            target = dest / name
            if target.exists() and _digest(target, algorithm) == expected:
                print(f"have {name}")
            else:
                print(f"downloading {name} ({entry['size']} bytes)")
                with client.stream("GET", entry["links"]["self"]) as resp:
                    resp.raise_for_status()
                    with target.open("wb") as fh:
                        for chunk in resp.iter_bytes():
                            fh.write(chunk)
                actual = _digest(target, algorithm)
                if actual != expected:
                    target.unlink()
                    raise SystemExit(f"{name}: {algorithm} {actual} != {expected}")
            paths.append(target)
    for path in paths:
        if path.suffix == ".zip":
            marker = dest / f".{path.name}.unpacked"
            if not marker.exists():
                with zipfile.ZipFile(path) as zf:
                    zf.extractall(dest)
                marker.write_text(_digest(path, "sha256"))
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dest", type=Path, default=Path(".data/logchunks"))
    parser.add_argument("--record", default=DEFAULT_RECORD)
    args = parser.parse_args(argv)
    for path in fetch(args.dest, args.record):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

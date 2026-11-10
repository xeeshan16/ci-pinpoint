"""Download the LogChunks dataset from Zenodo and verify its checksums.

LogChunks is published under CC BY 4.0 at https://doi.org/10.5281/zenodo.3632351
and is not vendored in this repository. :func:`fetch_logchunks` downloads every
file of the record into a directory, checks each against the checksum Zenodo
publishes, and unpacks the zip archive. Running it again is a no-op once the
files are present and verified.
"""

from __future__ import annotations

import hashlib
import logging
import zipfile
from pathlib import Path, PurePosixPath

import httpx

from ci_pinpoint.evaluation.dataset import DatasetError

log = logging.getLogger(__name__)

LOGCHUNKS_RECORD = "3632351"
ZENODO_API = "https://zenodo.org/api/records/{record}"


def _digest(path: Path, algorithm: str) -> str:
    h = hashlib.new(algorithm)
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_extract(archive: Path, dest: Path) -> None:
    """Unzip, refusing members that would land outside ``dest``."""
    root = dest.resolve()
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            name = PurePosixPath(member.filename)
            target = (dest / name).resolve()
            if name.is_absolute() or not target.is_relative_to(root):
                raise DatasetError(f"{archive.name}: unsafe path in archive: {member.filename}")
        zf.extractall(dest)


def fetch_logchunks(
    dest: Path,
    *,
    record: str = LOGCHUNKS_RECORD,
    api_url: str = ZENODO_API,
    client: httpx.Client | None = None,
    timeout_s: float = 120.0,
) -> list[Path]:
    """Download and unpack the record into ``dest``; return the downloaded files."""
    dest.mkdir(parents=True, exist_ok=True)
    own = client is None
    http = client or httpx.Client(timeout=timeout_s, follow_redirects=True)
    try:
        try:
            meta = http.get(api_url.format(record=record))
            meta.raise_for_status()
            entries = meta.json()["files"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise DatasetError(f"could not read Zenodo record {record}: {exc}") from exc
        paths: list[Path] = []
        for entry in entries:
            name = PurePosixPath(str(entry["key"])).name
            algorithm, expected = str(entry["checksum"]).split(":", 1)
            target = dest / name
            if target.exists() and _digest(target, algorithm) == expected:
                log.info("dataset file present", extra={"file": name})
            else:
                log.info("downloading", extra={"file": name, "bytes": entry.get("size")})
                partial = target.with_name(target.name + ".part")
                try:
                    with http.stream("GET", str(entry["links"]["self"])) as resp:
                        resp.raise_for_status()
                        with partial.open("wb") as fh:
                            for chunk in resp.iter_bytes():
                                fh.write(chunk)
                except httpx.HTTPError as exc:
                    partial.unlink(missing_ok=True)
                    raise DatasetError(f"download of {name} failed: {exc}") from exc
                actual = _digest(partial, algorithm)
                if actual != expected:
                    partial.unlink()
                    raise DatasetError(f"{name}: {algorithm} is {actual}, expected {expected}")
                partial.replace(target)
            paths.append(target)
    finally:
        if own:
            http.close()
    for path in paths:
        if path.suffix == ".zip":
            marker = dest / f".{path.name}.unpacked"
            digest = _digest(path, "sha256")
            if not marker.exists() or marker.read_text() != digest:
                _safe_extract(path, dest)
                marker.write_text(digest)
    return paths

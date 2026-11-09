"""Load logs from a file (plain or .gz), stdin, or a GitHub Actions URL."""

from __future__ import annotations

import gzip
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO

import httpx

from ci_pinpoint.ingest.github import (
    GitHubActionsClient,
    fetch_failed_logs,
    github_token,
    parse_run_url,
)
from ci_pinpoint.models import RawLog

MAX_FILE_BYTES = 256 * 1024 * 1024


class SourceError(ValueError):
    pass


def _decode(data: bytes) -> str:
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data.decode("utf-8", errors="replace")


def read_stream(stream: BinaryIO, name: str = "stdin") -> RawLog:
    data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise SourceError(f"{name} is larger than {MAX_FILE_BYTES // 2**20} MiB")
    return RawLog(text=_decode(data), source=name)


def load_sources(
    source: str,
    *,
    env: Mapping[str, str],
    api_url: str = "https://api.github.com",
    timeout_s: float = 60.0,
    transport: httpx.BaseTransport | None = None,
    stdin: BinaryIO | None = None,
) -> list[RawLog]:
    """Resolve SOURCE into one or more raw logs (one per failed job for a run URL)."""
    if source == "-":
        return [read_stream(stdin or sys.stdin.buffer)]
    if source.startswith(("http://", "https://")):
        ref = parse_run_url(source)
        if ref is None:
            raise SourceError(
                "unsupported URL; expected https://github.com/<owner>/<repo>/actions/runs/<id>"
                "[/job/<job_id>]"
            )
        client = GitHubActionsClient(
            token=github_token(env), api_url=api_url, timeout_s=timeout_s, transport=transport
        )
        try:
            return fetch_failed_logs(client, ref)
        finally:
            client.close()
    path = Path(source)
    if not path.is_file():
        raise SourceError(f"{source}: no such file")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise SourceError(f"{source} is larger than {MAX_FILE_BYTES // 2**20} MiB")
    return [RawLog(text=_decode(path.read_bytes()), source=str(path))]

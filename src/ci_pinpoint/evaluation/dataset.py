"""Labelled datasets for the evaluation: LogChunks, and any manifest of your own.

A :class:`Case` is one failed CI log plus the lines a reader needs to see to
understand the failure. Labels are stored as *requirements*: each requirement is
a set of raw line numbers, and it is met when any one of them is shown. That one
shape covers both sources:

* **LogChunks** labels the failure as a chunk of text copied out of the log. Each
  non-empty chunk line becomes a requirement whose alternatives are the log lines
  it was matched to (a chunk can occur more than once, e.g. a test failure that is
  printed again in a summary).
* **Manifests** (``manifest.json`` next to the logs, as in ``tests/fixtures/logs``)
  list anchor substrings; each anchor is a requirement met by any line containing it.

LogChunks: Brandt, Panichella, Zaidman and Beller, "LogChunks: A Data Set for
Build Log Analysis", MSR 2020. 797 Travis CI logs from 80 GitHub repositories,
CC BY 4.0, https://doi.org/10.5281/zenodo.3632351. Not vendored; see
:mod:`ci_pinpoint.evaluation.fetch`.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ci_pinpoint.normalize import ANSI_RE, split_lines

LOGCHUNKS_CITATION = (
    'C. E. Brandt, A. Panichella, A. Zaidman and M. Beller, "LogChunks: A Data Set for '
    'Build Log Analysis", MSR 2020. https://doi.org/10.5281/zenodo.3632351 (CC BY 4.0)'
)
LOGCHUNKS_LICENSE = "CC-BY-4.0"

Split = Literal["all", "dev", "test"]
SPLITS: tuple[Split, ...] = ("all", "dev", "test")
DEV_SHARE = 3  # out of 10: repositories hashed into buckets 0-2 form the dev split

# Escape bytes are not allowed in XML 1.0, so LogChunks labels keep only the
# printable remainder of colour codes: "[m[37m", "[30;1m", "[0K".
ORPHAN_SGR_RE = re.compile(r"(?:\(B)?\[(?:\d{1,3}(?:;\d{1,3})*)?[mK]")  # also tput's "(B"
MIN_SUBSTRING = 6  # shorter chunk lines must match a log line exactly
MAX_SKIP = 2  # non-matching, non-blank log lines tolerated between two chunk lines
MIN_LOCATED_SHARE = 0.5  # at least this share of chunk lines must be found
ANCHOR_CANDIDATES = 3  # try the longest few chunk lines as alignment anchors


class DatasetError(ValueError):
    """The dataset is missing or not in the expected layout."""


@dataclass(frozen=True, slots=True)
class Case:
    """One labelled log."""

    id: str
    path: Path
    repo: str
    split: Literal["dev", "test"]
    requirements: tuple[frozenset[int], ...]
    category: str | None = None
    language: str | None = None

    @property
    def label_lines(self) -> frozenset[int]:
        return frozenset().union(*self.requirements)

    def text(self) -> str:
        return read_log(self.path)


@dataclass(frozen=True, slots=True)
class Unlocated:
    """A label that could not be matched to the log it belongs to."""

    id: str
    reason: str
    found: int = 0
    wanted: int = 0


@dataclass(frozen=True, slots=True)
class Dataset:
    name: str
    root: Path
    cases: tuple[Case, ...]
    unlocated: tuple[Unlocated, ...]
    license: str
    citation: str


def read_log(path: Path) -> str:
    data = path.read_bytes()
    if path.suffix == ".gz":
        data = gzip.decompress(data)
    return data.decode("utf-8", errors="replace")


def split_of(repo: str) -> Literal["dev", "test"]:
    """Deterministic split by repository, so no repository is in both halves."""
    bucket = int(hashlib.sha256(repo.encode()).hexdigest(), 16) % 10
    return "dev" if bucket < DEV_SHARE else "test"


def label_text(text: str) -> str:
    """Normalise a log or label line for matching: no escapes, single spaces."""
    text = ANSI_RE.sub("", text).replace("\x1b", "")
    text = ORPHAN_SGR_RE.sub("", text)
    return " ".join(text.split())


def _variants(raw: str) -> tuple[str, ...]:
    """What a raw line may look like in a label: each \\r segment, or all of them."""
    parts = [label_text(p) for p in raw.split("\r")]
    parts = [p for p in parts if p]
    if len(parts) > 1:
        parts.append(" ".join(parts))
    return tuple(parts)


class _Matcher:
    def __init__(self, raw_lines: Sequence[str]) -> None:
        self.variants = [_variants(line) for line in raw_lines]

    def __len__(self) -> int:
        return len(self.variants)

    def blank(self, index: int) -> bool:
        return not self.variants[index]

    def matches(self, target: str, index: int) -> bool:
        if len(target) < MIN_SUBSTRING:
            return target in self.variants[index]
        return any(target in v for v in self.variants[index])

    def find(self, target: str, start: int, step: int) -> int | None:
        """The nearest line from ``start`` in direction ``step`` matching ``target``,
        skipping blank lines and at most ``MAX_SKIP`` other lines."""
        skipped = 0
        index = start
        while 0 <= index < len(self) and skipped <= MAX_SKIP:
            if self.matches(target, index):
                return index
            if not self.blank(index):
                skipped += 1
            index += step
        return None


def _walk(matcher: _Matcher, targets: Sequence[str], j: int, i: int) -> dict[int, int]:
    """Align ``targets`` around an anchor (target ``j`` at line index ``i``)."""
    aligned = {j: i}
    pos = i + 1
    for t in range(j + 1, len(targets)):
        found = matcher.find(targets[t], pos, 1)
        if found is not None:
            aligned[t] = found
            pos = found + 1
    pos = i - 1
    for t in range(j - 1, -1, -1):
        found = matcher.find(targets[t], pos, -1)
        if found is not None:
            aligned[t] = found
            pos = found - 1
    return aligned


def chunk_targets(chunk: str) -> list[str]:
    return [t for t in (label_text(p) for p in re.split(r"\r\n|\r|\n", chunk)) if t]


def locate_chunk(
    chunk: str, raw_lines: Sequence[str]
) -> tuple[tuple[frozenset[int], ...], int, int]:
    """Match a LogChunks chunk to raw line numbers (1-based).

    Returns ``(requirements, found, wanted)``. ``requirements`` is empty when
    fewer than half of the chunk's lines could be aligned.
    """
    targets = chunk_targets(chunk)
    if not targets:
        return (), 0, 0
    matcher = _Matcher(raw_lines)
    anchors = sorted(range(len(targets)), key=lambda t: -len(targets[t]))[:ANCHOR_CANDIDATES]
    best: list[dict[int, int]] = []
    best_size = 0
    for j in anchors:
        for i in range(len(matcher)):
            if not matcher.matches(targets[j], i):
                continue
            aligned = _walk(matcher, targets, j, i)
            if len(aligned) > best_size:
                best, best_size = [aligned], len(aligned)
            elif len(aligned) == best_size and aligned not in best:
                best.append(aligned)
        if best_size == len(targets):
            break
    if best_size < max(1, MIN_LOCATED_SHARE * len(targets)):
        return (), best_size, len(targets)
    requirements = tuple(
        frozenset(a[t] + 1 for a in best if t in a)
        for t in range(len(targets))
        if any(t in a for a in best)
    )
    return requirements, best_size, len(targets)


def _logchunks_root(path: Path) -> Path:
    for candidate in (path, path / "LogChunks"):
        if (candidate / "build-failure-reason").is_dir() and (candidate / "logs").is_dir():
            return candidate
    raise DatasetError(
        f"{path} does not contain LogChunks (expected build-failure-reason/ and logs/). "
        "Download it with: ci-pinpoint eval --fetch"
    )


def _examples(root: Path) -> Iterator[tuple[str, str, str | None]]:
    for xml in sorted((root / "build-failure-reason").rglob("*.xml")):
        try:
            tree = ET.parse(xml)  # noqa: S314 - a local, checksum-verified dataset file
        except ET.ParseError as exc:
            raise DatasetError(f"{xml}: {exc}") from exc
        for example in tree.getroot().iter("Example"):
            log = (example.findtext("Log") or "").strip()
            if log:
                yield log, example.findtext("Chunk") or "", example.findtext("Category")


def load_logchunks(path: Path, split: Split = "all", limit: int | None = None) -> Dataset:
    """Load LogChunks from a directory produced by :func:`fetch.fetch_logchunks`."""
    root = _logchunks_root(path)
    cases: list[Case] = []
    unlocated: list[Unlocated] = []
    for rel, chunk, category in _examples(root):
        language, repo_dir = rel.split("/")[:2]
        repo = repo_dir.replace("@", "/", 1)
        case_split = split_of(repo)
        if split not in ("all", case_split):
            continue
        if limit is not None and len(cases) + len(unlocated) >= limit:
            break
        case_id = rel.removesuffix(".log")
        log_path = root / "logs" / rel
        if not log_path.is_file():
            unlocated.append(Unlocated(case_id, "log file missing"))
            continue
        requirements, found, wanted = locate_chunk(chunk, split_lines(read_log(log_path)))
        if not requirements:
            reason = "empty chunk" if wanted == 0 else "chunk not found in log"
            unlocated.append(Unlocated(case_id, reason, found, wanted))
            continue
        cases.append(
            Case(
                id=case_id,
                path=log_path,
                repo=repo,
                split=case_split,
                requirements=requirements,
                category=f"{repo}#{category}" if category else None,
                language=language,
            )
        )
    return Dataset(
        name="logchunks",
        root=root,
        cases=tuple(cases),
        unlocated=tuple(unlocated),
        license=LOGCHUNKS_LICENSE,
        citation=LOGCHUNKS_CITATION,
    )


def load_manifest(path: Path, split: Split = "all", limit: int | None = None) -> Dataset:
    """Load ``manifest.json`` (``{"cases": [{"file", "anchors", ...}]}``) from ``path``."""
    manifest_path = path if path.is_file() else path / "manifest.json"
    if not manifest_path.is_file():
        raise DatasetError(f"no manifest.json in {path}")
    root = manifest_path.parent
    manifest = json.loads(manifest_path.read_text())
    cases: list[Case] = []
    unlocated: list[Unlocated] = []
    for entry in manifest.get("cases", []):
        if limit is not None and len(cases) + len(unlocated) >= limit:
            break
        file = str(entry["file"])
        case_id = file.removesuffix(".gz").removesuffix(".log")
        repo = str(entry.get("repo") or manifest.get("repo") or root.name)
        case_split = split_of(f"{repo}:{case_id}")
        if split not in ("all", case_split):
            continue
        log_path = root / file
        if not log_path.is_file():
            unlocated.append(Unlocated(case_id, "log file missing"))
            continue
        lines = [label_text(line) for line in split_lines(read_log(log_path))]
        anchors = [label_text(str(a)) for a in entry.get("anchors", [])]
        requirements = tuple(
            frozenset(no for no, text in enumerate(lines, start=1) if a in text) for a in anchors
        )
        if not requirements or not all(requirements):
            found = sum(1 for r in requirements if r)
            unlocated.append(Unlocated(case_id, "anchor not found in log", found, len(anchors)))
            continue
        cases.append(
            Case(
                id=case_id,
                path=log_path,
                repo=repo,
                split=case_split,
                requirements=requirements,
                category=entry.get("category"),
            )
        )
    return Dataset(
        name=str(manifest.get("name") or "manifest"),
        root=root,
        cases=tuple(cases),
        unlocated=tuple(unlocated),
        license=str(manifest.get("license") or "see manifest"),
        citation=str(manifest.get("description") or manifest_path),
    )

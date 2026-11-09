"""Drain-style log template mining.

Each line is masked (numbers, hashes, UUIDs, durations, paths) and routed
through a fixed-depth prefix tree keyed by token count and the first tokens.
Inside a leaf, the line joins the most similar cluster if enough positions
match, and the cluster template generalises the differing positions to
``<*>``. Otherwise it starts a new cluster.

Used for three things:

* rarity: a line whose template occurs 300 times is less likely to be the cause,
* novelty: two blocks built from the same templates repeat the same evidence,
* collapsing: runs of same-template lines are folded into one line plus a count,
  which leaves more of the token budget for evidence.

Reference: He et al., "Drain: An Online Log Parsing Approach with Fixed Depth
Tree", ICWS 2017. This is an independent, simplified implementation.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

WILDCARD = "<*>"

_MASKS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
        WILDCARD,
    ),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?Z?\b"), WILDCARD),
    (re.compile(r"\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"), WILDCARD),
    (re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{7,40}\b", re.I), WILDCARD),
    (re.compile(r"\b\d+(?:\.\d+)*(?:ms|s|m|h|%|[kKMG]i?B)?\b"), WILDCARD),
)
_SPLIT = re.compile(r"\s+")


def tokenize(text: str) -> list[str]:
    masked = text.strip()
    for pattern, repl in _MASKS:
        masked = pattern.sub(repl, masked)
    return [t for t in _SPLIT.split(masked) if t]


@dataclass
class Cluster:
    id: int
    tokens: list[str]
    size: int = 0

    @property
    def template(self) -> str:
        return " ".join(self.tokens)


@dataclass
class _Node:
    children: dict[str, _Node] = field(default_factory=dict)
    clusters: list[int] = field(default_factory=list)


class TemplateMiner:
    """Online Drain-style miner. ``add`` returns the cluster id for a line."""

    def __init__(self, depth: int = 4, similarity: float = 0.5, max_children: int = 100) -> None:
        if depth < 3:
            raise ValueError("depth must be at least 3")
        self._prefix_depth = depth - 2
        self._similarity = similarity
        self._max_children = max_children
        self._root = _Node()
        self.clusters: list[Cluster] = []

    def _leaf(self, tokens: Sequence[str]) -> _Node:
        node = self._root.children.setdefault(str(len(tokens)), _Node())
        for token in tokens[: self._prefix_depth]:
            key = WILDCARD if any(ch.isdigit() for ch in token) else token
            if key not in node.children:
                if len(node.children) >= self._max_children:
                    key = WILDCARD
                node = node.children.setdefault(key, _Node())
            else:
                node = node.children[key]
        return node

    @staticmethod
    def _score(template: Sequence[str], tokens: Sequence[str]) -> tuple[float, int]:
        same = sum(1 for a, b in zip(template, tokens, strict=True) if a == b and a != WILDCARD)
        wildcards = sum(1 for a in template if a == WILDCARD)
        return same / len(tokens), wildcards

    def add(self, text: str) -> int:
        tokens = tokenize(text)
        if not tokens:
            tokens = [""]
        leaf = self._leaf(tokens)
        best: Cluster | None = None
        best_key = (-1.0, -1)
        for cid in leaf.clusters:
            cluster = self.clusters[cid]
            sim, wildcards = self._score(cluster.tokens, tokens)
            if (sim, wildcards) > best_key:
                best, best_key = cluster, (sim, wildcards)
        if best is not None and best_key[0] >= self._similarity:
            best.tokens = [
                a if a == b else WILDCARD for a, b in zip(best.tokens, tokens, strict=True)
            ]
            best.size += 1
            return best.id
        cluster = Cluster(id=len(self.clusters), tokens=list(tokens), size=1)
        self.clusters.append(cluster)
        leaf.clusters.append(cluster.id)
        return cluster.id


@dataclass(frozen=True, slots=True)
class TemplateIndex:
    """Cluster assignment for every line of one log."""

    cluster_of: dict[int, int]  # raw line number -> cluster id
    sizes: tuple[int, ...]
    templates: tuple[str, ...]

    def size_of(self, line_no: int) -> int:
        cid = self.cluster_of.get(line_no)
        return self.sizes[cid] if cid is not None else 1

    def template_of(self, line_no: int) -> str:
        cid = self.cluster_of.get(line_no)
        return self.templates[cid] if cid is not None else ""


def mine(lines: Iterable[tuple[int, str]], similarity: float = 0.5) -> TemplateIndex:
    """Mine templates for ``(line_no, text)`` pairs. Blank lines are skipped."""
    miner = TemplateMiner(similarity=similarity)
    cluster_of: dict[int, int] = {}
    for no, text in lines:
        if text.strip():
            cluster_of[no] = miner.add(text)
    return TemplateIndex(
        cluster_of=cluster_of,
        sizes=tuple(c.size for c in miner.clusters),
        templates=tuple(c.template for c in miner.clusters),
    )

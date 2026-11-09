"""Optional stage 1b: re-rank candidate blocks with embeddings.

Heuristic scores know which words look like errors. Embeddings add two things
the regexes can't:

* **failure semantics:** similarity to short descriptions of common failure
  modes, so "Timed out waiting for log output" or "requirement failed:
  advertised.listeners ..." rank as failures even without an error keyword;
* **symptom linkage:** similarity to the failed step's tail (the symptom,
  e.g. "Failed to execute goal ... surefire ... There are test failures"),
  which pulls up the block that explains that particular symptom.

The final score blends the heuristic score with the embedding relevance, so a
weak embedder can reorder close calls but not bury a strong heuristic signal.
If the embedder fails (model not pulled, server down), ranking falls back to
the heuristic order and says why.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import replace

from ci_pinpoint.heuristics import STACK_FRAME, LogContext
from ci_pinpoint.models import Block
from ci_pinpoint.providers.base import Embedder, ProviderError
from ci_pinpoint.tokens import clip_line

log = logging.getLogger(__name__)

PROTOTYPES: tuple[str, ...] = (
    "a test failed: assertion error, the expected value differs from the actual value",
    "connection refused: a database, broker or service was not reachable",
    "compilation error: cannot find symbol, type mismatch or syntax error",
    "a dependency or package could not be resolved, downloaded or installed",
    "invalid or unknown configuration value made the application fail to start",
    "the process ran out of memory and was killed",
    "timed out waiting for a container, service or test to become ready",
    "permission denied, forbidden or authentication failed",
    "a required file, command, metric or environment variable is missing or not found",
    "code formatting or lint check reported violations",
    "an exception was thrown, caused by an underlying error",
)
DEFAULT_WEIGHT = 0.35
SYMPTOM_SHARE = 0.3
MAX_BLOCK_CHARS = 1500


def block_text(block: Block) -> str:
    """The text that represents a block for embedding: evidence lines, not frames."""
    lines = [clip_line(line.text, 300) for line in block.lines if not STACK_FRAME.match(line.text)]
    return "\n".join(line for line in lines if line.strip())[:MAX_BLOCK_CHARS] or " "


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class EmbeddingRanker:
    def __init__(self, embedder: Embedder, *, weight: float = DEFAULT_WEIGHT) -> None:
        if not 0.0 <= weight <= 1.0:
            raise ValueError("weight must be between 0 and 1")
        self._embedder = embedder
        self._weight = weight
        self.error: str | None = None

    @property
    def model(self) -> str:
        return self._embedder.model

    def relevance(self, blocks: Sequence[Block]) -> list[float]:
        """Embedding relevance of each block, min-max normalised to [0, 1]."""
        symptom = "\n".join(block_text(b) for b in blocks if "failed_step_tail" in b.signals)
        texts = [*PROTOTYPES, symptom or " ", *(block_text(b) for b in blocks)]
        vectors = self._embedder.embed(texts)
        protos = vectors[: len(PROTOTYPES)]
        symptom_vec = vectors[len(PROTOTYPES)]
        raw: list[float] = []
        for vec in vectors[len(PROTOTYPES) + 1 :]:
            proto = max(_cosine(vec, p) for p in protos)
            linked = _cosine(vec, symptom_vec) if symptom else 0.0
            raw.append((1 - SYMPTOM_SHARE) * proto + SYMPTOM_SHARE * linked)
        low, high = min(raw), max(raw)
        if high - low < 1e-9:
            return [0.5] * len(raw)
        return [(r - low) / (high - low) for r in raw]

    def rank(self, blocks: Sequence[Block], ctx: LogContext) -> list[Block]:
        if len(blocks) < 2:
            return list(blocks)
        try:
            relevance = self.relevance(blocks)
        except ProviderError as exc:
            self.error = str(exc)
            log.warning(
                "embedding ranking skipped, using heuristic order",
                extra={"embedder": self._embedder.model, "error": self.error},
            )
            return list(blocks)
        top = max(b.score for b in blocks)
        ranked = [
            replace(
                block,
                score=round((1 - self._weight) * block.score + self._weight * top * rel, 3),
                signals=tuple(sorted({*block.signals, "embedding"})),
            )
            for block, rel in zip(blocks, relevance, strict=True)
        ]
        log.info(
            "blocks re-ranked with embeddings",
            extra={"embedder": self._embedder.model, "blocks": len(blocks)},
        )
        return ranked

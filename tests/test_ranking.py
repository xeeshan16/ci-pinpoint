from __future__ import annotations

from collections.abc import Sequence

import pytest

from ci_pinpoint.heuristics import analyse, build_excerpt, candidate_blocks
from ci_pinpoint.models import Block, LineKind, LogLine
from ci_pinpoint.normalize import normalize
from ci_pinpoint.providers.base import ProviderError
from ci_pinpoint.providers.fake import FakeEmbedder
from ci_pinpoint.ranking import EmbeddingRanker, block_text

from .conftest import RealCase


def _block(no: int, text: str, score: float = 3.0, signals: tuple[str, ...] = ()) -> Block:
    line = LogLine(no=no, text=text, section=0, kind=LineKind.TEXT)
    return Block(no, no, score, signals, (line,))


class Failing:
    model = "broken"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise ProviderError("model 'nomic-embed-text' not found", status=404)


CTX = analyse(normalize("x"))


def test_relevance_prefers_failure_semantics() -> None:
    ranker = EmbeddingRanker(FakeEmbedder())
    blocks = [
        _block(1, "Downloading index from mirror, 512 kB"),
        _block(2, "connection refused: database service was not reachable"),
    ]
    rel = ranker.relevance(blocks)
    assert rel[1] > rel[0]
    assert min(rel) == 0.0
    assert max(rel) == 1.0
    ranked = ranker.rank(blocks, CTX)
    assert ranked[1].score > ranked[0].score
    assert all("embedding" in b.signals for b in ranked)


def test_heuristic_score_still_dominates() -> None:
    ranker = EmbeddingRanker(FakeEmbedder(), weight=0.35)
    strong = _block(1, "Downloading index from mirror", score=10.0)
    weak = _block(2, "connection refused: database service was not reachable", score=2.0)
    ranked = ranker.rank([strong, weak], CTX)
    assert ranked[0].score > ranked[1].score


def test_symptom_linkage_uses_failed_step_tail() -> None:
    ranker = EmbeddingRanker(FakeEmbedder())
    tail = _block(
        9, "There are test failures in DisputeTriagePipelineIT", signals=("failed_step_tail",)
    )
    linked = _block(3, "DisputeTriagePipelineIT container startup failed")
    unrelated = _block(4, "container startup failed for image redis")
    rel = ranker.relevance([linked, unrelated, tail])
    assert rel[0] > rel[1]


def test_failing_embedder_falls_back_to_heuristics() -> None:
    ranker = EmbeddingRanker(Failing())
    blocks = [_block(1, "a"), _block(2, "b", score=5.0)]
    assert ranker.rank(blocks, CTX) == blocks
    assert ranker.error is not None
    assert "not found" in ranker.error


def test_single_block_and_flat_relevance() -> None:
    ranker = EmbeddingRanker(FakeEmbedder())
    one = [_block(1, "x")]
    assert ranker.rank(one, CTX) == one
    assert ranker.relevance([_block(1, "same"), _block(2, "same")]) == [0.5, 0.5]


def test_weight_is_validated() -> None:
    with pytest.raises(ValueError, match="weight"):
        EmbeddingRanker(FakeEmbedder(), weight=1.5)


def test_block_text_skips_frames_and_clips() -> None:
    block = Block(
        1,
        3,
        1.0,
        (),
        (
            LogLine(1, "Caused by: boom", 0),
            LogLine(2, "\tat com.example.Foo.bar(Foo.java:1)", 0),
            LogLine(3, "y" * 1000, 0),
        ),
    )
    text = block_text(block)
    assert "Foo.java" not in text
    assert text.startswith("Caused by: boom\n")
    assert "[+700 chars]" in text


def test_ranking_keeps_root_causes_on_real_logs(real_case: RealCase) -> None:
    """With an embedder in the loop, labelled root causes still survive selection."""
    log = normalize(real_case.text())
    excerpt = build_excerpt(log, 800, EmbeddingRanker(FakeEmbedder()))
    selected = [line.text for b in excerpt.blocks for line in b.lines]
    for anchor in real_case.anchors:
        assert any(anchor in t for t in selected), anchor
    assert len(candidate_blocks(analyse(log))) == excerpt.candidates

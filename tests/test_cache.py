from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from ci_pinpoint.cache import (
    BriefCache,
    CachedEmbedder,
    EmbeddingStore,
    brief_key,
    default_cache_dir,
)
from ci_pinpoint.models import Brief, LogStats


def _brief() -> Brief:
    stats = LogStats(raw_lines=1, raw_tokens=10, excerpt_tokens=5, sections=1, failed_sections=[])
    return Brief(source="x", job_name=None, job_url=None, stats=stats, excerpt=[], diagnosis=None)


def test_default_cache_dir() -> None:
    assert default_cache_dir({"CI_PINPOINT_CACHE_DIR": "/c"}) == Path("/c")
    assert default_cache_dir({"XDG_CACHE_HOME": "/x"}) == Path("/x/ci-pinpoint")
    assert default_cache_dir({}).name == "ci-pinpoint"


def test_brief_key_depends_on_log_and_settings() -> None:
    base = brief_key("log", {"model": "a"})
    assert base == brief_key("log", {"model": "a"})
    assert base != brief_key("log2", {"model": "a"})
    assert base != brief_key("log", {"model": "b"})


def test_brief_roundtrip_and_corruption(tmp_path: Path) -> None:
    cache = BriefCache(tmp_path)
    assert cache.get("ab12") is None
    cache.put("ab12", _brief())
    got = cache.get("ab12")
    assert got is not None
    assert got.stats.raw_tokens == 10
    (tmp_path / "briefs" / "ab" / "ab12.json").write_text("{not json")
    assert cache.get("ab12") is None


def test_unwritable_cache_does_not_fail(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    BriefCache(blocker).put("ab12", _brief())  # parent is a file: logged, not raised


class Counting:
    model = "count"

    def __init__(self) -> None:
        self.seen: list[str] = []

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.seen.extend(texts)
        return [[float(len(t)), 1.0] for t in texts]


def test_cached_embedder_only_embeds_misses(tmp_path: Path) -> None:
    inner = Counting()
    store = EmbeddingStore(tmp_path / "e.sqlite3")
    emb = CachedEmbedder(inner, store)
    assert emb.embed(["a", "bb", "a"]) == [[1.0, 1.0], [2.0, 1.0], [1.0, 1.0]]
    assert inner.seen == ["a", "bb"]
    assert emb.embed(["bb", "ccc"]) == [[2.0, 1.0], [3.0, 1.0]]
    assert inner.seen == ["a", "bb", "ccc"]
    assert (emb.hits, emb.misses) == (2, 3)
    store.close()
    # A second process sees the same vectors.
    second = EmbeddingStore(tmp_path / "e.sqlite3")
    again = CachedEmbedder(Counting(), second)
    assert again.embed(["ccc"]) == [[3.0, 1.0]]
    assert again.misses == 0
    second.close()


def test_vectors_are_scoped_by_model(tmp_path: Path) -> None:
    store = EmbeddingStore(tmp_path / "e.sqlite3")
    store.put_many("m1", {"d": [1.0]})
    assert store.get_many("m2", ["d"]) == {}
    assert store.get_many("m1", ["d"]) == {"d": pytest.approx([1.0])}
    store.close()

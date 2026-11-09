from __future__ import annotations

import pytest

from ci_pinpoint.normalize import normalize
from ci_pinpoint.templates import WILDCARD, TemplateMiner, mine, tokenize

from .conftest import RealCase


def test_tokenize_masks_variable_parts() -> None:
    assert tokenize("Took 12.5s at 2026-01-01T10:00:00Z for f69f8e3d3cc3") == [
        "Took",
        WILDCARD,
        "at",
        WILDCARD,
        "for",
        WILDCARD,
    ]


def test_similar_lines_share_a_cluster_and_template() -> None:
    miner = TemplateMiner()
    a = miner.add("Downloaded from central: https://repo/a.jar (12 kB at 3.1 MB/s)")
    b = miner.add("Downloaded from central: https://repo/b.jar (40 kB at 9.0 MB/s)")
    c = miner.add("BUILD FAILURE")
    assert a == b != c
    assert miner.clusters[a].template == ("Downloaded from central: <*> (<*> kB at <*> MB/s)")
    assert miner.clusters[a].size == 2


def test_different_lengths_never_merge() -> None:
    miner = TemplateMiner()
    assert miner.add("a b c") != miner.add("a b c d")


def test_dissimilar_lines_of_same_length_stay_apart() -> None:
    miner = TemplateMiner(similarity=0.5)
    assert miner.add("Caused by: java.net.ConnectException refused") != miner.add(
        "Running tests in module core now"
    )


def test_depth_must_leave_room_for_a_prefix() -> None:
    with pytest.raises(ValueError, match="depth"):
        TemplateMiner(depth=2)


def test_mine_skips_blank_lines() -> None:
    index = mine([(1, "ok 1"), (2, ""), (3, "ok 2")])
    assert 2 not in index.cluster_of
    assert index.size_of(1) == 2
    assert index.size_of(2) == 1
    assert index.template_of(3) == f"ok {WILDCARD}"
    assert index.template_of(2) == ""


def test_real_logs_compress_into_few_templates(real_case: RealCase) -> None:
    log = normalize(real_case.text())
    index = mine((line.no, line.text) for line in log.lines)
    assert len(index.templates) < len(log.lines) * 0.8

"""Compare block ranking with and without embeddings on the labelled real logs.

For every case it reports where the first candidate block containing a
labelled root-cause line ranks (1 = first; failed-step tails excluded, since
they are always selected), and whether every root-cause line survives
selection at a range of token budgets.

    uv run python scripts/selection_report.py --embedder fake
    uv run python scripts/selection_report.py --embedder ollama --embed-model nomic-embed-text

Six cases make this a smoke check for ranking changes, not an evaluation.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from pathlib import Path

from ci_pinpoint.config import load_settings
from ci_pinpoint.heuristics import BlockRanker, analyse, candidate_blocks, select_blocks
from ci_pinpoint.logging_setup import configure
from ci_pinpoint.normalize import normalize
from ci_pinpoint.providers import build_embedder
from ci_pinpoint.ranking import EmbeddingRanker

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "logs"
BUDGETS = (150, 200, 300, 400, 600, 800)


def _evaluate(text: str, anchors: list[str], ranker: BlockRanker | None) -> dict[str, object]:
    ctx = analyse(normalize(text))
    blocks = candidate_blocks(ctx)
    if ranker is not None:
        blocks = ranker.rank(blocks, ctx)
    ordered = sorted(
        (b for b in blocks if "failed_step_tail" not in b.signals),
        key=lambda b: (-b.score, -b.end),
    )
    rank = next(
        (
            i
            for i, b in enumerate(ordered, start=1)
            if any(a in line.text for line in b.lines for a in anchors)
        ),
        None,
    )
    recall = {}
    for budget in BUDGETS:
        excerpt = select_blocks(blocks, ctx, budget)
        selected = [line.text for b in excerpt.blocks for line in b.lines]
        recall[budget] = all(any(a in t for t in selected) for a in anchors)
    return {"rank": rank, "candidates": len(ordered), "recall": recall}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--embedder", default="fake")
    parser.add_argument("--embed-model")
    parser.add_argument("--out", type=Path, default=Path("reports/selection"))
    args = parser.parse_args()
    configure(1, os.environ)

    settings = load_settings(overrides={"embedder": args.embedder, "embed_model": args.embed_model})
    embedder = build_embedder(settings, os.environ)
    if embedder is None:
        print("no embedder configured", file=sys.stderr)
        return 2
    ranker = EmbeddingRanker(embedder)
    manifest = json.loads((FIXTURES / "manifest.json").read_text())

    rows = []
    for case in manifest["cases"]:
        text = gzip.decompress((FIXTURES / case["file"]).read_bytes()).decode("utf-8", "replace")
        rows.append(
            {
                "case": case["file"].removesuffix(".log.gz"),
                "heuristic": _evaluate(text, case["anchors"], None),
                "embedding": _evaluate(text, case["anchors"], ranker),
            }
        )
    if ranker.error:
        print(f"embedder failed: {ranker.error}", file=sys.stderr)
        return 1

    def mrr(kind: str) -> float:
        ranks = [r[kind]["rank"] for r in rows]
        return float(sum(1 / r for r in ranks if isinstance(r, int))) / len(rows)

    def recall_at(kind: str, budget: int) -> int:
        return sum(1 for r in rows if r[kind]["recall"][budget])

    lines = [
        f"#### Block ranking on real CI logs: heuristics vs + embeddings ({embedder.model})",
        "",
        "| case | candidates | root-cause block rank (heuristic → embedding) |",
        "|---|---|---|",
    ]
    for r in rows:
        h, e = r["heuristic"], r["embedding"]
        lines.append(f"| {r['case']} | {h['candidates']} | {h['rank']} → {e['rank']} |")
    lines += [
        "",
        f"MRR of the root-cause block: heuristic {mrr('heuristic'):.3f}, "
        f"with embeddings {mrr('embedding'):.3f}.",
        "",
        "| budget (tokens) | " + " | ".join(str(b) for b in BUDGETS) + " |",
        "|---|" + "---|" * len(BUDGETS),
        "| root cause kept, heuristic | "
        + " | ".join(f"{recall_at('heuristic', b)}/{len(rows)}" for b in BUDGETS)
        + " |",
        "| root cause kept, + embeddings | "
        + " | ".join(f"{recall_at('embedding', b)}/{len(rows)}" for b in BUDGETS)
        + " |",
    ]
    report = "\n".join(lines) + "\n"
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "selection.md").write_text(report)
    (args.out / "selection.json").write_text(
        json.dumps({"embedder": embedder.model, "rows": rows}, indent=2, default=str)
    )
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())

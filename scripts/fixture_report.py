"""Run ci-pinpoint over the labelled real CI logs in tests/fixtures/logs.

For each case it records whether every labelled root-cause line made it into
the excerpt, whether the diagnosis cites one of them (verified against the
log), the token reduction, and the model's token usage and latency.

    uv run python scripts/fixture_report.py --provider fake --out reports/fixtures
    uv run python scripts/fixture_report.py --provider ollama --model qwen2.5:3b --out reports/real

This is a smoke check on six cases, not the evaluation: the eval harness with
baselines on a public labelled dataset is `ci-pinpoint eval` (see docs/PLAN.md).
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from pathlib import Path

from ci_pinpoint.config import load_settings
from ci_pinpoint.logging_setup import configure
from ci_pinpoint.models import RawLog
from ci_pinpoint.pipeline import explain
from ci_pinpoint.providers import build_chat, build_embedder
from ci_pinpoint.ranking import EmbeddingRanker
from ci_pinpoint.render import render_markdown

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "logs"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--budget", type=int)
    parser.add_argument("--embedder", help="auto (default), none, ollama, openai, fake")
    parser.add_argument("--out", type=Path, default=Path("reports/fixtures"))
    parser.add_argument(
        "--require-cited",
        action="store_true",
        help="exit 1 unless every case cites a labelled root-cause line",
    )
    args = parser.parse_args()
    configure(1, os.environ)  # warnings and info (incl. unparseable replies) to stderr

    settings = load_settings(
        overrides={
            "provider": args.provider,
            "model": args.model,
            "budget_tokens": args.budget,
            "embedder": args.embedder,
        }
    )
    chat = build_chat(settings, os.environ)
    embedder = build_embedder(settings, os.environ)
    ranker = EmbeddingRanker(embedder) if embedder is not None else None
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    args.out.mkdir(parents=True, exist_ok=True)

    rows = []
    markdown = []
    for case in manifest["cases"]:
        text = gzip.decompress((FIXTURES / case["file"]).read_bytes()).decode("utf-8", "replace")
        brief = explain(
            RawLog(
                text=text, source=case["run_url"], job_name=case["job"], job_url=case["run_url"]
            ),
            settings,
            chat,
            ranker=ranker,
        )
        selected = [t for block in brief.excerpt for _, t in block.lines]
        in_excerpt = all(any(a in t for t in selected) for a in case["anchors"])
        d = brief.diagnosis
        cited = d is not None and any(a in c.text for c in d.citations for a in case["anchors"])
        rows.append(
            {
                "case": case["file"].removesuffix(".log.gz"),
                "label": case["category"],
                "predicted": d.diagnosis.category.value if d else None,
                "root_cause_in_excerpt": in_excerpt,
                "root_cause_cited": cited,
                "verified_citations": len(d.citations) if d else 0,
                "rejected_citations": len(d.rejected_citations) if d else 0,
                "attempts": d.attempts if d else 0,
                "raw_tokens": brief.stats.raw_tokens,
                "excerpt_tokens": brief.stats.excerpt_tokens,
                "reduction": round(brief.stats.reduction, 4),
                "input_tokens": d.usage.input_tokens if d else 0,
                "output_tokens": d.usage.output_tokens if d else 0,
                "latency_s": d.latency_s if d else None,
                "error": brief.error,
            }
        )
        (args.out / f"{rows[-1]['case']}.json").write_text(brief.model_dump_json(indent=2))
        markdown.append(render_markdown(brief, show_excerpt=False))

    n = len(rows)
    summary = {
        "provider": chat.provider,
        "model": chat.model,
        "budget_tokens": settings.budget_tokens,
        "ranked_with": ranker.model if ranker and ranker.error is None else None,
        "cases": n,
        "root_cause_in_excerpt": sum(r["root_cause_in_excerpt"] for r in rows),
        "root_cause_cited": sum(r["root_cause_cited"] for r in rows),
        "category_matches": sum(r["predicted"] == r["label"] for r in rows),
        "rows": rows,
    }
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2))

    table = [
        f"#### Real CI logs: {chat.provider}/{chat.model}, budget {settings.budget_tokens} tokens, "
        f"ranking: {summary['ranked_with'] or 'heuristic only'}",
        "",
        "| case | root cause in excerpt | root cause cited | category (label → predicted) "
        "| tokens raw → excerpt | model tokens in/out | latency |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        latency = f"{r['latency_s']:.1f}s" if r["latency_s"] is not None else "-"
        table.append(
            f"| {r['case']} | {'yes' if r['root_cause_in_excerpt'] else 'no'} "
            f"| {'yes' if r['root_cause_cited'] else 'no'} | {r['label']} → {r['predicted']} "
            f"| {r['raw_tokens']:,} → {r['excerpt_tokens']:,} "
            f"| {r['input_tokens']:,}/{r['output_tokens']:,} | {latency} |"
        )
    table += [
        "",
        f"Root cause in excerpt {summary['root_cause_in_excerpt']}/{n}, "
        f"cited {summary['root_cause_cited']}/{n}, category {summary['category_matches']}/{n}.",
    ]
    report = "\n".join(table) + "\n\n" + "\n\n---\n\n".join(markdown) + "\n"
    (args.out / "report.md").write_text(report)
    print(report)
    if args.require_cited and summary["root_cause_cited"] < n:
        print(f"only {summary['root_cause_cited']}/{n} cases cite the root cause", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

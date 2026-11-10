"""JSON and Markdown reports for an evaluation run."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from ci_pinpoint import __version__
from ci_pinpoint.evaluation.dataset import Dataset
from ci_pinpoint.evaluation.harness import (
    CaseResult,
    paired_delta,
    summarise,
    summarise_diagnosis,
)
from ci_pinpoint.evaluation.strategies import BASELINES

LABELS = {
    "tail": "tail (last lines)",
    "grep": "grep -iE 'error\\|fail'",
    "pinpoint": "ci-pinpoint selection",
    "pinpoint-nofill": "ci-pinpoint, no budget fill",
    "pinpoint+emb": "ci-pinpoint + embeddings",
}


@dataclass(frozen=True, slots=True)
class RunInfo:
    split: str
    budgets: tuple[int, ...]
    main_budget: int
    strategies: tuple[str, ...]
    embedder: str | None = None
    provider: str | None = None
    model: str | None = None
    diagnose_limit: int | None = None


def build_report(dataset: Dataset, results: Sequence[CaseResult], info: RunInfo) -> dict[str, Any]:
    summary = {
        name: {str(b): asdict(summarise(results, name, b)) for b in info.budgets}
        for name in info.strategies
    }
    tools = [s for s in info.strategies if s not in BASELINES]
    deltas = [
        asdict(paired_delta(results, tool, base, info.main_budget))
        for tool in tools
        for base in info.strategies
        if base in BASELINES
    ]
    diagnosis = summarise_diagnosis(results)
    return {
        "tool": {"name": "ci-pinpoint", "version": __version__},
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "dataset": {
            "name": dataset.name,
            "license": dataset.license,
            "citation": dataset.citation,
            "split": info.split,
            "cases": len(results),
            "unlocated": len(dataset.unlocated),
        },
        "settings": {
            "budgets": list(info.budgets),
            "main_budget": info.main_budget,
            "strategies": list(info.strategies),
            "embedder": info.embedder,
            "provider": info.provider,
            "model": info.model,
            "diagnose_limit": info.diagnose_limit,
        },
        "summary": summary,
        "recall_deltas": deltas,
        "diagnosis": asdict(diagnosis) if diagnosis else None,
        "unlocated": [asdict(u) for u in dataset.unlocated],
        "cases": [
            {
                "id": r.id,
                "repo": r.repo,
                "split": r.split,
                "language": r.language,
                "raw_lines": r.raw_lines,
                "raw_tokens": r.raw_tokens,
                "label_lines": r.label_lines,
                "requirements": r.requirements,
                "scores": {
                    name: {str(b): asdict(s) for b, s in by_budget.items()}
                    for name, by_budget in r.scores.items()
                },
                "diagnosis": asdict(r.diagnosis) if r.diagnosis else None,
            }
            for r in results
        ],
    }


def _pct(value: float) -> str:
    return f"{100 * value:.1f}%"


def render_markdown(report: dict[str, Any]) -> str:
    ds = report["dataset"]
    st = report["settings"]
    main = str(st["main_budget"])
    out = [
        f"### ci-pinpoint eval: {ds['name']} ({ds['split']} split, {ds['cases']} logs)",
        "",
        f"Token budget {main} per log; every strategy is held to the same budget and the "
        "same token estimate. Recall is the share of the labelled failure lines that are shown.",
        "",
        "| strategy | failure shown | line recall | whole failure shown | precision "
        "| mean tokens | reduction vs raw log |",
        "|---|---|---|---|---|---|---|",
    ]
    for name in st["strategies"]:
        s = report["summary"][name][main]
        out.append(
            f"| {LABELS.get(name, name)} | {_pct(s['hit_rate'])} | {_pct(s['mean_recall'])} "
            f"| {_pct(s['full_recall_rate'])} | {_pct(s['mean_precision'])} "
            f"| {s['mean_tokens']:.0f} | {_pct(s['mean_reduction'])} |"
        )
    if report["recall_deltas"]:
        out += ["", "Paired difference in line recall, 95% bootstrap interval:", ""]
        for d in report["recall_deltas"]:
            out.append(
                f"- {LABELS.get(d['a'], d['a'])} vs {LABELS.get(d['b'], d['b'])}: "
                f"{100 * d['mean']:+.1f} points [{100 * d['low']:+.1f}, {100 * d['high']:+.1f}]"
            )
    budgets = [str(b) for b in st["budgets"]]
    if len(budgets) > 1:
        out += [
            "",
            "Line recall by budget (tokens):",
            "",
            "| strategy | " + " | ".join(budgets) + " |",
            "|---|" + "---|" * len(budgets),
        ]
        for name in st["strategies"]:
            cells = " | ".join(_pct(report["summary"][name][b]["mean_recall"]) for b in budgets)
            out.append(f"| {LABELS.get(name, name)} | {cells} |")
    diag = report.get("diagnosis")
    if diag:
        out += [
            "",
            f"Model diagnosis on a sample of {diag['cases']} logs "
            f"({st['provider']} / {st['model']}, budget {main}):",
            "",
            "| verified | cites a labelled line | citation precision | errors "
            "| mean latency | mean tokens in / out |",
            "|---|---|---|---|---|---|",
            f"| {_pct(diag['verified_rate'])} | {_pct(diag['cited_label_rate'])} "
            f"| {_pct(diag['mean_citation_precision'])} | {diag['errors']} "
            f"| {diag['mean_latency_s']:.1f} s "
            f"| {diag['mean_input_tokens']:.0f} / {diag['mean_output_tokens']:.0f} |",
        ]
    out += [
        "",
        f"Labels that could not be matched to their log: {ds['unlocated']} (excluded).",
        f"Dataset: {ds['citation'].rstrip('.')}. License: {ds['license']}.",
        f"ci-pinpoint {report['tool']['version']}, {report['created']}.",
    ]
    return "\n".join(out) + "\n"


def to_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=False) + "\n"

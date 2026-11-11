"""``ci-pinpoint eval``: measure line selection (and optionally diagnosis) on labelled logs."""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import TextIO

from ci_pinpoint.cache import default_cache_dir
from ci_pinpoint.config import EMBEDDERS, PROVIDERS, ConfigError, load_settings
from ci_pinpoint.evaluation.dataset import (
    SPLITS,
    Dataset,
    DatasetError,
    load_logchunks,
    load_manifest,
)
from ci_pinpoint.evaluation.fetch import fetch_logchunks
from ci_pinpoint.evaluation.harness import DiagnoseOptions, run, summarise
from ci_pinpoint.evaluation.report import RunInfo, build_report, render_markdown, to_json
from ci_pinpoint.evaluation.strategies import BASELINES, STRATEGY_NAMES, build_strategies
from ci_pinpoint.providers import build_chat, build_embedder
from ci_pinpoint.ranking import EmbeddingRanker
from ci_pinpoint.rerank import LLMRanker

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_BELOW_THRESHOLD = 1
EXIT_USAGE = 2
DEFAULT_BUDGETS = (500, 1000, 2000, 4000)


def _int_list(value: str) -> tuple[int, ...]:
    try:
        items = tuple(int(v) for v in value.split(",") if v.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"expected comma-separated integers, got {value!r}"
        ) from exc
    if not items or min(items) <= 0:
        raise argparse.ArgumentTypeError("budgets must be positive")
    return items


def _names(value: str) -> tuple[str, ...]:
    names = tuple(v.strip() for v in value.split(",") if v.strip())
    unknown = [n for n in names if n not in STRATEGY_NAMES]
    if unknown or not names:
        raise argparse.ArgumentTypeError(
            f"unknown strategy {', '.join(unknown) or '(none)'}; use {', '.join(STRATEGY_NAMES)}"
        )
    return names


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    ev = sub.add_parser(
        "eval",
        help="measure line selection against baselines on labelled logs",
        description="Score ci-pinpoint's line selection against tail and grep baselines on a "
        "labelled dataset (LogChunks by default, or a manifest.json of your own), at the same "
        "token budget. Optionally score model diagnoses on a sample.",
    )
    ev.add_argument("--dataset", choices=("logchunks", "manifest"), default="logchunks")
    ev.add_argument(
        "--data",
        type=Path,
        help="dataset directory (default for logchunks: <cache dir>/datasets/logchunks)",
    )
    ev.add_argument("--fetch", action="store_true", help="download LogChunks from Zenodo first")
    ev.add_argument("--split", choices=SPLITS, default="all", help="repository split (all)")
    ev.add_argument("--budget", type=int, help="headline token budget (2000)")
    ev.add_argument(
        "--budgets", type=_int_list, default=DEFAULT_BUDGETS, help="budget sweep, e.g. 500,1000"
    )
    ev.add_argument(
        "--strategies",
        type=_names,
        help="comma-separated: tail,grep,pinpoint (default), pinpoint-nofill, pinpoint+emb, "
        "pinpoint+llm",
    )
    ev.add_argument("--embedder", choices=EMBEDDERS, help="adds pinpoint+emb when set")
    ev.add_argument("--embed-model", help="embedding model name, e.g. nomic-embed-text")
    ev.add_argument(
        "--rerank",
        action="store_true",
        help="adds pinpoint+llm: candidate blocks re-ranked by the chat model (--provider)",
    )
    ev.add_argument(
        "--diagnose",
        type=int,
        default=0,
        metavar="N",
        help="also diagnose a deterministic sample of N logs with the model",
    )
    ev.add_argument("--provider", choices=PROVIDERS, help="model provider for --diagnose")
    ev.add_argument("--model", help="model name for --diagnose")
    ev.add_argument("--base-url", help="OpenAI-compatible base URL for --diagnose")
    ev.add_argument("--limit", type=int, help="only the first N labelled logs (quick runs)")
    ev.add_argument("--jobs", type=int, default=os.cpu_count() or 1, help="worker processes")
    ev.add_argument("--out", type=Path, default=Path("reports/eval"), help="report directory")
    ev.add_argument(
        "--fail-under",
        type=float,
        metavar="RECALL",
        help="exit 1 if the pinpoint strategy's line recall at the headline budget is below "
        "this (0-1)",
    )
    ev.add_argument("-v", "--verbose", action="count", default=0, help="log to stderr")


def _load(args: argparse.Namespace, env: Mapping[str, str]) -> Dataset:
    if args.dataset == "manifest":
        if args.data is None:
            raise DatasetError("--dataset manifest needs --data <dir with manifest.json>")
        return load_manifest(args.data, args.split, args.limit)
    data = args.data or default_cache_dir(env) / "datasets" / "logchunks"
    if args.fetch:
        fetch_logchunks(data)
    return load_logchunks(data, args.split, args.limit)


def _rerank_stats(ranker: LLMRanker) -> dict[str, float]:
    calls = max(1, ranker.calls)
    return {
        "calls": ranker.calls,
        "failures": ranker.failures,
        "mean_latency_s": round(ranker.latency_s / calls, 2),
        "mean_input_tokens": round(ranker.usage.input_tokens / calls, 1),
        "mean_output_tokens": round(ranker.usage.output_tokens / calls, 1),
    }


def run_eval(args: argparse.Namespace, env: Mapping[str, str], out: TextIO, err: TextIO) -> int:
    try:
        settings = load_settings(
            env=env,
            overrides={
                "provider": args.provider,
                "model": args.model,
                "base_url": args.base_url,
                "budget_tokens": args.budget,
                "embedder": args.embedder,
                "embed_model": args.embed_model,
            },
        )
        embedder = build_embedder(settings, env)
        ranker = EmbeddingRanker(embedder) if embedder is not None else None
        chat = build_chat(settings, env) if args.diagnose > 0 or args.rerank else None
        llm_ranker = LLMRanker(chat) if args.rerank and chat is not None else None
        names = args.strategies or (
            *BASELINES,
            "pinpoint",
            *(("pinpoint+emb",) if ranker else ()),
            *(("pinpoint+llm",) if llm_ranker else ()),
        )
        strategies = build_strategies(names, ranker, llm_ranker)
        dataset = _load(args, env)
    except (ConfigError, DatasetError, ValueError) as exc:
        print(f"ci-pinpoint: {exc}", file=err)
        return EXIT_USAGE
    if not dataset.cases:
        print(f"ci-pinpoint: no labelled logs in {dataset.root} ({args.split} split)", file=err)
        return EXIT_USAGE

    main_budget = settings.budget_tokens
    budgets = tuple(sorted({*args.budgets, main_budget}))
    log.info("evaluating", extra={"cases": len(dataset.cases), "strategies": list(names)})
    options = (
        DiagnoseOptions(
            chat=chat,
            limit=args.diagnose,
            max_output_tokens=settings.max_output_tokens,
            repair_attempts=settings.repair_attempts,
            ranker=ranker,
        )
        if chat is not None and args.diagnose > 0
        else None
    )
    results = run(
        dataset.cases,
        strategies,
        budgets,
        jobs=max(1, args.jobs),
        diagnose_options=options,
        diagnose_budget=main_budget,
    )
    if ranker is not None and ranker.error:
        print(f"ci-pinpoint: embedder failed, ranking fell back: {ranker.error}", file=err)
    info = RunInfo(
        split=args.split,
        budgets=budgets,
        main_budget=main_budget,
        strategies=tuple(names),
        embedder=ranker.model if ranker else None,
        provider=chat.provider if chat and args.diagnose > 0 else None,
        model=chat.model if chat and args.diagnose > 0 else None,
        diagnose_limit=args.diagnose or None,
        reranker=llm_ranker.model if llm_ranker else None,
        rerank_stats=_rerank_stats(llm_ranker) if llm_ranker else None,
    )
    report = build_report(dataset, results, info)
    markdown = render_markdown(report)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "eval.json").write_text(to_json(report), encoding="utf-8")
    (args.out / "eval.md").write_text(markdown, encoding="utf-8")
    print(markdown, file=out, end="")

    if args.fail_under is not None:
        # Gate on the shipped selection; ablations like pinpoint-nofill are for comparison.
        tool = (
            "pinpoint"
            if "pinpoint" in names
            else next((n for n in names if n not in BASELINES), None)
        )
        if tool is not None:
            recall = summarise(results, tool, main_budget).mean_recall
            if recall < args.fail_under:
                print(
                    f"ci-pinpoint: {tool} line recall {recall:.3f} is below {args.fail_under}",
                    file=err,
                )
                return EXIT_BELOW_THRESHOLD
    return EXIT_OK

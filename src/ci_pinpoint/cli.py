"""Command-line interface.

Exit codes for ``explain``: 0 diagnosed (or excerpt printed with --no-llm), 1 the
excerpt was built but the model could not produce a verified diagnosis, 2 bad input
or configuration. ``eval`` returns 1 when ``--fail-under`` is not met.
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from typing import BinaryIO, TextIO

from ci_pinpoint import __version__
from ci_pinpoint.config import EMBEDDERS, PROVIDERS, ConfigError, load_settings
from ci_pinpoint.evaluation import command as eval_command
from ci_pinpoint.ingest import GitHubError, GitLabError, SourceError, load_sources
from ci_pinpoint.logging_setup import configure
from ci_pinpoint.mcp_server import serve_stdio
from ci_pinpoint.pipeline import explain
from ci_pinpoint.render import FORMATS, render
from ci_pinpoint.runtime import Runtime, build_runtime

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_UNDIAGNOSED = 1
EXIT_USAGE = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ci-pinpoint",
        description="Find the log lines that broke a CI build and explain why.",
    )
    parser.add_argument("--version", action="version", version=f"ci-pinpoint {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    ex = sub.add_parser(
        "explain",
        help="diagnose a failed CI log",
        description="Diagnose a failed CI job. SOURCE is a log file (plain or .gz), '-' for "
        "stdin, a GitHub Actions run/job URL (needs GITHUB_TOKEN), or a GitLab job or "
        "pipeline URL.",
    )
    ex.add_argument("source", help="log file, '-', or https://github.com/<o>/<r>/actions/runs/<id>")
    ex.add_argument("--provider", choices=PROVIDERS, help="model provider (default: auto)")
    ex.add_argument("--model", help="model name or Azure deployment")
    ex.add_argument("--base-url", help="OpenAI-compatible base URL, e.g. http://localhost:8000/v1")
    ex.add_argument("--budget", type=int, dest="budget_tokens", help="excerpt token budget (2000)")
    ex.add_argument("--timeout", type=float, dest="timeout_s", help="per-call timeout in seconds")
    ex.add_argument("--format", "-f", choices=FORMATS, default="text", help="output format")
    ex.add_argument(
        "--embedder",
        choices=EMBEDDERS,
        help="embedder for block ranking: none (default), auto (follows --provider), ollama, ...",
    )
    ex.add_argument("--embed-model", help="embedding model name, e.g. nomic-embed-text")
    ex.add_argument(
        "--rerank",
        action="store_true",
        default=None,
        help="let the chat model re-rank candidate blocks before selection (one extra call)",
    )
    ex.add_argument("--no-llm", action="store_true", help="only select and print the log lines")
    ex.add_argument("--no-cache", action="store_true", help="ignore and don't write the cache")
    ex.add_argument(
        "--no-redact",
        action="store_true",
        help="don't redact secrets (tokens, keys, passwords) from the log before using it",
    )
    ex.add_argument("--show-excerpt", action="store_true", help="print the selected lines too")
    ex.add_argument("-v", "--verbose", action="count", default=0, help="log to stderr (-vv: debug)")
    eval_command.add_parser(sub)

    mcp = sub.add_parser(
        "mcp",
        help="run as an MCP server over stdio, for coding agents",
        description="Serve diagnose_run, get_excerpt, search_log and get_log_lines to an MCP "
        "client (Claude Code, Cursor, VS Code) over stdio. Log files are readable only under "
        "the working directory, or the directories in CI_PINPOINT_MCP_ROOTS.",
    )
    mcp.add_argument("--provider", choices=PROVIDERS, help="model provider (default: auto)")
    mcp.add_argument("--model", help="model name or Azure deployment")
    mcp.add_argument("--base-url", help="OpenAI-compatible base URL")
    mcp.add_argument("--budget", type=int, dest="budget_tokens", help="default token budget")
    mcp.add_argument("--embedder", choices=EMBEDDERS, help="embedder for block ranking")
    mcp.add_argument("--no-llm", action="store_true", help="serve excerpts and search only")
    mcp.add_argument("--no-cache", action="store_true", help="don't read or write the cache")
    mcp.add_argument("-v", "--verbose", action="count", default=0, help="log to stderr")
    return parser


def _serve_mcp(
    args: argparse.Namespace,
    env: Mapping[str, str],
    stdin: BinaryIO | None,
    stdout: TextIO | None,
    err: TextIO,
) -> int:
    try:
        settings = load_settings(
            env=env,
            overrides={
                "provider": args.provider,
                "model": args.model,
                "base_url": args.base_url,
                "budget_tokens": args.budget_tokens,
                "embedder": args.embedder,
            },
        )
    except ConfigError as exc:
        print(f"ci-pinpoint: {exc}", file=err)
        return EXIT_USAGE
    model_error: str | None = None
    try:
        runtime = build_runtime(settings, env, use_llm=not args.no_llm, use_cache=not args.no_cache)
    except ConfigError as exc:
        # Still useful without a model: excerpts and search need none.
        model_error = str(exc)
        log.warning("serving without a model", extra={"error": model_error})
        runtime = build_runtime(settings, env, use_llm=False, use_cache=not args.no_cache)
    reader = io.TextIOWrapper(stdin, encoding="utf-8", newline="\n") if stdin else None
    try:
        serve_stdio(runtime, env, stdin=reader, stdout=stdout, model_error=model_error)
    finally:
        runtime.close()
    return EXIT_OK


def run(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdin: BinaryIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    env = os.environ if env is None else env
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    args = _parser().parse_args(argv)
    configure(args.verbose, env)
    if args.command == "eval":
        return eval_command.run_eval(args, env, out, err)
    if args.command == "mcp":
        return _serve_mcp(args, env, stdin, stdout, err)

    runtime: Runtime | None = None
    try:
        settings = load_settings(
            env=env,
            overrides={
                "provider": args.provider,
                "model": args.model,
                "base_url": args.base_url,
                "budget_tokens": args.budget_tokens,
                "timeout_s": args.timeout_s,
                "embedder": args.embedder,
                "embed_model": args.embed_model,
                "redact": False if args.no_redact else None,
                "rerank": args.rerank,
            },
        )
        runtime = build_runtime(settings, env, use_llm=not args.no_llm, use_cache=not args.no_cache)
        raws = load_sources(
            args.source,
            env=env,
            api_url=settings.github_api_url,
            timeout_s=settings.timeout_s,
            stdin=stdin,
        )
    except (ConfigError, SourceError, GitHubError, GitLabError) as exc:
        if runtime is not None:
            runtime.close()
        print(f"ci-pinpoint: {exc}", file=err)
        return EXIT_USAGE

    try:
        briefs = [
            explain(raw, settings, runtime.chat, ranker=runtime.ranker, cache=runtime.cache)
            for raw in raws
        ]
    finally:
        runtime.close()
    print(render(briefs, args.format, show_excerpt=args.show_excerpt), file=out)
    if runtime.chat is None:
        return EXIT_OK
    undiagnosed = [b for b in briefs if b.diagnosis is None or not b.diagnosis.verified]
    for brief in undiagnosed:
        reason = brief.error or "no citation could be verified against the log"
        print(f"ci-pinpoint: {brief.job_name or brief.source}: {reason}", file=err)
    return EXIT_UNDIAGNOSED if undiagnosed else EXIT_OK


def main() -> None:  # pragma: no cover - thin wrapper
    sys.exit(run())

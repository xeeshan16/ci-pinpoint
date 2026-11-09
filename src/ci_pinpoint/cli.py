"""Command-line interface.

Exit codes: 0 diagnosed (or excerpt printed with --no-llm), 1 the excerpt was
built but the model could not produce a verified diagnosis, 2 bad input or
configuration.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from typing import BinaryIO, TextIO

from ci_pinpoint import __version__
from ci_pinpoint.config import PROVIDERS, ConfigError, load_settings
from ci_pinpoint.ingest import GitHubError, SourceError, load_sources
from ci_pinpoint.logging_setup import configure
from ci_pinpoint.pipeline import explain
from ci_pinpoint.providers import ChatModel, build_chat
from ci_pinpoint.render import FORMATS, render

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
        "stdin, or a GitHub Actions run/job URL (needs GITHUB_TOKEN).",
    )
    ex.add_argument("source", help="log file, '-', or https://github.com/<o>/<r>/actions/runs/<id>")
    ex.add_argument("--provider", choices=PROVIDERS, help="model provider (default: auto)")
    ex.add_argument("--model", help="model name or Azure deployment")
    ex.add_argument("--base-url", help="OpenAI-compatible base URL, e.g. http://localhost:8000/v1")
    ex.add_argument("--budget", type=int, dest="budget_tokens", help="excerpt token budget (2000)")
    ex.add_argument("--timeout", type=float, dest="timeout_s", help="per-call timeout in seconds")
    ex.add_argument("--format", "-f", choices=FORMATS, default="text", help="output format")
    ex.add_argument("--no-llm", action="store_true", help="only select and print the log lines")
    ex.add_argument("--show-excerpt", action="store_true", help="print the selected lines too")
    ex.add_argument("-v", "--verbose", action="count", default=0, help="log to stderr (-vv: debug)")
    return parser


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

    try:
        settings = load_settings(
            env=env,
            overrides={
                "provider": args.provider,
                "model": args.model,
                "base_url": args.base_url,
                "budget_tokens": args.budget_tokens,
                "timeout_s": args.timeout_s,
            },
        )
        chat: ChatModel | None = None if args.no_llm else build_chat(settings, env)
        raws = load_sources(
            args.source,
            env=env,
            api_url=settings.github_api_url,
            timeout_s=settings.timeout_s,
            stdin=stdin,
        )
    except (ConfigError, SourceError, GitHubError) as exc:
        print(f"ci-pinpoint: {exc}", file=err)
        return EXIT_USAGE

    briefs = [explain(raw, settings, chat) for raw in raws]
    print(render(briefs, args.format, show_excerpt=args.show_excerpt), file=out)
    if chat is None:
        return EXIT_OK
    undiagnosed = [b for b in briefs if b.diagnosis is None or not b.diagnosis.verified]
    for brief in undiagnosed:
        reason = brief.error or "no citation could be verified against the log"
        print(f"ci-pinpoint: {brief.job_name or brief.source}: {reason}", file=err)
    return EXIT_UNDIAGNOSED if undiagnosed else EXIT_OK


def main() -> None:  # pragma: no cover - thin wrapper
    sys.exit(run())

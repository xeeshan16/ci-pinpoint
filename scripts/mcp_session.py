"""Drive `ci-pinpoint mcp` the way a coding agent would, and record the session.

Starts the server as a subprocess, connects with the official MCP Python SDK client,
and calls diagnose_run, then search_log and get_log_lines on what the diagnosis cited.
Writes every call and the text the agent would receive to <out>/session.md. Used by
the real-model workflow to record a real-model session; works with any provider.

    uv run python scripts/mcp_session.py --provider ollama --model qwen2.5:3b \
        --log tests/fixtures/logs/smoke-test-missing-metrics.log.gz --out reports/mcp
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters


def _text(result: Any) -> str:
    return "\n".join(getattr(block, "text", "") for block in result.content)


async def _session(args: argparse.Namespace) -> list[dict[str, Any]]:
    server = [sys.executable, "-m", "ci_pinpoint", "mcp", "--no-cache", "-v"]
    if args.provider:
        server += ["--provider", args.provider]
    if args.model:
        server += ["--model", args.model]
    params = StdioServerParameters(command=server[0], args=server[1:])
    calls: list[dict[str, Any]] = []
    async with Client(params, read_timeout_seconds=args.timeout) as client:

        async def call(name: str, arguments: dict[str, Any]) -> Any:
            started = time.perf_counter()
            result = await client.call_tool(name, arguments)
            calls.append(
                {
                    "tool": name,
                    "arguments": arguments,
                    "seconds": round(time.perf_counter() - started, 1),
                    "is_error": result.is_error,
                    "text": _text(result),
                    "structured": result.structured_content,
                }
            )
            return result

        diagnosed = await call("diagnose_run", {"source": args.log})
        briefs = (diagnosed.structured_content or {}).get("briefs") or []
        diagnosis = (briefs[0].get("diagnosis") if briefs else None) or {}
        cited = [c["line"] for c in diagnosis.get("citations") or []]
        if cited:
            first = min(cited)
            await call(
                "get_log_lines", {"source": args.log, "start": max(1, first - 3), "end": first + 3}
            )
        await call("search_log", {"source": args.log, "pattern": args.search, "max_matches": 5})
    return calls


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--log", default="tests/fixtures/logs/smoke-test-missing-metrics.log.gz")
    parser.add_argument("--search", default="MISSING|error")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--out", type=Path, default=Path("reports/mcp"))
    args = parser.parse_args()
    calls = asyncio.run(_session(args))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "session.json").write_text(json.dumps(calls, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# MCP session",
        "",
        f"Server: `ci-pinpoint mcp --provider {args.provider or 'auto'}"
        + (f" --model {args.model}" if args.model else "")
        + "`, client: the official MCP Python SDK over stdio.",
        "",
    ]
    for c in calls:
        lines += [
            f"## `{c['tool']}` ({c['seconds']} s{', error' if c['is_error'] else ''})",
            "",
            "```json",
            json.dumps(c["arguments"]),
            "```",
            "",
            "````markdown",
            c["text"],
            "````",
            "",
        ]
    (args.out / "session.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    if any(c["is_error"] for c in calls):
        sys.exit(1)


if __name__ == "__main__":
    main()

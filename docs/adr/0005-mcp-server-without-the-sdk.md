# ADR 0005: A small stdio MCP server instead of the MCP SDK

- Status: accepted
- Date: 2026-11-12

## Context
Coding agents are where a CI brief is most useful: an agent that is told "CI failed" today
either asks the developer to paste the log or downloads all of it into its context. The
MCP server is the main way agents will use ci-pinpoint, so it has to install in one
`uvx` command, start fast, and work in Claude Code, Cursor and VS Code.

The official Python SDK (`mcp`) is the obvious choice. Version 2.3 brings 26 packages
with it (anyio, starlette, uvicorn, sse-starlette, jsonschema, pyjwt, a second httpx
fork, ...) for HTTP transports, OAuth, resources, prompts, sampling and the 2026-07-28
"modern" protocol. ci-pinpoint's own runtime dependencies are httpx and
pydantic. The SDK has also changed its public API between major versions, and the CLI
would carry that churn for a feature most CLI users never run.

What ci-pinpoint needs from MCP is narrow: stdio transport, the `initialize` handshake,
`tools/list`, `tools/call`, progress notifications and cancellation. That is a small,
stable part of the specification.

## Decision
Implement that subset directly (`ci_pinpoint/mcp_server/protocol.py`, about 300 lines):

- JSON-RPC 2.0, one message per line; stdout carries protocol messages only, logs go to
  stderr.
- Handshake versions 2024-11-05, 2025-03-26, 2025-06-18 and 2025-11-25. The reply echoes
  the client's version when supported, otherwise the latest. Titles, `outputSchema` and
  `structuredContent` are sent only from 2025-06-18, annotations from 2025-03-26.
- `server/discover` (the 2026-07-28 probe) answers "method not found", which the
  specification and the SDK treat as "legacy server, use the handshake".
- Tool calls run on daemon threads (at most four at once), so `ping` and quick tools are
  answered while a slow model call runs. A cancelled call's reply is dropped.
- Bad arguments are JSON-RPC "invalid params"; problems the agent can fix (an unknown
  job, a bad regex, a file outside the roots, a fetch error) are tool results with
  `isError: true` and a message that says what to do.

Interoperability is tested, not assumed: the suite starts `ci-pinpoint mcp` as a
subprocess and drives it with the official SDK client (`mcp.Client`, which probes
`server/discover` first and falls back), checking the handshake, tool schemas, progress,
structured output validated against the declared `outputSchema`, and tool errors. The SDK
is a development dependency only.

## Consequences
- `uvx --from git+... ci-pinpoint mcp` keeps the two direct runtime dependencies (httpx,
  pydantic) instead of adding 26 packages, and the server starts in under half a second.
- When a client needs something outside the subset (Streamable HTTP, resources, the
  modern protocol only), the SDK is the way to add it; the tool layer (`tools.py`) does
  not depend on the transport and would move over unchanged.
- The protocol code is ours to maintain. The SDK client test in CI is the guard: if a
  newer SDK stops talking to the server, CI fails.

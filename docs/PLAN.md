# Plan: CI Pinpoint, 7 days

Goal: an open-source tool that finds the exact lines in a failed CI log that broke
the build, explains why with line citations checked against the log, and hands a
coding agent a few-KB brief instead of a multi-MB log. Works with local models.

Proposed 2026-11-02, started 2026-11-09. One milestone per day.

## Day 1: first end-to-end slice
Packaging (uv, a console script, ruff, mypy strict, pytest with coverage). An LLM
and embedding provider interface with OpenAI-compatible (Ollama and vLLM), OpenAI,
Anthropic and Azure providers, plus a deterministic fake for tests. Log ingest from
a file, from stdin, or from a public GitHub Actions run URL (failed jobs only). A
normaliser that strips ANSI codes and timestamps and splits on step and group
markers. A first `ci-pinpoint explain` that turns heuristic candidates into an LLM
diagnosis and validates every line citation. Tests on real public logs, CI, a README
skeleton, PLAN and PROGRESS, LICENSE, CONTRIBUTING, CHANGELOG and templates.

## Day 2: retrieval core
Drain-style template mining to collapse repeated lines, block chunking, embedding
ranking (local embedder or Ollama), selecting context to fit a token budget, and an
on-disk cache keyed by log hash.

## Day 3: eval harness
A LogChunks loader (with license attribution). Three baselines: tail-N, grep
error/fail, and heuristics only. Metrics: chunk line recall and precision at a fixed
budget, and token reduction. One command: `ci-pinpoint eval`, with JSON and Markdown
reports.

## Day 4: diagnosis quality and safety
An LLM rerank stage, a failure-category taxonomy, a schema repair pass, and citation
verification that rejects any line not present in the log. Log text is handled as
untrusted input (prompt-injection hardening), and secrets are redacted before any
prompt. Measured against embedding-only ranking.

## Day 5: where developers work
An MCP server (stdio) with `diagnose_run`, `get_excerpt` and `search_log`, plus
ready-to-paste configs for Claude Code, Cursor and VS Code. A GitHub Action
(`action.yml`) that writes the brief to the job summary, with an example workflow.
GitLab job log support.

## Day 6: production concerns
OpenTelemetry traces and metrics, structured logs, timeouts and retries with backoff,
token and cost accounting, concurrent multi-job runs, and a Docker image built and
smoke-tested in CI. A CI job that runs the eval on a real Ollama model. ADRs and a
threat model.

## Day 7: finish
Measured results table (tool vs baselines, with dataset, provider, model and
settings), real sample output, README polish with a Mermaid diagram, ROADMAP,
CHANGELOG, a clean-clone quick start check, the `v0.1.0` tag, and marking the
project completed.

## Notes on the plan
- Day 1 already covers part of Day 4 (schema repair and citation rejection) and
  Day 6 (retries with backoff, structured logs), because the first slice is not
  useful without them. Those days build on it: Day 4 adds the rerank, the
  injection test suite and secret redaction; Day 6 adds tracing and metrics.
- Zenodo (where LogChunks is published) is not reachable from the build sandbox, so the
  LogChunks eval runs in CI (dataset cached with actions/cache); the harness itself is
  tested locally on synthetic LogChunks-shaped data and the six fixtures.
- Day 3 also added the budget fill to selection, because the first eval run showed the
  scored blocks under-spending the budget. Day 4's measurements use the dev split.
- Day 4 measured the LLM re-rank on a deterministic 60-log sample of the dev split rather
  than all 189 logs: a full run needs about 56 s per re-rank call on a CPU runner and did not
  fit the workflow's time limit. The diagnosis-prompt changes were checked on the six real
  logs and on the same 20 test-split logs as day 3 (before/after, not tuned on them).

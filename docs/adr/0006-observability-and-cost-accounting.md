# ADR 0006: Optional OpenTelemetry, built-in cost accounting

- Status: accepted
- Date: 2026-11-13

## Context
ci-pinpoint runs in two places where operators ask different questions. In CI it runs
once per failed build: "how long did the model take, and what did this cost?" As an MCP
server for a coding agent it runs many times in one process: "which stage is slow, how
many tokens am I spending, is a provider degrading?" Both need the pipeline to report its
own work — stage latency, model tokens, cost — without the caller instrumenting it by
hand.

Two tensions shaped the decision:

1. **No new always-on dependency.** The runtime depends on two packages (httpx, pydantic);
   ADR 0005 kept the MCP server off the 26-package SDK for the same reason. A full
   OpenTelemetry install is heavier than the tool itself, and most CLI users will never
   wire up a collector.
2. **Cost must be honest.** Model prices change and vary by contract. A number in the
   brief that silently goes stale, or that pretends a local model costs money, is worse
   than no number.

## Decision

**Token and cost accounting is built in and always on.** Every brief that ran a model
carries an `Accounting` with the chat model's input and output tokens (diagnosis plus any
re-ranking) and an estimated USD cost. The text and Markdown briefs show a one-line
summary. Pricing comes from a small table of *indicative published list prices*, matched
by the longest model-name substring; local providers (Ollama, the fake test provider) are
free; and an unknown hosted model shows no cost rather than a guess. `CI_PINPOINT_PRICE_IN`
and `CI_PINPOINT_PRICE_OUT` (USD per million tokens) override the table and always win, so
a team prices with its own contract. Embedding calls are not counted — embedders report no
token usage, and saying so is more honest than inventing a number.

**OpenTelemetry is optional and off by default.** It lives behind the `otel` extra and a
thin seam (`telemetry.py`): a no-op base class whose `stage()` context manager and
`record_brief()` do nothing, and an OTel-backed subclass built only when
`CI_PINPOINT_OTEL=1` and the extra is installed. The pipeline always calls the seam; with
telemetry off the calls cost nothing. When on, each stage (normalize, select, diagnose)
is a span with latency, the diagnose span carries provider, model and attempts, and
counters track model calls, input/output tokens and cost, plus a stage-latency histogram.
Spans and metrics go to an OTLP collector when `OTEL_EXPORTER_OTLP_ENDPOINT` is set, else
to the console. Log text is never put on a span — attributes are metadata only (source,
model, counts), never log lines, which keeps the untrusted-input rules of ADR 0004 intact
in traces.

## Consequences
- A developer sees what a diagnosis cost without any setup, and can trust it: it is based
  on list prices (overridable), local models read "free", and unknown models read "cost
  n/a" instead of a fabricated figure.
- Teams running the MCP server or the Action at scale get real traces and metrics by
  installing one extra and setting two env vars; everyone else carries no OpenTelemetry
  code paths at runtime.
- The accounting captures the chat model only. If an embedding provider ever reports
  usage, it joins the same `Accounting`; until then the brief says what it does and does
  not count.
- The price table will drift from real prices over time. The override is the contract-
  accurate path, and the table is documented as indicative; the README repeats this so no
  one treats a printed cost as a quote.

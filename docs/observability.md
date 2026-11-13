# Observability and cost

ci-pinpoint reports its own work: what each diagnosis cost, and — when you ask for it —
traces and metrics for every stage and model call.

## Cost and token accounting (always on)

Every brief that ran a model carries the chat model's token usage and an estimated cost.
The text and Markdown output show it on one line:

```
  excerpt: 1,980 of ~14,800 tokens (86.6% smaller), 8 block(s)
  model: 2,970 in / 359 out tokens · ~$0.0018 (gpt-4.1-mini)
```

and the JSON brief carries it as `accounting`:

```json
"accounting": {
  "provider": "openai", "model": "gpt-4.1-mini",
  "input_tokens": 2970, "output_tokens": 359, "cost_usd": 0.001762
}
```

The usage covers the diagnosis and any `--rerank` call. Embedding calls are **not**
counted — embedders report no token usage.

**Pricing is indicative, and overridable.** The built-in table holds published list prices
(USD per million tokens) for common OpenAI and Anthropic models, entered by hand and easy
to fall out of date. Local providers (Ollama, the fake test provider) are free, and an
unknown hosted model shows `cost n/a` rather than a guess. To price with your own
contract, set the override — it always wins:

```bash
export CI_PINPOINT_PRICE_IN=0.40    # USD per 1M input tokens
export CI_PINPOINT_PRICE_OUT=1.60   # USD per 1M output tokens
```

A printed cost is an estimate, not a quote.

## OpenTelemetry traces and metrics (opt-in)

Tracing is off by default and behind an extra, so the core keeps no OpenTelemetry
dependency. Install the extra and turn it on:

```bash
pipx install "ci-pinpoint[otel]"        # or: uv tool install "ci-pinpoint[otel] @ git+..."
export CI_PINPOINT_OTEL=1
ci-pinpoint explain build.log --provider ollama
```

With no collector configured, spans and metrics print to the console (handy for a first
look). Point them at a collector with the standard variable:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
```

### What is recorded

Spans, per run:

- `normalize`, `select`, `diagnose` — one per stage, each tagged with `source` and
  carrying `duration_ms`.
- the `diagnose` span also carries `provider`, `model`.

Metrics:

| Instrument | Type | Labels |
|---|---|---|
| `ci_pinpoint.model.calls` | counter | provider, model |
| `ci_pinpoint.model.tokens` | counter (tokens) | provider, model, direction (input/output) |
| `ci_pinpoint.model.cost_usd` | counter (USD) | provider, model |
| `ci_pinpoint.diagnoses` | counter | provider, model, verified |
| `ci_pinpoint.stage.duration` | histogram (ms) | stage |

This is most useful for the MCP server and the GitHub Action, which run many diagnoses:
you get stage latency, token spend and cost over time, and a signal when a provider
degrades.

**Log text is never placed on a span.** Span attributes are metadata only — source, model,
counts — so the untrusted-input and secret-redaction guarantees (see the
[threat model](threat-model.md)) hold in traces too.

## Structured logs

Independently of OpenTelemetry, set `CI_PINPOINT_LOG_FORMAT=json` for JSON logs on stderr
(stdout stays reserved for the brief), and `-v` / `-vv` to raise the level.

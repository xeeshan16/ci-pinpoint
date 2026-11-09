# ADR 0001: Select lines without a model, then diagnose with one, then verify

- Status: accepted
- Date: 2026-11-09

## Context
Failed CI logs run from thousands to hundreds of thousands of lines (one fixture is
235k estimated tokens). Small local models have 8k to 32k context windows and get
worse as the context fills. Hosted models can take more, but cost grows with it and
coding agents pay for every token they read. The line that explains a failure is
often not the last line, and often contains no "error" word (`MISSING
gen_ai_request_model`, a refused connection logged at INFO, the deepest `Caused by`).

## Decision
Three stages with a hard boundary between them:

1. **Select** (no model): normalise the log, score lines with explainable signals,
   build blocks, and choose them under a token budget. Deterministic and cheap, and
   it is also a baseline we can measure.
2. **Diagnose** (model): the model sees only the excerpt, with original line
   numbers, and must return a fixed JSON schema with citations.
3. **Verify** (no model): every citation is checked against the raw log. Wrong
   numbers with a unique verbatim match are corrected; anything else is dropped.
   With no verified citation left, the tool asks once more, then says so.

## Consequences
- The model can never make the user read a line that is not in their log.
- Recall is capped by stage 1: if selection misses the line, the model cannot cite
  it. That is why selection gets its own eval (Day 3) and an embedding ranker
  (Day 2), and why the tail of every failed step is always included.
- Users without a model still get value (`--no-llm`), and CI can test the whole
  path with a deterministic fake.

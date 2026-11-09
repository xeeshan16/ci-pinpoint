# Progress

## Day 1: 2026-11-09, first end-to-end slice

**Shipped**
- Packaging with uv and hatchling, a `ci-pinpoint` console script, ruff (lint and
  format), mypy strict over src, tests and scripts, pytest with branch coverage
  (gate at 85%).
- `normalize`: strips the BOM, GitHub timestamps, ANSI codes and carriage-return
  redraws; splits on `##[group]`, `::group::` and GitLab `section_start` markers;
  recognises `##[error]` and `::error ...::` annotations; marks the script GitHub
  echoes inside a `Run ...` group as a command, not output. Line numbers always
  refer to the raw log (this caught a real bug: `str.splitlines` also splits on
  `\r`, which shifted every line number after a progress bar).
- `heuristics`: weighted line signals (exceptions, `Caused by`, compiler errors,
  test failures, OOM, refused connections, `MISSING`, "cannot/invalid/unknown"),
  negative patterns for lines that only look like errors (`Failures: 0, Errors: 0`,
  `-Werror`, `error_type="none"`), build-tool boilerplate filtered out, WARN/INFO
  log levels dampened, repeated templates dampened, stack frames compacted. Seeds
  become windows, windows merge into blocks, and the tail of every failed step is a
  block. Blocks are chosen greedily under a token budget with a per-block cap and a
  novelty check, so the 19th copy of the same `Caused by` does not crowd out
  everything else.
- Providers behind two protocols (`ChatModel`, `Embedder`): OpenAI-compatible
  (Ollama, vLLM, LM Studio), OpenAI, Azure OpenAI, Anthropic, plus deterministic
  fakes. Shared HTTP client with timeouts and full-jitter exponential backoff that
  respects `Retry-After` and retries only 408/409/425/429/5xx and transport errors.
- Diagnosis: a fenced prompt that treats the log as untrusted data, JSON output
  parsed tolerantly (code fences, chatter, `"L123"` line numbers, "85%"
  confidence), validated with pydantic, one repair round trip with the exact
  validation error, and citation verification: each cited line must exist, must
  have been shown to the model, and must match the log text; a wrong number with
  a unique verbatim match is corrected and marked as such; everything else is
  dropped and reported.
- Ingest: files (plain or gzip), stdin, and GitHub Actions run, attempt and job
  URLs (failed jobs only, paginated, follows the log redirect, clear error when
  no token is set).
- `ci-pinpoint explain` with text, Markdown and JSON output, `--no-llm` for the
  excerpt alone, exit codes 0/1/2, structured logs to stderr (JSON on request),
  settings from `.ci-pinpoint.toml`, env vars and flags.
- Six real, unmodified failed GitHub Actions job logs from xeeshan16/kafka-llm-enricher
  as fixtures, hand-labelled with the root-cause line(s): a Kafka listener
  misconfiguration, a refused Postgres connection, a config error buried under 19
  failing tests, a smoke test that fails on two `MISSING` metrics with no error
  word at all, and two formatter failures.
- CI: lint/format/types, tests on Python 3.11 to 3.13 with coverage and JUnit
  reports, a live test against the real GitHub API, and a package job that installs
  the wheel into a clean venv and runs it on a real log. A separate `real-model`
  workflow runs the fixtures through a real model (Ollama, qwen2.5:3b on CPU).

**Measured today** (local, not an evaluation: six cases, written by me)
- The labelled root-cause lines are inside the excerpt for 6/6 logs at budgets of
  800, 2,000 and 4,000 tokens (enforced by a test).
- Excerpt size at the default 2,000-token budget, from estimated raw size:
  15,311 → 1,995; 14,839 → 2,000; 161,427 → 1,997; 63,069 → 961; 235,399 → 1,995;
  9,702 → 359 tokens.
- 135 tests pass locally and in CI on Python 3.11 to 3.13 (including a live GitHub
  API test in CI), coverage 95.6%.

**Real model runs** (Ollama `qwen2.5:3b`, CPU-only GitHub runner, budget 2,000
tokens, temperature 0, one repair; measured in CI because the build sandbox cannot
reach a model; six logs, so read these as a smoke test)

| run | change under test | verified diagnosis | labelled root cause cited | category correct |
|---|---|---|---|---|
| [42662551838](https://github.com/xeeshan16/ci-pinpoint/actions/runs/42662551838) | first slice, JSON mode | 0/6 | 0/6 | 0/6 |
| [42664660125](https://github.com/xeeshan16/ci-pinpoint/actions/runs/42664660125) | + json_schema structured output, nested-field lifting | 4/6 | 3/6 | 1/6 |
| [42667018690](https://github.com/xeeshan16/ci-pinpoint/actions/runs/42667018690) | + cut-off detection, citation clipping, shorter root cause | 5/6 | 4/6 | 1/6 |

What the runs taught us, each fixed in its own commit:
1. The 3B model nested `citations` and `confidence` inside `root_cause`, so strict
   validation rejected every reply. Fix: send the schema as a `json_schema`
   response format (constrained decoding on Ollama, vLLM, OpenAI and Azure, with a
   fallback to JSON mode) and lift nested fields without inventing content.
2. A reply that hit the output limit still contained a complete citation object;
   the parser took that inner object for the answer. Fix: only the outermost object
   counts, and the repair message names the output limit.
3. Still open: on the Kafka listener log the model loops until the 1,024-token
   limit on both attempts; categories are mostly wrong (`test` or `compile` for
   configuration and infrastructure failures). Day 4 adds the taxonomy and rerank.
   Latency is 40 to 140 s per log on a CPU runner.

**Not yet**
- The excerpt for the Spring log still includes two near-duplicate assertion
  blocks; template mining (Day 2) should fold them.

**Next (Day 2):** Drain-style template mining, block chunking, embedding ranking,
token-budgeted context selection and an on-disk cache.

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

## Day 2: 2026-11-09, retrieval core

Built the same day as day 1: the routine was started again by hand after day 1 had
already shipped, and its rule is to build the next unbuilt milestone rather than
redo one. The plan is unchanged; the project is one day ahead of the calendar.

**Shipped**
- `templates`: a simplified Drain miner (fixed-depth prefix tree, position-wise
  similarity, `<*>` wildcards) with masking for timestamps, hashes, UUIDs, numbers
  and sizes. The 15,687-line Maven log becomes 291 templates in about 0.15 s.
- Selection uses templates three ways: weak lines with very common templates are
  dampened (strong ones are not: the same `Caused by` under 19 failing tests is the
  common cause, not noise); block novelty is judged by evidence templates; and runs of
  same-template filler inside a block are folded into one line plus
  `... N more line(s) like: <template>`, shown to the model and in the output.
- Block chunking: windows never cross a step boundary; lines longer than 400
  characters are clipped in excerpts (citations of a clipped line still verify); a
  block may always keep its single best line, even past its budget share.
- `ranking`: optional embedding re-ranking that blends the heuristic score with
  similarity to eleven short failure-mode descriptions and to the failed step's tail.
  Falls back to heuristic order if the embedder fails; in `auto` mode it does not
  retry, so a missing model costs one fast call.
- `cache`: verified briefs on disk (atomic JSON, keyed by log hash plus every setting
  that changes the answer) and embedding vectors in SQLite keyed by model and text
  digest. `--no-cache`, `CI_PINPOINT_CACHE_DIR`, and a read-only cache directory
  degrades to no cache instead of failing.
- `--embedder`, `--embed-model`, and `ranked with ...` / `from cache` in the output.
- `scripts/selection_report.py`: block rank of the root cause (MRR) and budget recall,
  heuristic vs embeddings. CI runs it with the deterministic embedder; the
  real-model workflow runs it with `nomic-embed-text`.

**Measured** (the six labelled real logs; also the logs these changes were tuned on,
so treat as a smoke test until the LogChunks eval)

Root cause kept in the excerpt, by token budget:

| selection | 150 | 200 | 300 | 400 | 600 | 800 |
|---|---|---|---|---|---|---|
| day 1 heuristics (local run) | - | 4/6 | 4/6 | 4/6 | 6/6 | 6/6 |
| day 2 templates + folding + clipping (CI run 42674132270) | 5/6 | 6/6 | 6/6 | 6/6 | 6/6 | 6/6 |
| day 2 + `nomic-embed-text` re-ranking (same run) | 5/6 | 5/6 | 6/6 | 6/6 | 6/6 | 6/6 |

- Mean reciprocal rank of the root-cause block among non-tail candidates: 0.833
  heuristic, 0.833 with `nomic-embed-text`. Embeddings moved the smoke test's cause
  from rank 2 to 1 and the Spring cause from 1 to 2. No gain, so ranking is **off by
  default** ([ADR 0002](adr/0002-template-mining-and-optional-embedding-ranking.md)).
- Diagnosis with the default settings, qwen2.5:3b on Ollama, CPU runner
  ([run 42674132270](https://github.com/xeeshan16/ci-pinpoint/actions/runs/42674132270)):
  verified diagnosis 6/6 (day 1's last run: 5/6), labelled root cause cited 5/6
  (4/6), category 2/6 (1/6). One run each, so model variance is not separated from
  the change.
- 189 tests pass locally and in CI on Python 3.11 to 3.13; coverage 96%.

**Found along the way**
- An `EmbeddingStore` leaked its SQLite connection when the source failed to load
  after the store was opened; fixed, and the suite now runs clean with
  `-W error::ResourceWarning`.
- Raw-log token estimates briefly used clipped line lengths (63,069 became 52,732
  for one log); raw size now uses unclipped lines, excerpts use clipped ones.
- When `ci` and `real-model` started on the same push, GitHub left one or more `ci`
  jobs unscheduled and failed the run without logs (three times, a different job
  each time). Dispatched alone, the same commit was green
  ([run 42676225715](https://github.com/xeeshan16/ci-pinpoint/actions/runs/42676225715)).
  `real-model` now runs after `ci` succeeds on main instead of in parallel.

**Next (Day 3):** the evaluation harness: a LogChunks loader (fetched in CI, since
Zenodo is blocked from the build sandbox), tail-N, grep and heuristics-only baselines,
chunk line recall and precision at a fixed budget, token reduction, and
`ci-pinpoint eval` with JSON and Markdown reports.

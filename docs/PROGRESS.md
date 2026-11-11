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

## Day 3: 2026-11-10, evaluation harness

**Shipped**
- `ci-pinpoint eval`: one command that scores line selection against baselines on a
  labelled dataset and writes `eval.json` (per-log scores) and `eval.md`.
  - Datasets: LogChunks (797 Travis CI logs from 80 repositories, CC BY 4.0), downloaded
    from Zenodo on demand with `--fetch`, checksum-verified, unzipped with a path check;
    or any `manifest.json` of labelled logs (the six GitHub Actions fixtures use it).
  - Label alignment: LogChunks labels are text with escape bytes removed, so `[37m`
    remnants, blank lines from `\r\r\n` endings and partial first lines are matched back to
    raw line numbers, anchored on the most distinctive chunk lines. 773 of 797 align; the
    24 that don't are listed with how much was found (among them chunks of up to 5,696
    lines, and six one-line chunks from one repository that are not found in their logs).
  - Strategies at the same budget and token estimate: `tail`, `grep -iE 'error|fail'`,
    `pinpoint`, `pinpoint-nofill` (ablation) and `pinpoint+emb`.
  - Metrics: failure shown, line recall, whole failure shown, precision, tokens, reduction,
    a paired bootstrap 95% interval on the recall difference, a budget sweep.
  - Repository split (SHA-256 of the repo, 3 in 10 to dev): 189 dev, 584 test logs.
  - `--diagnose N` scores model diagnoses on a deterministic sample (verified, cites a
    labelled line, citation precision, latency, tokens); `--fail-under` gates CI.
- Travis CI support in the normaliser (folds, `$ command` steps, the failed-command line,
  Travis's post-failure boilerplate), needed because LogChunks is Travis.
- Budget fill: leftover budget goes to the failed step's output, nearest the failure first,
  as one block sized after folding and frame compaction (binary search on the number of
  lines), never re-adding lines a chosen block deliberately left out.
- CI runs the eval on every push (dev, all, test, and the fixtures), gated at line recall
  0.75 on all logs. A manual `eval` workflow adds Ollama embeddings and model diagnosis.
- ADR 0003 on the evaluation method.

**Measured** (LogChunks, 2,000-token budget, no model, deterministic;
[CI run 42822696413](https://github.com/xeeshan16/ci-pinpoint/actions/runs/42822696413))

| split | strategy | failure shown | line recall | whole failure | mean tokens |
|---|---|---|---|---|---|
| test (584) | tail | 69.7% | 64.5% | 60.8% | 1,983 |
| test (584) | grep | 56.5% | 18.5% | 5.1% | 757 |
| test (584) | pinpoint, no fill | 81.5% | 60.5% | 42.3% | 895 |
| test (584) | pinpoint | 91.6% | 78.3% | 64.9% | 1,613 |
| dev (189) | tail | 72.5% | 68.9% | 66.7% | 1,945 |
| dev (189) | pinpoint, no fill | 77.2% | 61.7% | 49.7% | 1,060 |
| dev (189) | pinpoint | 84.1% | 71.2% | 64.0% | 1,484 |

- Recall difference vs tail (test): +13.8 points, 95% interval +10.4 to +17.2. On dev:
  +2.3, interval -2.5 to +7.1, so not significant there.
- Budget sweep, all 773 logs, line recall: pinpoint 53.8 / 67.4 / 76.6 / 81.9% at
  500 / 1,000 / 2,000 / 4,000 tokens; tail 41.3 / 53.9 / 65.6 / 74.5%.
- The six GitHub Actions fixtures: pinpoint 6/6 at every budget; tail 1/6 at 2,000 tokens,
  grep 5/6.
- 227 tests pass locally and in CI on Python 3.11 to 3.13
  ([CI run 42825005092](https://github.com/xeeshan16/ci-pinpoint/actions/runs/42825005092)).

**Model diagnosis on LogChunks** (first real-model numbers on the public set: Ollama
`qwen2.5:3b` on a CPU-only runner, 2,000-token budget, temperature 0, one repair; a
deterministic sample of 20 test-split logs;
[eval run 42823682451](https://github.com/xeeshan16/ci-pinpoint/actions/runs/42823682451))

| verified diagnosis | cites a labelled line | citation precision | errors | mean latency | tokens in / out |
|---|---|---|---|---|---|
| 17/20 (85%) | 9/20 (45%) | 34.9% | 2 | 47 s | 2,621 / 326 |

The excerpt contains the labelled failure for about 9 in 10 logs, but a 3B model cites it
in fewer than half: the gap between "the right lines reach the model" and "the model
points at them" is now the biggest loss, and it is what day 4 works on. Twenty logs, one
run: a direction, not a precise rate.

An embedding re-ranking run over all 773 logs (`nomic-embed-text` on the runner's CPU,
[eval run 42823687484](https://github.com/xeeshan16/ci-pinpoint/actions/runs/42823687484))
was still running when this entry was written; day 4 records its result next to the LLM
rerank.

**What the eval taught us**
1. The first run (before the fill) said the day 2 selection *lost* to plain tail on line
   recall (60.8% vs 65.6%, all logs), while finding the failure more often (80.5% vs
   70.4%) with half the tokens: it stopped at about 940 tokens of a 2,000 budget, and
   LogChunks failures sit in the failing step's last lines more often than not. The fill
   turned that into a 13.8-point lead on test. That first run included the test
   repositories, so the change was motivated by test data in aggregate (it was not fitted
   to individual labels); the dev split, where the gain is smaller, is the more cautious
   number. Tuning from here looks at dev only.
2. Grep is a weak baseline on real logs: most lines that say "error" or "fail" are not the
   failure, and the chunk is often a test assertion or a compiler message without either
   word.
3. Precision is low for every strategy (13 to 18%) because a 2,000-token excerpt is larger
   than most chunks. That is the model's job on day 4: rerank and cite.

**Found along the way**
- The first version of the CI gate judged the first non-baseline strategy, which was the
  `pinpoint-nofill` ablation, and failed CI on `23a7bf6` although `pinpoint` was above the
  threshold. Fixed in the next commit with a test that only the budget fill can pass.
- Zenodo is blocked from the build environment, so the harness was developed against
  LogChunks-shaped test data and run on the real dataset in CI only.

**Housekeeping**
- A stray `probe` branch exists on the repository from a dataset-inspection experiment;
  it could not be deleted from the build environment and can be removed.

**Next (Day 4):** LLM rerank of candidate blocks, a failure-category taxonomy, prompt-
injection hardening with a test suite, secret redaction before any prompt, measured on the
dev split against embedding-only ranking.

## Day 4: 2026-11-11, diagnosis quality and safety

**Shipped**
- `redact`: secrets are replaced with `[REDACTED:<kind>]` right after normalisation, so
  selection, prompts, the cache and every output only see redacted text. Known token formats
  (GitHub, GitLab, AWS, Slack, Google, Stripe, npm, OpenAI/Anthropic-style keys), JWTs,
  credentials in URLs, authorization headers, secret-named assignments whose value looks like
  a secret, and whole PEM private key blocks. Line numbers never change. The brief reports
  counts by kind; `--no-redact` turns it off. None of the six real logs has anything to redact
  (a test checks that), so it costs no evidence there.
- `injection`: the excerpt is fenced with a random-nonce tag, look-alike tags are defused,
  instruction-like lines are named to the model as data and **can never be accepted as
  citations**, and the validated answer is checked: echoing injected text or pointing the fix
  at a host only injected lines mention adds a warning and caps confidence at 0.3; a
  pipe-to-shell or destructive fix is flagged either way. `untrusted_lines` and `warnings` are
  in the brief. 25-payload test suite with benign look-alikes and an obeying model end to end.
- `taxonomy`: definitions for every category and tie-break rules ("name the cause, not the
  reporter") in the prompt; the fake provider uses the same cause-before-symptom rules.
- `rerank`: `--rerank` lets the chat model order up to eight candidate blocks against the
  symptom in one short schema-constrained call, blended with the heuristic score, failed-step
  tails untouched, heuristic fallback on any failure. Chains after embedding ranking.
- Eval: `--rerank` adds a `pinpoint+llm` strategy; reports add paired differences between
  ci-pinpoint variants at every budget, re-rank cost, and the injection detector's hits on
  LogChunks as a false-positive check (with examples).
- `scripts/injection_report.py`: three payloads planted in the six real logs, run through the
  real model in the `real-model` workflow.
- ADR 0004 (logs are untrusted input).

**Measured**

*Block re-ranking* (LogChunks dev split, a deterministic 60-log sample, line recall; Ollama
`qwen2.5:3b` for the LLM re-rank, `nomic-embed-text` for embeddings, CPU runner;
[eval run 43051770635](https://github.com/xeeshan16/ci-pinpoint/actions/runs/43051770635)):

| strategy | 500 | 1,000 | 2,000 | 4,000 |
|---|---|---|---|---|
| tail | 46.3% | 54.2% | 65.3% | 68.3% |
| ci-pinpoint (heuristics) | 56.5% | 61.7% | 69.5% | 77.3% |
| + embeddings | 56.5% | 61.6% | 71.1% | 78.9% |
| + LLM re-rank | 55.1% | 60.4% | 69.4% | 77.3% |

- LLM re-rank vs heuristics: -1.5 points at 500 tokens (95% interval -3.9 to +0.2), -1.3 at
  1,000 (-3.2 to -0.1), no difference at 2,000 and 4,000. Against embeddings it is behind at
  every budget (-1.3 to -1.7). Embeddings vs heuristics: +1.6 at 2,000 (-0.2 to +5.0).
- Cost: 50 calls for 60 logs (the rest had nothing to reorder), 4 replies unusable and fell
  back to heuristic order, 30.6 s and 1,512 input tokens per call on a CPU runner.
- **Decision: `--rerank` stays off by default.** A 3B model does not order failure blocks
  better than the heuristics it is blended with, and it costs a model call per log. The
  feature stays because the measurement is one flag away for larger models
  (`ci-pinpoint eval --rerank --provider openai`), which day 6 can run if a key is
  available. Embeddings remain the better optional ranker, and still a small gain.
- The first attempt, all 189 dev logs, hit the workflow's 150-minute limit after 156 re-rank
  calls (about 56 s each while embeddings shared the CPU) and lost its report; `--sample`
  exists because of it.

*Prompt injection* (Ollama `qwen2.5:3b`, CPU runner; 6 real logs x 3 payloads inserted just
before the step's `##[error]` line; one run each):

| run | obeyed (raw replies) | caught by guards | silent (reached the brief unflagged) | root cause still cited |
|---|---|---|---|---|
| [43026838329](https://github.com/xeeshan16/ci-pinpoint/actions/runs/43026838329) (prompt v3) | 6/18 | 5/18 | 0/18 | 7/18 |
| [43036195150](https://github.com/xeeshan16/ci-pinpoint/actions/runs/43036195150) (prompt v4) | 11/18 | 10/18 | 0/18 | 7/18 |

The detector flagged all 18 payload lines. A 3B model follows the payload often (the
"override" and "fix" payloads 5 of 6 times in the second run), but no obeyed payload reached
the brief without a warning or a dropped citation. The cost is availability: with a payload in
the log the labelled root cause is still cited in only 7 of 18 runs (vs 4 of 6 without), so an
attacker can make a diagnosis worse, just not silently wrong. "Obeyed" counts raw replies
including ones repaired afterwards, so it is an upper bound.

*Diagnosis on the six real logs* (same model, 2,000 tokens, one run each):

| run | prompt | diagnosed | root cause cited | category | longest reply (tokens) | latency per log |
|---|---|---|---|---|---|---|
| day 3, [42829320718](https://github.com/xeeshan16/ci-pinpoint/actions/runs/42829320718) | v2 | 5/6 (one cut off twice) | 4/6 | 1/6 | 1,024+ | 115 to 160 s |
| [43026838329](https://github.com/xeeshan16/ci-pinpoint/actions/runs/43026838329) | v3: + fence, flagged lines, taxonomy | 6/6 | 3/6 | 2/6 | 1,394 (cut off, repaired) | 63 to 142 s |
| [43036195150](https://github.com/xeeshan16/ci-pinpoint/actions/runs/43036195150) | v4: + evidence before category, 120-char citations | 6/6 | 4/6 | 2/6 | 494 | 48 to 72 s |

- The taxonomy did not fix categories for a 3B model: v3 answered `compile` for four
  non-compile failures; v4 (category generated after the evidence) stopped that but still
  scores 2/6 (`network`, `other`, `configuration` for infrastructure, configuration and test).
  Category stays a weak field for small models; tomorrow's MCP output should present it as a
  hint, not a verdict.
- Asking for at most 120 characters per citation ended the cut-off replies on the Spotless
  logs (the longest reply went from over 1,024 to 494 tokens), so every log now gets a
  diagnosis, and latency fell to 48 to 72 s per log (same runner type; one run each).
- On 20 LogChunks test-split logs (same deterministic sample as day 3, prompt v3,
  [eval run 43027132131](https://github.com/xeeshan16/ci-pinpoint/actions/runs/43027132131)):
  verified 18/20 (day 3: 17/20), cites a labelled line 10/20 (day 3: 9/20), mean latency
  103 s (47 s), mostly from the longer prompt and the cut-off replies v4 addresses. Within
  noise for 20 logs and one run.

*Injection false positives on LogChunks:* the first run flagged 10 of 584 test-split logs,
all one repository and one line, an iOS app called "Get InfoPlist without jailbreak" in a
README list. The bare word is no longer a trigger.

**Found along the way**
- The schema's key order is the generation order under constrained decoding; putting the
  category first made a 3B model commit to it before reading the evidence.
- Small models paste whole diff lines into citations until the output limit cuts them off;
  a 120-character prefix verifies just as well.
- An eval run that dies at its time limit uploads nothing. Long model runs now use a sample.

**Next (Day 5):** where developers work: an MCP server (stdio) with `diagnose_run`,
`get_excerpt` and `search_log`, ready-to-paste configs for Claude Code, Cursor and VS Code, a
GitHub Action (`action.yml`) that writes the brief to the job summary, and GitLab job logs.

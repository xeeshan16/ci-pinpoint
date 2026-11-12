# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `ci-pinpoint mcp`: an MCP server over stdio for coding agents, with `diagnose_run`,
  `get_excerpt`, `search_log` and `get_log_lines` for GitHub runs, GitLab jobs and log
  files. Redacted output labelled as untrusted, instruction-like lines marked, file access
  confined to `CI_PINPOINT_MCP_ROOTS`, bounded regex search, an in-memory log cache,
  progress notifications and cancellation, and excerpts even without a model. Protocol
  versions 2024-11-05 to 2025-11-25, tested with the official MCP Python SDK client. Configs
  for Claude Code, Cursor and VS Code in `examples/mcp`, guide in `docs/mcp.md`.
- A GitHub Action (`action.yml`): explains the failed jobs of the current run (or any
  `source`) into the job summary, adds an annotation per job, and outputs `brief-json`,
  `summary` and `diagnosed`. Reports the selected lines when no model is configured.
  Example workflows in `examples/workflows`.
- GitLab job and pipeline URLs (gitlab.com and self-managed) with `GITLAB_TOKEN` or
  `CI_JOB_TOKEN`; tokens are only sent to gitlab.com or the configured GitLab host. The
  runner's `ERROR: Job failed` line marks the script section as the failed step, and
  `$ command` echoes score as commands.
- `explain --summary-file`, `--json-out` and `--github-annotations` for CI systems.
- Secret redaction before selection, prompts, cache and output: GitHub, GitLab, AWS,
  Slack, Google, Stripe, npm and LLM API keys, JWTs, URL credentials, authorization
  headers, secret-named assignments and PEM private key blocks. The brief counts
  redactions by kind (`redactions`); `--no-redact` / `CI_PINPOINT_REDACT=false`.
- Prompt-injection hardening: a random-nonce fence, defused look-alike tags,
  instruction-like lines listed to the model as data and never accepted as citations
  (`untrusted_lines` in the brief), and output checks that warn and cap confidence when a
  diagnosis repeats injected text or plants a fix host, and flag pipe-to-shell or
  destructive fixes (`warnings`). A 25-payload test suite and
  `scripts/injection_report.py` for real-model runs.
- A failure-category taxonomy with definitions and tie-break rules in the prompt.
- LLM re-ranking of candidate blocks (`--rerank` / `CI_PINPOINT_RERANK`, off by default
  because `qwen2.5:3b` did not beat the heuristics on the dev split): one short
  schema-constrained call, blended with the heuristic score, with fallback to heuristic
  order. Chains after embedding ranking.
- `ci-pinpoint eval --sample N`: a deterministic, repository-spread sample for slow model
  strategies.
- `ci-pinpoint eval --rerank` adds a `pinpoint+llm` strategy; reports now include paired
  recall differences between ci-pinpoint variants at every budget, re-rank latency and
  token cost, and how many logs the injection detector flags.
- `ci-pinpoint eval`: scores line selection against `tail` and `grep` baselines at equal
  token budgets on LogChunks (797 labelled Travis CI logs, downloaded and checksum-verified
  with `--fetch`) or any `manifest.json` of labelled logs; failure shown, line recall,
  precision, tokens, a paired bootstrap interval, a dev/test split by repository,
  `--diagnose N` for model citations, JSON and Markdown reports and a `--fail-under` gate.
- Travis CI logs: fold and time markers, top-level `$ command` steps and the failed
  command line.
- Leftover excerpt budget is spent on the failed step's output, nearest the failure first
  (LogChunks test split line recall 60.5% to 78.3% at 2,000 tokens).
- Drain-style template mining; selection dampens weak repeated lines, judges block
  novelty by template and folds runs of same-template lines into one line plus a count.
- Optional embedding re-ranking of candidate blocks (`--embedder`, off by default) with
  Ollama, OpenAI or any OpenAI-compatible embeddings endpoint.
- On-disk cache for verified briefs and embedding vectors (`--no-cache`,
  `CI_PINPOINT_CACHE_DIR`).
- Long lines are clipped in excerpts; windows stay inside their step.
- `ci-pinpoint explain` for log files (plain or gzip), stdin and GitHub Actions
  run/attempt/job URLs, with text, Markdown and JSON output and `--no-llm`.
- Log normaliser for GitHub Actions and GitLab logs (timestamps, ANSI, groups,
  sections, annotations) that keeps raw line numbers.
- Heuristic candidate selection with failed-step tails, negative patterns,
  boilerplate filtering, stack-frame compaction and a token budget.
- Providers: OpenAI-compatible (Ollama, vLLM), OpenAI, Azure OpenAI, Anthropic and
  deterministic fakes for tests; retries with full-jitter backoff and Retry-After.
- Diagnosis with a fixed JSON schema (sent as a structured-output schema where the
  provider supports it), one repair round trip, and citation verification against
  the raw log.
- Six hand-labelled real failed GitHub Actions logs as test fixtures.
- CI for Python 3.11 to 3.13, a clean-venv package smoke test, and a workflow
  that runs the fixtures through a real Ollama model.

### Changed
- Diagnosis prompt `diagnose-v4`: the schema asks for summary, root cause and citations
  before the category (generation order under constrained decoding), and citations quote at
  most the first 120 characters of a line, which ended cut-off replies on long diff lines.

### Fixed
- The `\x1b(B` that `tput sgr0` emits is stripped; a line ending in `\r\x1b[0K` keeps its
  text.
- Small-model replies with fields nested under `root_cause`, string citations or
  more than eight citations no longer fail validation.
- A reply cut off at the output limit is reported as such instead of an inner
  object being parsed as the answer.
- Raw-log token estimates no longer count clipped line lengths.

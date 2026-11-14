# Roadmap

Where ci-pinpoint goes after v0.1.0. Items are ordered by how much they would help
someone using it today. Each "good first issue" item is small enough for a first PR
and names the files to start from; see [CONTRIBUTING.md](../CONTRIBUTING.md).

## Next (v0.2)

- **A GitHub Actions evaluation set.** LogChunks is Travis CI from 2018 to 2019; the
  GitHub Actions evidence is six hand-labelled logs. Collect 100+ public failed Actions
  logs across languages, label the root-cause lines, and publish the labels (not the
  logs) with a fetch script, so the eval measures today's CI.
- **Model-level evaluation at scale.** Citation accuracy is measured on 40 logs with a
  3B local model. Add a batched, resumable `eval --diagnose` that can run hundreds of
  logs against a hosted model with a cost cap, and report citation precision/recall
  with confidence intervals, against a model-level baseline (the same model given the
  `tail` of the log instead of the selected excerpt).
- **Per-test failures as separate findings.** A job with 19 failing tests gets one
  diagnosis today. Group failures by exception template and diagnose each group, so an
  agent can fix them one by one.
- **Diff-aware diagnosis.** Given the PR diff (or `git diff` of the failing commit),
  rank the files the cited lines implicate and say which change most likely broke the
  build.
- **MCP protocol 2026-07-28.** Support the newer handshake and `server/discover` so
  clients that only speak it can connect (today the server relies on their fallback).

## Later

- **Flaky-failure detection.** Compare the failure's templates with the last N runs of
  the same job; if the same block appears in passing runs too, say so instead of
  diagnosing a code change.
- **Other CI systems by URL.** CircleCI, Buildkite and Jenkins console URLs (files and
  stdin already work for any CI).
- **A PR comment mode for the action**, updating one comment per run instead of the job
  summary only.
- **A tokenizer-exact budget** using the target model's tokenizer when it is available
  locally, instead of the 3.5 characters-per-token estimate.
- **Redaction plug-ins:** let teams add organisation-specific secret formats from the
  config file.

## Good first issues

- **A `ci-pinpoint lines <source> <start> <end>` subcommand** that prints a line range
  with the same caps as the MCP `get_log_lines` tool, to follow a citation from a shell:
  `src/ci_pinpoint/cli.py`, `src/ci_pinpoint/mcp_server/tools.py`.
- **More failure signals for one ecosystem you know** (for example Rust `error[E…]`,
  Go `--- FAIL`, .NET `error CS…`), each with a labelled log fixture:
  `src/ci_pinpoint/heuristics.py`, `tests/test_heuristics.py`.
- **Recognise Buildkite and CircleCI section markers** in the normaliser:
  `src/ci_pinpoint/normalize.py`, `tests/test_normalize.py`.
- **A `--format sarif` output**, so citations show up in code-scanning UIs:
  `src/ci_pinpoint/render.py`.
- **Price table entries** for more hosted models, with a source link in the comment:
  `src/ci_pinpoint/pricing.py`, `tests/test_pricing.py`.
- **A `ci-pinpoint cache clear` subcommand**: `src/ci_pinpoint/cache.py`,
  `src/ci_pinpoint/cli.py`.

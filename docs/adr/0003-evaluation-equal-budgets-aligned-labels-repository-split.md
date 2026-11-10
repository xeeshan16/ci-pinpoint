# ADR 0003: Evaluate selection at equal token budgets, on aligned labels, split by repository

- Status: accepted
- Date: 2026-11-10

## Context
The claim this tool makes is "the lines that matter, in a few KB". That needs a number
on logs it was not built on, against the obvious alternatives a developer or an agent
would use instead: the end of the log, or a grep for `error|fail`. LogChunks (Brandt et
al., MSR 2020) is the only public dataset we found where humans marked the part of a
real CI log that explains the failure: 797 Travis CI logs from 80 repositories, CC BY 4.0.

Three things make a naive comparison misleading:
1. A strategy that shows more text finds more. Comparing a 2,000-token excerpt with a
   400-token grep says nothing.
2. LogChunks labels are text, not line numbers. The text is the log with escape bytes
   removed (XML cannot hold them), so `[37m` remnants, blank lines from `\r\r\n` endings
   and partial first lines must be matched back to the raw log.
3. Tuning selection on the same logs it is scored on inflates the score; logs from one
   repository look alike, so a random split by log would leak.

## Decision
- Every strategy gets the **same token budget, counted by the same estimator**, on the
  same cleaned lines (the baselines run on the cleaned log, which makes them cheaper and
  stronger than on raw bytes). We report several budgets, not one.
- **Labels are aligned, not guessed.** Each chunk line is matched to log lines
  (exact for short lines, substring otherwise), anchored on the most distinctive chunk
  lines and walked outward, tolerating blank lines and at most two unrelated lines between
  chunk lines. Every occurrence with the best alignment counts. A chunk with fewer than half
  of its lines found is excluded and listed by id with how much was found. 24 of 797 are
  excluded today.
- Metrics per log: failure shown (any labelled line shown), line recall (share of chunk
  lines shown), whole failure shown, precision, tokens and reduction. The headline
  comparison is a paired bootstrap 95% interval on the recall difference.
- **Split by repository**, deterministically (SHA-256 of `owner/repo`, 3 in 10 buckets to
  dev): 189 dev logs, 584 test logs. Changes to selection are judged on dev; test is
  reported, not tuned on.
- The dataset is downloaded from Zenodo and checksum-verified on demand (`--fetch`), not
  vendored. The same harness scores any `manifest.json` of labelled logs, so a team can
  measure on its own failures.

## Consequences
- The first full run exposed a real weakness: selection used under half of its budget, and
  plain tail covered more of the labelled failure (line recall 60.8% vs 65.6%). Filling
  the leftover budget from the failed step fixed it (76.6% vs 65.6% on all logs). That
  change was motivated by that first run, which included the test repositories; it was not
  fitted to individual labels, but the test split is not perfectly pristine for it. From
  here, tuning looks at dev only.
- LogChunks is Travis CI, 2018 to 2019. Our main target is GitHub Actions. The six labelled
  GitHub Actions logs in `tests/fixtures/logs` are scored by the same harness, but six logs
  are a smoke test.
- Line recall measures whether the right lines reach the model, not whether the model
  explains them. `--diagnose N` scores model citations on a deterministic sample; it is
  slow on CPU, so it runs on demand rather than on every push.

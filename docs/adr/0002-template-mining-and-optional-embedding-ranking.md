# ADR 0002: Drain-style templates in selection; embedding ranking opt-in

- Status: accepted
- Date: 2026-11-09

## Context
Day 1 selection treated every line on its own. Real CI logs are dominated by
repetition: 8,095 Maven `Progress (...)` lines in one fixture, 247 identical stack
frames in another, 19 failing tests that all repeat the same `Caused by`. Repetition
wasted budget and hid the one unusual line. The plan also called for embedding-based
ranking, on the theory that semantic similarity to known failure modes would surface
causes that have no error keyword.

## Decision
1. Mine templates with a simplified Drain (fixed-depth prefix tree, token-position
   similarity, `<*>` wildcards). It runs in about 0.15 s on a 15,687-line log and has no
   dependencies.
2. Use templates three ways: dampen *weak* lines with very common templates (but not
   strong ones, because a cause repeated across failing tests is a common cause); judge
   block novelty by evidence templates; and fold runs of same-template filler into one
   line plus a count in the excerpt, which the model and the reader both see.
3. Ship embedding re-ranking as an option (`--embedder`), blended so that the heuristic
   score keeps most of the weight, with SQLite caching of vectors. Keep it **off by
   default**.

## Evidence
On the six labelled real logs (CI run 42672806235, `nomic-embed-text` on Ollama):
- templates and folding moved "root cause kept in the excerpt" at a 200 to 400-token
  budget from 4/6 to 6/6;
- embedding re-ranking left the mean reciprocal rank of the root-cause block unchanged
  (0.833) and lost one root cause at a 200-token budget (5/6 vs 6/6).

## Consequences
- The default needs one model (the chat model), which keeps the five-minute quick start.
- The LogChunks evaluation (day 3) will measure both rankers on 797 logs; if embeddings
  help there, the default can change with evidence rather than intuition.
- Six logs were also the development set for these changes, so these numbers are not an
  unbiased estimate.

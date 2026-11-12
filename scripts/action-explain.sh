#!/usr/bin/env bash
# Runs ci-pinpoint inside the GitHub Action (action.yml). Inputs arrive as INPUT_*
# environment variables; outputs go to $GITHUB_OUTPUT, the brief to the job summary.
set -euo pipefail

bin="${CI_PINPOINT_BIN:-ci-pinpoint}"
source_ref="${INPUT_SOURCE:-}"
if [[ -z "$source_ref" ]]; then
  source_ref="$DEFAULT_SOURCE"
fi
# One file per invocation: a job may use the action more than once.
json_out="$RUNNER_TEMP/ci-pinpoint-briefs-${GITHUB_ACTION:-run}-$$.json"

args=(explain "$source_ref" --format text --show-excerpt --json-out "$json_out")
[[ -n "${GITHUB_STEP_SUMMARY:-}" ]] && args+=(--summary-file "$GITHUB_STEP_SUMMARY")
[[ "${INPUT_ANNOTATE:-true}" == "true" ]] && args+=(--github-annotations)
[[ -n "${INPUT_BUDGET:-}" ]] && args+=(--budget "$INPUT_BUDGET")
[[ -n "${INPUT_MODEL:-}" ]] && args+=(--model "$INPUT_MODEL")
[[ -n "${INPUT_BASE_URL:-}" ]] && args+=(--base-url "$INPUT_BASE_URL")

no_llm="${INPUT_NO_LLM:-false}"
if [[ -n "${INPUT_PROVIDER:-}" ]]; then
  args+=(--provider "$INPUT_PROVIDER")
elif [[ -z "${ANTHROPIC_API_KEY:-}${AZURE_OPENAI_ENDPOINT:-}${OPENAI_API_KEY:-}${CI_PINPOINT_BASE_URL:-}${INPUT_BASE_URL:-}${CI_PINPOINT_PROVIDER:-}" ]]; then
  # No model is configured, and a runner has no local Ollama: don't spend retries on
  # localhost, report the selected lines instead.
  no_llm=true
  echo "::notice title=ci-pinpoint::No model configured (set OPENAI_API_KEY, ANTHROPIC_API_KEY, the Azure variables, or the provider and base-url inputs); reporting the selected log lines only."
fi
[[ "$no_llm" == "true" ]] && args+=(--no-llm)

set +e
"$bin" "${args[@]}"
code=$?
set -e

diagnosed=false
summary=""
if [[ -f "$json_out" ]]; then
  read -r diagnosed summary < <(python3 - "$json_out" <<'PY'
import json, sys
briefs = json.load(open(sys.argv[1], encoding="utf-8"))
ok = bool(briefs) and all((b.get("diagnosis") or {}).get("verified") for b in briefs)
first = next((b["diagnosis"]["diagnosis"]["summary"] for b in briefs if b.get("diagnosis")), "")
print("true" if ok else "false", " ".join(first.split())[:500])
PY
  )
fi
{
  echo "brief-json=$json_out"
  echo "diagnosed=$diagnosed"
  echo "summary=$summary"
} >> "$GITHUB_OUTPUT"

case "$code" in
  0) ;;
  1) echo "::warning title=ci-pinpoint::The model did not return a verified diagnosis; the summary shows the selected log lines instead." ;;
  *) echo "::error title=ci-pinpoint::ci-pinpoint could not read the failed run (exit $code); see the step log."
     exit "$code" ;;
esac

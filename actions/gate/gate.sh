#!/usr/bin/env bash
# Shell steps of the aobs reliability-gate action (actions/gate/action.yml).
#
#   gate.sh install   create a private virtual environment and install aobs-server into it
#   gate.sh run       run `aobs-server gate`, write the reports, the job summary and the outputs
#   gate.sh verdict   pass or fail the step according to the gate result and fail-on-regression
#
# Every input arrives in an environment variable (set by action.yml from `inputs.*`); nothing is
# interpolated into this script, so an input cannot inject shell code. Trace contents are
# untrusted: the gate's stdout is printed between ::stop-commands:: markers so a trace cannot issue
# workflow commands, and the job summary is the gate's own Markdown, which escapes trace data.
set -euo pipefail

notice() { printf '::notice title=aobs gate::%s\n' "$1"; }
fail() {
	printf '::error title=aobs gate::%s\n' "$1"
	exit "${2:-2}"
}

output() { # name value: one line of $GITHUB_OUTPUT (values here never hold a line break)
	case "$2" in *$'\n'* | *$'\r'*) fail "output $1 would hold a line break" ;; esac
	if [[ -n "${GITHUB_OUTPUT:-}" ]]; then printf '%s=%s\n' "$1" "$2" >>"$GITHUB_OUTPUT"; fi
}

boolean() { # name value: a true/false input, or a configuration error (exit code 2)
	case "$2" in
	true | false) ;;
	*) fail "input $1 must be true or false" ;;
	esac
}

venv_bin() { # the scripts directory of a virtual environment (bin, or Scripts on Windows)
	if [[ -d "$1/Scripts" && ! -d "$1/bin" ]]; then printf '%s/Scripts' "$1"; else printf '%s/bin' "$1"; fi
}

cmd_install() {
	: "${AOBS_PYTHON:?the setup-python step did not report a Python interpreter}"
	local source="${AOBS_INSTALL_SOURCE:-}" venv bin
	if [[ -z "$source" ]]; then
		# The server package of the same checkout as this action, so versions match.
		source="$(cd "${GITHUB_ACTION_PATH:?}/../../server" && pwd)"
	fi
	venv="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/aobs-gate-venv.XXXXXX")"
	"$AOBS_PYTHON" -m venv "$venv"
	bin="$(venv_bin "$venv")"
	echo "Installing aobs-server from: $source"
	"$bin/python" -m pip install --disable-pip-version-check --no-input --quiet -- "$source"
	"$bin/aobs-server" --help >/dev/null
	output aobs-server "$bin/aobs-server"
}

expand() { # pattern: sets matches to the pattern's glob expansion (the pattern itself if none)
	local IFS= # globbed but never split at spaces
	# shellcheck disable=SC2206 # unquoted on purpose: glob expansion
	matches=($1)
}

# Exit codes of `aobs-server gate` (docs/reliability-gate.md).
describe_exit() {
	case "$1" in
	1) echo "missing input, no runs in the baseline, database unavailable or a report file that cannot be written" ;;
	2) echo "invalid policy or options" ;;
	*) echo "unexpected exit code" ;;
	esac
}

cmd_run() {
	local bin="${AOBS_GATE_BIN:?the install step did not report aobs-server}"
	local policy="${AOBS_POLICY:-}" traces="${AOBS_TRACES:-}"
	local no_errors="${AOBS_NO_ERROR_MESSAGES:-false}"
	boolean no-error-messages "$no_errors"
	boolean comment-on-pr "${AOBS_COMMENT_ON_PR:-false}"
	boolean fail-on-regression "${AOBS_FAIL_ON_REGRESSION:-true}"
	[[ -n "$policy" ]] || fail "input policy is required"

	local dir="${AOBS_REPORT_DIR:-}"
	if [[ -z "$dir" ]]; then dir="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/aobs-gate.XXXXXX")"; fi
	mkdir -p -- "$dir"
	dir="$(cd -- "$dir" && pwd)"
	local junit="$dir/agent-gate.xml" markdown="$dir/agent-gate.md"
	rm -f -- "$junit" "$markdown"
	output junit-path "$junit"
	output markdown-path "$markdown"

	# `traces`: one path, glob pattern or db: selection per line. Globs expand like in bash with
	# globstar; a pattern that matches nothing is passed on as is, so the gate reports it missing.
	local -a args=(gate "--policy=$policy" "--junit=$junit" "--markdown=$markdown") inputs=()
	local line
	shopt -s globstar 2>/dev/null || true # bash 4+; with an older bash, ** matches like *
	while IFS= read -r line || [[ -n "$line" ]]; do
		line="${line%$'\r'}"
		line="${line#"${line%%[![:space:]]*}"}"
		line="${line%"${line##*[![:space:]]}"}"
		[[ -z "$line" ]] && continue
		if [[ "$line" == db:* || "$line" != *[*?[]* ]]; then
			inputs+=("$line")
		else
			expand "$line"
			inputs+=("${matches[@]}")
		fi
	done <<<"$traces"
	[[ ${#inputs[@]} -gt 0 ]] || fail "input traces is empty: give at least one trace file, glob or db: selection"
	if [[ -n "${AOBS_BASELINE:-}" ]]; then args+=("--baseline=$AOBS_BASELINE"); fi
	if [[ -n "${AOBS_BASELINE_TRACE:-}" ]]; then args+=("--baseline-trace=$AOBS_BASELINE_TRACE"); fi
	if [[ -n "${AOBS_PROJECT:-}" ]]; then args+=("--project=$AOBS_PROJECT"); fi
	if [[ "$no_errors" == true ]]; then args+=(--no-error-messages); fi
	args+=(-- "${inputs[@]}")

	# Trace data reaches stdout: stop the runner from reading workflow commands in it.
	local token code=0
	token="aobs-$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
	echo "::stop-commands::$token"
	"$bin" "${args[@]}" || code=$?
	echo "::$token::"

	local result
	case "$code" in
	0) result=pass ;;
	3) result=fail ;;
	*) result=error ;;
	esac
	output result "$result"
	output exit-code "$code"

	if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
		if [[ "$result" != error && -f "$markdown" ]]; then
			cat -- "$markdown" >>"$GITHUB_STEP_SUMMARY"
		else
			printf '### aobs reliability gate: **ERROR**\n\nThe gate gave no verdict: exit code %d (%s). See the step log.\n' \
				"$code" "$(describe_exit "$code")" >>"$GITHUB_STEP_SUMMARY"
		fi
	fi
}

cmd_verdict() {
	local result="${AOBS_RESULT:-error}" code="${AOBS_EXIT_CODE:-1}" enforce="${AOBS_FAIL_ON_REGRESSION:-true}"
	boolean fail-on-regression "$enforce"
	[[ "$code" =~ ^[0-9]+$ ]] || code=1
	case "$result" in
	pass) echo "aobs reliability gate: PASS" ;;
	fail)
		if [[ "$enforce" == true ]]; then fail "the reliability gate failed (see the job summary)" 3; fi
		notice "the reliability gate failed; fail-on-regression is false, so the step passes"
		;;
	*) fail "the reliability gate gave no verdict: exit code $code ($(describe_exit "$code"))" "$((code == 0 ? 1 : code))" ;;
	esac
}

case "${1:-}" in
install) cmd_install ;;
run) cmd_run ;;
verdict) cmd_verdict ;;
*) fail "usage: gate.sh install|run|verdict" ;;
esac

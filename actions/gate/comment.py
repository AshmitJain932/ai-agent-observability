"""Post or update the reliability gate's sticky pull-request comment (actions/gate/action.yml).

Runs only when the action's ``comment-on-pr`` input is true. One comment per pull request and
``comment-tag``: it is found again by a hidden marker in its first line and edited in place, so a
pull request never collects one comment per push. It is written with ``gh api`` and the token in
``GH_TOKEN`` (the job's ``GITHUB_TOKEN``), which needs ``pull-requests: write``. This script never
reads, prints or logs the token; only ``gh`` uses it.

Not being able to comment is a warning, never a failure: the gate's verdict is the job's result.
A pull request from a fork gets a read-only token on ``pull_request`` events, so it is skipped
with a notice before any request is made.

The body is the gate's own Markdown report, which escapes trace data (no markup, links, HTML or
line breaks). As defence in depth, a zero-width space is put after every ``@`` so a trace cannot
mention, and so notify, a GitHub user or team. Everything else in the body is fixed text.

Standard library only; inputs come from environment variables, never from the command line.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

BOT_LOGIN = "github-actions[bot]"  # the author of comments written with the default GITHUB_TOKEN
MAX_BODY = 60_000  # GitHub rejects comment bodies over 65536 characters
TAG = re.compile(r"[A-Za-z0-9._-]{1,64}")
PR_EVENTS = ("pull_request", "pull_request_target")
_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


def _log_text(text: str) -> str:
    """Text for the step log that cannot be read as a workflow command."""
    return _UNSAFE.sub(" ", text).replace("::", ": :").strip()[:300]


def notice(message: str) -> None:
    print(f"::notice title=aobs gate comment::{_log_text(message)}")


def warning(message: str) -> None:
    print(f"::warning title=aobs gate comment::{_log_text(message)}")


def marker(tag: str) -> str:
    return f"<!-- aobs-reliability-gate:{tag} -->"


def comment_body(tag: str, result: str, exit_code: str, markdown: str | None, run_url: str | None) -> str:
    """The comment: marker, the gate's Markdown (or a fixed error text), a link to the run."""
    if result in ("pass", "fail") and markdown is not None:
        report = markdown.replace("@", "@\u200b").rstrip("\n")
    else:
        code = exit_code if exit_code.isdigit() else "?"
        report = f"### aobs reliability gate: **ERROR**\n\nThe gate gave no verdict (exit code {code})."
    footer = f"\n\n[Workflow run]({run_url})" if run_url else ""
    if len(report) + len(footer) > MAX_BODY:
        cut = report.rfind("\n", 0, MAX_BODY - len(footer) - 200)
        report = report[: max(cut, 0)] + "\n\n… the report was cut here; the job summary has all of it."
    return f"{marker(tag)}\n{report}{footer}\n"


class _GhError(Exception):
    def __init__(self, what: str, proc: subprocess.CompletedProcess[str]) -> None:
        super().__init__(what)
        self.what, self.proc = what, proc


def _gh(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True, timeout=120, check=False)


def _failed(what: str, proc: subprocess.CompletedProcess[str]) -> None:
    err = proc.stderr or ""
    if "HTTP 403" in err or "Resource not accessible by integration" in err:
        warning(
            f"cannot {what}: the token may not write pull-request comments. Give the job "
            "`permissions: pull-requests: write` (pull requests from forks get a read-only token)."
        )
    else:
        first = next((line for line in err.splitlines() if line.strip()), f"gh exited with {proc.returncode}")
        warning(f"cannot {what}: {first}")


def find_comment(repo: str, number: int, tag: str) -> int | None:
    """The id of this tag's earlier comment by the Actions bot, or None. Raises on a gh failure."""
    proc = _gh(
        "api",
        "--paginate",
        f"repos/{repo}/issues/{number}/comments?per_page=100",
        "--jq",
        ".[] | {id: .id, login: .user.login, head: .body[0:200]}",
    )
    if proc.returncode != 0:
        raise _GhError("read the pull request's comments", proc)
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if item.get("login") == BOT_LOGIN and str(item.get("head") or "").startswith(marker(tag)):
            return int(item["id"])
    return None


def _write_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path and "\n" not in value and "\r" not in value:
        with open(path, "a", encoding="utf-8") as out:
            out.write(f"{name}={value}\n")


def main() -> int:
    env = os.environ
    tag = env.get("AOBS_COMMENT_TAG") or "default"
    if not TAG.fullmatch(tag):
        print("::error title=aobs gate comment::input comment-tag may hold only letters, digits, '.', '_' and '-'")
        return 2
    event = env.get("GITHUB_EVENT_NAME", "")
    if event not in PR_EVENTS:
        notice(f"no pull-request comment: the event is {event or 'unknown'}, not a pull request")
        return 0
    try:
        payload = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        pr = payload["pull_request"]
        number = int(pr["number"])
    except (KeyError, OSError, ValueError, TypeError):
        warning("no pull-request comment: the event payload has no pull request number")
        return 0
    repo = env.get("GITHUB_REPOSITORY", "")
    head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
    if event == "pull_request" and head_repo != repo:
        notice("no pull-request comment: this pull request comes from a fork, whose token is read-only")
        return 0
    if not env.get("GH_TOKEN"):
        warning("no pull-request comment: no token")
        return 0

    markdown_path = env.get("AOBS_MARKDOWN_PATH", "")
    markdown = None
    if markdown_path and Path(markdown_path).is_file():
        markdown = Path(markdown_path).read_text(encoding="utf-8", errors="replace")
    server, run_id = env.get("GITHUB_SERVER_URL", ""), env.get("GITHUB_RUN_ID", "")
    run_url = f"{server}/{repo}/actions/runs/{run_id}" if server and run_id.isdigit() else None
    body = json.dumps(
        {"body": comment_body(tag, env.get("AOBS_RESULT", ""), env.get("AOBS_EXIT_CODE", ""), markdown, run_url)}
    )

    try:
        existing = find_comment(repo, number, tag)
        if existing is None:
            args, what = ["--method", "POST", f"repos/{repo}/issues/{number}/comments"], "post the comment"
        else:
            args, what = ["--method", "PATCH", f"repos/{repo}/issues/comments/{existing}"], "update the comment"
        proc = _gh("api", *args, "--input", "-", "--jq", ".html_url", stdin=body)
        if proc.returncode != 0:
            raise _GhError(what, proc)
    except FileNotFoundError:
        warning("no pull-request comment: the gh command-line tool is not installed on this runner")
        return 0
    except subprocess.TimeoutExpired:
        warning("no pull-request comment: gh did not answer within 120 s")
        return 0
    except _GhError as exc:
        _failed(exc.what, exc.proc)
        return 0
    url = proc.stdout.strip()
    print(f"{'Updated' if existing is not None else 'Posted'} the gate comment: {_log_text(url)}")
    _write_output("comment-url", url)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Server-side secret scrubbing for error messages, stack traces, string attributes and redacted
content. It mirrors the SDK's patterns (``aobs._sanitize``) without importing the SDK; a shared
test corpus checks that both remove the same secrets.
"""

from __future__ import annotations

import re

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("api_key", re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}")),
    ("aobs_key", re.compile(r"\baobs_ik_[A-Za-z0-9_-]{20,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")),
    ("url_credentials", re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)")),
)
_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|passwd|authorization)\b(\s*[:=]\s*)(['\"]?)([^\s'\",;]{6,})"
)


def scrub_secrets(text: str) -> tuple[str, int]:
    """Replace credential-shaped substrings with ``[REDACTED:<kind>]``. Returns (text, count)."""
    total = 0
    for name, pattern in _SECRET_PATTERNS:
        text, n = pattern.subn(f"[REDACTED:{name}]", text)
        total += n

    def _assign(m: re.Match[str]) -> str:
        if m.group(4).startswith("[REDACTED"):
            return m.group(0)
        return f"{m.group(1)}{m.group(2)}{m.group(3)}[REDACTED:credential]"

    text, n = _ASSIGNMENT.subn(_assign, text)
    return text, total + n


def scrub_to_fit(text: str, max_chars: int) -> tuple[str, int]:
    """Scrub, then cut to ``max_chars`` (a replacement can be longer than what it replaced)."""
    scrubbed, count = scrub_secrets(text)
    return scrubbed[:max_chars], count

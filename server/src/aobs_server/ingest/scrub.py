"""Server-side secret scrubbing for error messages, stack traces, string attributes and redacted
content. It mirrors the SDK's patterns (``aobs._sanitize``) without importing the SDK; a shared
test corpus checks that both remove the same secrets.

Error messages and stack traces additionally lose email addresses and card numbers
(:func:`scrub_error_text`, mirroring the SDK's ``aobs.privacy.scrub_error_text``), so text from
older SDKs or other emitters is cleaned before it is stored.
"""

from __future__ import annotations

import re
import string
from collections.abc import Callable

# Private keys and JWTs are found without a regex over the whole secret: as regexes
# (``-----BEGIN …-----[\s\S]*?-----END …-----`` and ``\beyJ…{5,}\.eyJ…{5,}\.…{5,}``) they try
# every BEGIN marker, or every ``eyJ`` inside a long token, and scan to the end of the text from
# each one: quadratic (32 KiB took 0.3 s, 1 MiB minutes). Both scanners below look at every
# character a bounded number of times and find exactly what those regexes found, except that a
# private key with no END marker is removed up to the end of the text instead of left in place.
_PRIVATE_KEY_BEGIN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_PRIVATE_KEY_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")
# A run of token characters with at least two dots in it; the lookbehind starts it only at the
# beginning of a run, so a long run without dots is scanned once, not once per character.
_DOTTED_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]++(?:\.[A-Za-z0-9_-]++){2,}+")


def _scrub_private_keys(text: str, replacement: str) -> tuple[str, int]:
    begin = _PRIVATE_KEY_BEGIN.search(text)
    if begin is None:
        return text, 0
    parts: list[str] = []
    count = 0
    done = 0
    while begin is not None:
        parts.append(text[done : begin.start()])
        parts.append(replacement)
        count += 1
        end = _PRIVATE_KEY_END.search(text, begin.end())
        if end is None:  # cut off, or malformed: fail closed
            done = len(text)
            break
        done = end.end()
        begin = _PRIVATE_KEY_BEGIN.search(text, done)
    parts.append(text[done:])
    return "".join(parts), count


def _jwt_start(segment: str, at_boundary: bool) -> int:
    """Where a JWT's first part starts in ``segment``: the first ``eyJ`` at a word boundary with at
    least five token characters after it, or -1. ``at_boundary``: whether the segment's start is
    one (inside the segment only ``-`` ends a word)."""
    at = segment.find("eyJ")
    while at != -1 and at + 8 <= len(segment):
        if at_boundary if at == 0 else segment[at - 1] == "-":
            return at
        at = segment.find("eyJ", at + 1)
    return -1


def _scrub_jwts(text: str, replacement: str) -> tuple[str, int]:
    if "eyJ" not in text:
        return text, 0
    count = 0

    def scrub_token(match: re.Match[str]) -> str:
        nonlocal count
        token = match.group()
        segments = token.split(".")
        before = text[match.start() - 1] if match.start() else ""
        at_boundary = not (before.isalnum() or before == "_")  # what \b sees before the token
        parts: list[str] = []
        done = 0  # end, in ``token``, of the previous replacement
        start = 0  # start, in ``token``, of segments[i]
        i = 0
        while i + 2 < len(segments):
            at = _jwt_start(segments[i], at_boundary or i > 0)
            payload, signature = segments[i + 1], segments[i + 2]
            if at != -1 and payload.startswith("eyJ") and len(payload) >= 8 and len(signature) >= 5:
                parts.append(token[done : start + at])
                parts.append(replacement)
                count += 1
                done = start + len(segments[i]) + len(payload) + len(signature) + 2
                start = done + 1
                i += 3
            else:
                start += len(segments[i]) + 1
                i += 1
        parts.append(token[done:])
        return "".join(parts)

    return _DOTTED_TOKEN.sub(scrub_token, text), count


_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str] | Callable[[str, str], tuple[str, int]]], ...] = (
    ("private_key", _scrub_private_keys),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("api_key", re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}")),
    ("aobs_key", re.compile(r"\baobs_ik_[A-Za-z0-9_-]{20,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("jwt", _scrub_jwts),
    ("url_credentials", re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)")),
)
_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|passwd|authorization)\b(\s*[:=]\s*)(['\"]?)([^\s'\",;]{6,})"
)


def scrub_secrets(text: str) -> tuple[str, int]:
    """Replace credential-shaped substrings with ``[REDACTED:<kind>]``. Returns (text, count)."""
    total = 0
    for name, pattern in _SECRET_PATTERNS:
        replacement = f"[REDACTED:{name}]"
        text, n = pattern.subn(replacement, text) if isinstance(pattern, re.Pattern) else pattern(text, replacement)
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


_EMAIL_LOCAL_CHARS = string.ascii_letters + string.digits + "._%+-"
_EMAIL_DOMAIN_RUN = re.compile(r"[A-Za-z0-9.-]*")
_ASCII_LETTERS = frozenset(string.ascii_letters)
_ASCII_LETTER_RUN = re.compile(r"[A-Za-z]*")


def _email_end(text: str, at: int) -> int | None:
    """Where an address whose ``@`` is at ``at`` ends, or None if the domain is not valid. The domain
    ends after the rightmost ``.`` that has a domain character before it and two letters after it
    (as the greedy regex ``[A-Za-z0-9.-]+\\.[A-Za-z]{2,}`` decides)."""
    run_end = _EMAIL_DOMAIN_RUN.match(text, at + 1).end()  # type: ignore[union-attr]  # "*" always matches
    dot = text.rfind(".", at + 2, run_end)
    while dot != -1:
        if dot + 2 < len(text) and text[dot + 1] in _ASCII_LETTERS and text[dot + 2] in _ASCII_LETTERS:
            return _ASCII_LETTER_RUN.match(text, dot + 1).end()  # type: ignore[union-attr]
        dot = text.rfind(".", at + 2, dot)
    return None


def redact_emails(text: str) -> tuple[str, int]:
    """Replace e-mail addresses with ``[REDACTED:email]``, in time linear in the text's length.

    Mirrors the SDK's ``aobs.privacy.EmailRedactor`` (a test checks they agree) and finds exactly
    what ``re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}", ...)`` finds, without that
    regex's quadratic time on long runs of address characters (it tries every start in the run and
    scans to the run's end each time). Candidates are found from their ``@`` instead.
    """
    at = text.find("@")
    if at == -1:
        return text, 0
    parts: list[str] = []
    count = 0
    done = 0  # end of the previous replacement: the regex would resume its search here
    previous_at = -1
    while at != -1:
        floor = max(done, previous_at + 1)  # a local part never spans an "@" or a replacement
        start = floor + len(text[floor:at].rstrip(_EMAIL_LOCAL_CHARS))
        end = _email_end(text, at) if start < at else None
        previous_at = at
        if end is None:
            at = text.find("@", at + 1)
            continue
        parts.append(text[done:start])
        parts.append("[REDACTED:email]")
        count += 1
        done = end
        at = text.find("@", end)
    parts.append(text[done:])
    return "".join(parts), count


_CARD_NUMBER = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")


def _luhn(candidate: str) -> bool:
    digits = [int(c) for c in candidate if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def scrub_personal_data(text: str) -> tuple[str, int]:
    """Replace email addresses and card numbers (Luhn-checked) with ``[REDACTED:<kind>]``."""
    text, emails = redact_emails(text)
    cards = 0

    def _card(m: re.Match[str]) -> str:
        nonlocal cards
        if not _luhn(m.group(0)):
            return m.group(0)
        cards += 1
        return "[REDACTED:card_number]"

    text = _CARD_NUMBER.sub(_card, text)
    return text, emails + cards


def scrub_error_text(text: str, max_chars: int) -> tuple[str, int, int]:
    """Secrets, then email addresses and card numbers, then cut to ``max_chars``.
    Returns (text, secrets replaced, personal data replaced)."""
    scrubbed, secrets = scrub_secrets(text)
    scrubbed, personal = scrub_personal_data(scrubbed)
    return scrubbed[:max_chars], secrets, personal

"""Secret redaction for text that humans will read (task live log).

One pure function. It is best effort, not a guarantee: it masks the credential
shapes the fleet handles, plus the exact secrets the caller passes in. The work
stays linear in the input (no ReDoS): a rule starts only where its run starts (a
lookbehind, or a literal keyword), so no two starts rescan the same run.
"""

import re

REDACTED = "[redacted]"
_MIN_EXACT = 8  # shorter exact secrets would mask ordinary words

# The name's ending; whatever comes before it stays outside the match (any length).
_NAME = r"(?:token|secret|passw(?:or)?d|pwd|api[_-]?key|access[_-]?key|private[_-]?key|credentials?)"

_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]{0,32}PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]{0,32}PRIVATE KEY-----|\Z)",
    re.DOTALL,
)

# (pattern, replacement), applied in order after the key blocks. Prefix groups keep the surrounding syntax.
_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p, flags), r)
    for p, r, flags in (
        (
            r"(\b(?:proxy-)?authorization\s*[:=]\s*)(?:(?:bearer|basic|token|digest)\s+)?[^\s\"',;]+",
            rf"\1{REDACTED}",
            re.IGNORECASE,
        ),
        (r"(\bbearer\s+)[A-Za-z0-9._~+/=-]{8,}", rf"\1{REDACTED}", re.IGNORECASE),
        # a JWT starts a run: never inside one (`-eyJ-eyJ…` would rescan it per start)
        (
            r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*",
            REDACTED,
            0,
        ),
        (r"\bsk-[A-Za-z0-9_-]{16,}", REDACTED, 0),
        (r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})", REDACTED, 0),
        (r"\bglpat-[A-Za-z0-9_-]{16,}", REDACTED, 0),
        (r"\bxox[abprs]-[A-Za-z0-9-]{10,}", REDACTED, 0),
        (r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b", REDACTED, 0),
        (r"\bAIza[0-9A-Za-z_-]{35}", REDACTED, 0),
        # user:password@ in a URL
        (
            r"(\b[a-z][a-z0-9+.-]{0,15}://[^\s/:@]+:)[^\s/@]+@",
            rf"\1{REDACTED}@",
            re.IGNORECASE,
        ),
        # NAME=value, "name": "value", name: value
        (
            rf"({_NAME})([\"']?\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s\"',;]+)",
            rf"\1\2{REDACTED}",
            re.IGNORECASE,
        ),
        # --token value: from the start of the word, which has a dash before the name
        (
            r"(?<![\w-])(\w*--?[\w-]*(?:token|secret|password|api-?key)\s+)(?!-)\S+",
            rf"\1{REDACTED}",
            re.IGNORECASE,
        ),
    )
)


def redact_private_keys(text: str) -> str:
    """`text` with PEM private key blocks masked (an unterminated one to the end)."""
    return _KEY_BLOCK.sub("[redacted private key]", text)


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    """`text` with credentials masked. `secrets` are exact values to mask wherever
    they appear (for example the caller's own token). Idempotent."""
    text = redact_private_keys(text)
    for secret in secrets:
        if len(secret) >= _MIN_EXACT:
            text = text.replace(secret, REDACTED)
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text

"""
Ingestion guards for the Mesh context layer.
Sanitizes input, detects adversarial prompt injection patterns,
and auto-redacts credentials while enforcing local_only (private) storage.
"""

from __future__ import annotations

import re
from typing import NamedTuple, Optional

from .passive import (
    _INVISIBLE_CHAR_CLASS,
    _is_credential,
    _is_poison_attempt,
    _redact_secrets_for_cloud,
)

# Strip control characters (C0/C1) except newline (\n) and tab (\t), plus the
# invisible formatting characters that can hide or split poison text and
# credentials (soft hyphen, zero-width joiners, bidi controls, tag characters).
# The invisible ranges are shared with `passive.py` so the guard, the poison
# detector, and the cloud redactor can never drift apart.
_CONTROL_CHAR_RE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f" + _INVISIBLE_CHAR_CLASS + "]"
)


class GuardResult(NamedTuple):
    content: str
    local_only: bool
    rejected: bool
    reason: Optional[str] = None
    # `()` rather than `[]`: a mutable default on a NamedTuple is shared by every
    # result that omits the field, so one caller appending a warning would leak
    # it into all future results.
    warnings: tuple[str, ...] = ()


def guard_ingest(content: str, local_only: bool = False) -> GuardResult:
    """
    Validate and sanitize content before memory ingestion across all entry points:
    1. Strip NUL and unexpected control characters (preserving \\n and \\t).
    2. Check for adversarial prompt injection patterns. If detected, reject.
    3. Check for credentials or secrets. If detected, redact secrets and force local_only=True.
    """
    if not isinstance(content, str):
        content = str(content or "")

    # Strip NUL and non-whitespace control characters
    sanitized = _CONTROL_CHAR_RE.sub("", content).strip()

    if not sanitized:
        return GuardResult(
            content="",
            local_only=local_only,
            rejected=True,
            reason="Memory content cannot be empty or solely control characters.",
            warnings=(),
        )

    # Bound the work done by the credential/poison regexes. The memory store
    # rejects content over `MAX_CONTENT_LENGTH` anyway, but that check happened
    # *after* the guard, so a multi-megabyte unbroken token (base64 blob,
    # minified code) spent seconds in regex matching before being refused.
    from .memory import MAX_CONTENT_LENGTH

    if len(sanitized) > MAX_CONTENT_LENGTH:
        return GuardResult(
            content=sanitized,
            local_only=local_only,
            rejected=True,
            reason=(
                f"content length ({len(sanitized)} chars) exceeds maximum allowed "
                f"length ({MAX_CONTENT_LENGTH} chars)."
            ),
            warnings=(),
        )

    # 1. Prompt injection / poison check. `_is_poison_attempt()` applies NFKC
    # normalization internally so full-width / compatibility characters cannot
    # hide an instruction phrase from the regex while the context renderer
    # normalizes it back into one.
    if _is_poison_attempt(sanitized):
        return GuardResult(
            content=sanitized,
            local_only=local_only,
            rejected=True,
            reason="Prompt injection or adversarial instruction pattern detected in memory content.",
            warnings=(),
        )

    # 2. Credential / secret check
    warnings: list[str] = []
    final_local_only = bool(local_only)
    final_content = sanitized

    if _is_credential(sanitized):
        final_content = _redact_secrets_for_cloud(sanitized)
        final_local_only = True
        warnings.append("Credential or secret pattern detected: secret was redacted and forced to private (local_only).")

    return GuardResult(
        content=final_content,
        local_only=final_local_only,
        rejected=False,
        reason=None,
        warnings=tuple(warnings),
    )

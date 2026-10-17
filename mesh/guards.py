"""
Ingestion guards for the Mesh context layer.
Sanitizes input, detects adversarial prompt injection patterns,
and auto-redacts credentials while enforcing local_only (private) storage.
"""

from __future__ import annotations

import re
from typing import NamedTuple, Optional

from .passive import (
    _is_credential,
    _is_poison_attempt,
    _redact_secrets_for_cloud,
)

# Strip control characters (C0/C1) except newline (\n) and tab (\t)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


class GuardResult(NamedTuple):
    content: str
    local_only: bool
    rejected: bool
    reason: Optional[str] = None
    warnings: list[str] = []


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
            warnings=[],
        )

    # 1. Prompt injection / poison check
    if _is_poison_attempt(sanitized):
        return GuardResult(
            content=sanitized,
            local_only=local_only,
            rejected=True,
            reason="Prompt injection or adversarial instruction pattern detected in memory content.",
            warnings=[],
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
        warnings=warnings,
    )

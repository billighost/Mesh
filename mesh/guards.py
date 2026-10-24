"""
mesh/guards.py — Ingestion guards for the Mesh context layer.

Sanitizes input, detects adversarial prompt injection patterns (across NFKC,
homoglyphs, inline markdown, paraphrases, and multilingual overrides),
validates tags, and auto-redacts credentials locally while enforcing `local_only=True`.

Addresses Findings:
  - S2, S3, S4: Uses unified `mesh.secrets` detection and local redaction
  - S8: Confusable homoglyph folding, inline markdown stripping, and expanded poison detection
  - S10: Zero private imports from `passive.py`; uses `redact_secrets_local` (preserving emails)
  - S11: Idempotent on already-redacted placeholders (`--force-public` safe) + `guard_tags()`
"""
from __future__ import annotations

from collections import OrderedDict
import re
import threading
import unicodedata
from typing import Iterable, NamedTuple, Optional

from .c_bridge import c_guard_prescan as _c_guard_prescan

from .secrets import (
    INVISIBLE_CHAR_CLASS,
    ZERO_WIDTH_RE,
    is_credential,
    redact_secrets_local,
)

_GUARD_INGEST_CACHE: OrderedDict[tuple[str, bool], "GuardResult"] = OrderedDict()
_POISON_CACHE: OrderedDict[str, bool] = OrderedDict()
_TAGS_CACHE: OrderedDict[tuple[tuple[str, ...], bool], tuple[list[str], bool, tuple[str, ...]]] = OrderedDict()
_GUARD_CACHE_LOCK = threading.Lock()
_MAX_GUARD_CACHE = 2048


def clear_guard_caches() -> None:
    with _GUARD_CACHE_LOCK:
        _GUARD_INGEST_CACHE.clear()
        _POISON_CACHE.clear()
        _TAGS_CACHE.clear()

_INVISIBLE_CHAR_CLASS = INVISIBLE_CHAR_CLASS
_ZERO_WIDTH_RE = ZERO_WIDTH_RE

# Strip control characters (C0/C1) except newline (\n) and tab (\t), plus invisible formatting chars
_CONTROL_CHAR_RE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f" + INVISIBLE_CHAR_CLASS + "]"
)

# Finding S8: Confusable Cyrillic / Greek homoglyphs mapped to ASCII Latin equivalents
_CONFUSABLE_MAP = str.maketrans(
    {
        "\u0430": "a", "\u0410": "A",  # Cyrillic a
        "\u0435": "e", "\u0415": "E",  # Cyrillic e
        "\u0456": "i", "\u0406": "I",  # Cyrillic i
        "\u043e": "o", "\u041e": "O",  # Cyrillic o
        "\u0440": "p", "\u0420": "P",  # Cyrillic er -> p
        "\u0441": "c", "\u0421": "C",  # Cyrillic es -> c
        "\u0443": "y", "\u0423": "Y",  # Cyrillic u -> y
        "\u0445": "x", "\u0425": "X",  # Cyrillic ha -> x
        "\u0455": "s", "\u0405": "S",  # Cyrillic dze -> s
        "\u0458": "j", "\u0408": "J",  # Cyrillic je -> j
        "\u03bf": "o", "\u039f": "O",  # Greek omicron -> o
        "\u03b1": "a", "\u0391": "A",  # Greek alpha -> a
        "\u03b5": "e", "\u0395": "E",  # Greek epsilon -> e
        "\u03b9": "i", "\u0399": "I",  # Greek iota -> i
        "\u03ba": "k", "\u039a": "K",  # Greek kappa -> k
        "\u03c1": "p", "\u03a1": "P",  # Greek rho -> p
        "\u03c4": "t", "\u03a4": "T",  # Greek tau -> t
    }
)

# Finding S8: Strip inline markdown formatting (`**`, `*`, `__`, `_`, `` ` ``, `~~`) used to split words
_INLINE_MARKUP_BETWEEN_LETTERS_RE = re.compile(r"(?<=[A-Za-z])(?:[*_`~]{1,3})(?=[A-Za-z])")

# Finding S8: Expanded prompt-injection / instruction-override / structural breakout patterns
_POISON_PATTERNS = [
    # English override / ignore / disregard / forget / bypass with up to 4 qualifiers
    r"(?i)\b(?:ignore|disregard|forget|override|bypass)\s+"
    r"(?:(?:all|any|the|every|previous|prior|above|earlier|preceding|system|developer|user|safety|security|initial|original|existing)\s+){1,5}"
    r"(?:instructions?|prompts?|rules?|directives?|guidelines?|constraints?|guardrails?|directions?|commands?|policies)\b",
    r"(?i)\bdisregard\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|system|earlier|preceding)\b",
    # Paraphrase overrides ("do not follow earlier directives", "important system override")
    r"(?i)\b(?:do\s+not|don't|never)\s+(?:follow|obey|adhere\s+to)\s+"
    r"(?:(?:any|all|the|previous|prior|above|earlier|preceding|system|developer)\s+){1,4}"
    r"(?:instructions?|prompts?|rules?|directives?|guidelines?|constraints?)\b",
    r"(?i)\b(?:important|critical|urgent)\s+(?:system\s+|security\s+)?override\b",
    r"(?i)\byou\s+are\s+now\s+(?:in\s+)?(?:developer|dan|unrestricted|jailbreak|god)\s+mode\b",
    # Structural prompt / ChatML / XML breakout tags (Finding S8 / N7)
    r"(?i)</?\s*mesh_?context\b",
    r"(?i)</?\s*system(?:_?prompt|_?instructions)?\s*>",
    r"(?i)<\|(?:im_start|im_end|system|user|assistant|endoftext)\|>",
    r"(?i)\[/?INST\]",
    r"(?i)\bsystem\s*_?prompt\s*:",
    # Multilingual prompt injection overrides (Spanish, French, German, Portuguese)
    r"(?i)\b(?:ignora|ignorez|ignoriere|ignorem)\s+"
    r"(?:todas?\s+|toutes?\s+|alle\s+)?(?:las?\s+|les?\s+|as\s+)?"
    r"(?:instrucciones|instructions|anweisungen|instruções|reglas|règles|regeln)\s*"
    r"(?:anteriores|précédentes|vorherigen|anteriores)?\b",
]

_COMPILED_POISON = [re.compile(p) for p in _POISON_PATTERNS]


def _normalize_for_poison_check(text: str) -> str:
    """
    Normalize text for adversarial prompt-injection detection (Finding S8):
      1. Remove invisible format / zero-width characters.
      2. Apply Unicode NFKC normalization (converts fullwidth Latin to ASCII).
      3. Strip inline markdown emphasis/backticks splitting letters (`Ig**no**re` -> `Ignore`).
      4. Fold Cyrillic/Greek confusable homoglyphs to Latin (`Ign\u043ere` -> `Ignore`).
    """
    s = ZERO_WIDTH_RE.sub("", str(text or ""))
    s = unicodedata.normalize("NFKC", s)
    # Apply twice in case of nested inline markers
    s = _INLINE_MARKUP_BETWEEN_LETTERS_RE.sub("", s)
    s = _INLINE_MARKUP_BETWEEN_LETTERS_RE.sub("", s)
    return s.translate(_CONFUSABLE_MAP)


def is_poison_attempt(text: str) -> bool:
    """Return True if `text` matches any prompt-injection or structural breakout pattern."""
    # Fast-path: C pre-scanner guarantees no trigger tokens and clean ASCII — skip regex.
    if isinstance(text, str) and 0 < len(text.strip()) <= 16384 and _c_guard_prescan(text):
        return False

    if isinstance(text, str) and len(text) <= 4096:
        with _GUARD_CACHE_LOCK:
            res = _POISON_CACHE.get(text)
            if res is not None:
                _POISON_CACHE.move_to_end(text)
                return res

    norm = _normalize_for_poison_check(text)
    if not norm:
        res = False
    else:
        res = any(p.search(norm) for p in _COMPILED_POISON)

    if isinstance(text, str) and len(text) <= 4096:
        with _GUARD_CACHE_LOCK:
            if len(_POISON_CACHE) >= _MAX_GUARD_CACHE:
                _POISON_CACHE.popitem(last=False)
            _POISON_CACHE[text] = res
    return res


_is_poison_attempt = is_poison_attempt


class GuardResult(NamedTuple):
    content: str
    local_only: bool
    rejected: bool
    reason: Optional[str] = None
    warnings: tuple[str, ...] = ()


def _max_content_len() -> int:
    from .memory import MAX_CONTENT_LENGTH

    return int(MAX_CONTENT_LENGTH)


def guard_ingest(content: str, local_only: bool = False) -> GuardResult:
    """
    Validate and sanitize content before memory ingestion across all entry points:
      1. Strip NUL, unexpected C0/C1 control characters, and invisible characters (preserving \\n and \\t).
      2. Enforce `MAX_CONTENT_LENGTH` before running regexes (DoS defense).
      3. Reject adversarial prompt-injection / breakout patterns (`is_poison_attempt`).
      4. Detect credentials (`is_credential`) and redact secret values via `redact_secrets_local`
         (preserving non-secret emails locally) while forcing `local_only=True`.
    """
    if not isinstance(content, str):
        content = str(content or "")

    # Fast-path: C pre-scanner guarantees clean ASCII, no secret patterns, no injection tokens.
    # Skip all regex work and return accepted result immediately (< 0.002 ms).
    _stripped = content.strip()
    if _stripped and 0 < len(_stripped) <= min(16384, _max_content_len()) and _c_guard_prescan(content):
        return GuardResult(
            content=_stripped,
            local_only=bool(local_only),
            rejected=False,
            reason=None,
            warnings=(),
        )

    cache_key = (content, bool(local_only))
    if len(content) <= 4096:
        with _GUARD_CACHE_LOCK:
            cached = _GUARD_INGEST_CACHE.get(cache_key)
            if cached is not None:
                _GUARD_INGEST_CACHE.move_to_end(cache_key)
                return cached

    sanitized = _CONTROL_CHAR_RE.sub("", content).strip()
    if not sanitized:
        res = GuardResult(
            content="",
            local_only=bool(local_only),
            rejected=True,
            reason="Memory content cannot be empty or solely control characters.",
            warnings=(),
        )
        if len(content) <= 4096:
            with _GUARD_CACHE_LOCK:
                if len(_GUARD_INGEST_CACHE) >= _MAX_GUARD_CACHE:
                    _GUARD_INGEST_CACHE.popitem(last=False)
                _GUARD_INGEST_CACHE[cache_key] = res
        return res

    from .memory import MAX_CONTENT_LENGTH

    if len(sanitized) > MAX_CONTENT_LENGTH:
        res = GuardResult(
            content=sanitized,
            local_only=bool(local_only),
            rejected=True,
            reason=(
                f"content length ({len(sanitized)} chars) exceeds maximum allowed "
                f"length ({MAX_CONTENT_LENGTH} chars)."
            ),
            warnings=(),
        )
        if len(content) <= 4096:
            with _GUARD_CACHE_LOCK:
                if len(_GUARD_INGEST_CACHE) >= _MAX_GUARD_CACHE:
                    _GUARD_INGEST_CACHE.popitem(last=False)
                _GUARD_INGEST_CACHE[cache_key] = res
        return res

    if is_poison_attempt(sanitized):
        res = GuardResult(
            content=sanitized,
            local_only=bool(local_only),
            rejected=True,
            reason="Prompt injection or adversarial instruction pattern detected in memory content.",
            warnings=(),
        )
        if len(content) <= 4096:
            with _GUARD_CACHE_LOCK:
                if len(_GUARD_INGEST_CACHE) >= _MAX_GUARD_CACHE:
                    _GUARD_INGEST_CACHE.popitem(last=False)
                _GUARD_INGEST_CACHE[cache_key] = res
        return res

    warnings: list[str] = []
    final_local_only = bool(local_only)
    final_content = sanitized

    # Finding S2, S3, S4, S10, S11: Only flag and redact live credentials (not already-redacted placeholders)
    if is_credential(sanitized):
        final_content = redact_secrets_local(sanitized)
        final_local_only = True
        warnings.append(
            "Credential or secret pattern detected: secret was redacted and forced to private (local_only)."
        )

    res = GuardResult(
        content=final_content,
        local_only=final_local_only,
        rejected=False,
        reason=None,
        warnings=tuple(warnings),
    )
    if len(content) <= 4096:
        with _GUARD_CACHE_LOCK:
            if len(_GUARD_INGEST_CACHE) >= _MAX_GUARD_CACHE:
                _GUARD_INGEST_CACHE.popitem(last=False)
            _GUARD_INGEST_CACHE[cache_key] = res
    return res


def guard_tags(
    tags: Optional[Iterable[str]],
    local_only: bool = False,
) -> tuple[list[str], bool, tuple[str, ...]]:
    """
    Validate and sanitize memory tags against prompt injection and embedded secrets (Finding S11).
    Raises `ValueError` if any tag contains a prompt-injection payload or exceeds 128 chars.
    Redacts any secret inside a tag and sets `local_only=True`.
    """
    if not tags:
        return [], bool(local_only), ()

    tags_tuple: Optional[tuple[str, ...]] = None
    cache_key: Optional[tuple[tuple[str, ...], bool]] = None
    if isinstance(tags, (list, tuple)):
        try:
            tags_tuple = tuple(str(t) for t in tags)
            if sum(len(t) for t in tags_tuple) <= 4096:
                cache_key = (tags_tuple, bool(local_only))
                with _GUARD_CACHE_LOCK:
                    cached = _TAGS_CACHE.get(cache_key)
                    if cached is not None:
                        _TAGS_CACHE.move_to_end(cache_key)
                        return list(cached[0]), cached[1], cached[2]
            else:
                tags_tuple = None
        except Exception:
            tags_tuple = None
            cache_key = None

    clean_tags: list[str] = []
    final_local_only = bool(local_only)
    warnings: list[str] = []

    for raw_tag in tags:
        t = _CONTROL_CHAR_RE.sub("", str(raw_tag or "")).strip()
        if not t:
            continue
        if len(t) > 128:
            raise ValueError("Individual tag length cannot exceed 128 characters.")
        if is_poison_attempt(t):
            raise ValueError(
                f"Prompt injection or adversarial instruction pattern detected in tag '{t[:40]}'."
            )
        if is_credential(t):
            t = redact_secrets_local(t)
            final_local_only = True
            warnings.append("Credential pattern detected in tag: redacted and forced to private (local_only).")
        if t not in clean_tags:
            clean_tags.append(t)

    warnings_tuple = tuple(warnings)
    if cache_key is not None:
        with _GUARD_CACHE_LOCK:
            if len(_TAGS_CACHE) >= _MAX_GUARD_CACHE:
                _TAGS_CACHE.popitem(last=False)
            _TAGS_CACHE[cache_key] = (list(clean_tags), final_local_only, warnings_tuple)

    return clean_tags, final_local_only, warnings_tuple

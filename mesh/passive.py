"""
mesh/passive.py — Passive memory capture from agent session logs.

Reads a conversation log (raw text, file path, directory path, or JSONL)
and extracts facts worth storing in Mesh.

Security & Privacy Hardening:
  - Automatically redacts API keys, bearer tokens, passwords, and secrets via
    `_redact_secrets_for_cloud()` BEFORE sending any log text to external LLM providers.
  - Asks the user for explicit `y/n` permission before uploading logs to cloud LLM APIs.
  - Marks any locally extracted credential-bearing sentences as `local_only=True`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Optional

from . import ui
from .audit import validate_namespace
from .daemon import request_user_permission
from .memory import VALID_MEMORY_TYPES, Mesh

EXTRACTION_PROMPT = """You are a memory extraction assistant for Mesh, a shared agent memory system.
Given a conversation or session log between a human and an AI agent, extract facts, decisions,
processes, instructions, preferences, and discoveries that would be valuable to remember for future sessions.

Rules:
- Only extract concrete, specific, reusable facts
- Do NOT extract opinions, casual conversation, or things only relevant in the moment
- Do NOT extract raw passwords, API keys, or secrets in plain text without marking them private=true
- Each memory should be a single, self-contained sentence
- Prefer specifics over generalities: "the deploy script is at ./scripts/deploy.sh" not "there is a deploy script"

Return ONLY a JSON array of objects. No preamble, no explanation, no markdown fences.
Example:
  [{"content": "the staging server URL is staging.example.com", "type": "fact", "tags": ["infrastructure"], "private": false},
   {"content": "always run tests before merging to main", "type": "instruction", "tags": ["workflow"], "private": false},
   {"content": "API key is [REDACTED_SECRET]", "type": "fact", "tags": ["credentials"], "private": true}]

Valid types: fact, preference, context, result, instruction, process, decision, error, general
If nothing worth remembering was found, return an empty array: []

Session log to analyze:
"""

_TYPE_ALIASES = {
    "rule": "instruction",
    "guideline": "instruction",
    "workflow": "process",
    "choice": "decision",
    "bug": "error",
    "fix": "result",
    "outcome": "result",
    "background": "context",
    "setting": "preference",
    "note": "general",
}

_LOG_FILE_EXTENSIONS = {".log", ".txt", ".jsonl", ".md"}

# Redaction patterns applied BEFORE sending any log text to external LLM providers
_CLOUD_REDACTION_RULES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
        ),
        "[REDACTED_PRIVATE_KEY]",
    ),
    (
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
        ),
        "[REDACTED_JWT]",
    ),
    (
        re.compile(
            r"\b(?:sk-ant-[a-zA-Z0-9_-]{8,}|sk-[a-zA-Z0-9_-]{6,}|gh[pousr]_[a-zA-Z0-9]{12,}|github_pat_[a-zA-Z0-9_]{16,}|glpat-[a-zA-Z0-9_-]{12,}|hf_[a-zA-Z0-9]{16,}|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|xox[baprs]-[a-zA-Z0-9-]{10,}|AIza[0-9A-Za-z_-]{20,}|rk_live_[0-9a-zA-Z]{16,}|sk_live_[0-9a-zA-Z]{16,})\b"
        ),
        "[REDACTED_API_KEY]",
    ),
    (re.compile(r"(?i)\b(Bearer\s+)[^\s\"'`]{8,}"), r"\1[REDACTED_TOKEN]"),
    (
        # URL userinfo. Empty usernames (`redis://:pass@host`) and passwords that
        # contain `@` (`postgres://user:p@ss@host/db`) must both be fully redacted:
        # the password group is greedy so it ends at the *last* `@` before the host.
        # `[^\s]` (rather than `[^\s/]`) also catches unescaped `/` in a pasted
        # password, matching the detection rule below.
        re.compile(r"([a-zA-Z0-9+.-]{1,30}://[^/\s:@]*:)([^\s]*)(@[^@\s/]+)"),
        r"\1[REDACTED_PASSWORD]\3",
    ),
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|api[_\s-]?key|access[_\s-]?token|auth[_\s-]?token|refresh[_\s-]?token|private[_\s-]?key|client[_\s-]?secret|aws_secret_access_key|aws_session_token|token)"
            r"(['\"]?\s*(?:is|=|:)\s*['\"]?)([^\s,'\"`;\n[]{3,})"
        ),
        r"\1\2[REDACTED_SECRET]",
    ),
    (
        re.compile(r"(?i)\b(authorization\s*[:=]\s*)(basic|bearer|token)\s+([^\s,'\"`;\n]{3,})"),
        r"\1\2 [REDACTED_TOKEN]",
    ),
    (
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        "[REDACTED_EMAIL]",
    ),
]


def _redact_secrets_for_cloud(text: str) -> str:
    """
    Scrub API keys, tokens, URI passwords, and secret assignments from `text`
    before sending it to any external LLM API (OpenAI, Anthropic, Groq, etc.).
    """
    # Compatibility characters (full-width letters, mathematical alphanumerics,
    # etc.) would otherwise smuggle a key past ASCII regexes while an LLM still
    # reads it normally. Invisible separators are removed first: they are
    # preserved by NFKC and would otherwise split the secret across a regex.
    redacted = _ZERO_WIDTH_RE.sub("", text)
    redacted = unicodedata.normalize("NFKC", redacted)
    for pattern, replacement in _CLOUD_REDACTION_RULES:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _normalize_memory_type(raw_type: Optional[str]) -> str:
    """Ensure any LLM- or heuristic-generated memory type maps cleanly to VALID_MEMORY_TYPES."""
    if not raw_type:
        return "fact"
    cleaned = str(raw_type).strip().lower()
    if cleaned in VALID_MEMORY_TYPES:
        return cleaned
    return _TYPE_ALIASES.get(cleaned, "general")


def _resolve_log_input(log_input: str, max_chars: int = 16000) -> str:
    """
    Accept either raw log text OR a file/directory path string.
    If `log_input` points to an existing file or directory on disk, reads the recent
    log content automatically.
    """
    if not log_input or not log_input.strip():
        return ""
    stripped = log_input.strip()
    if "\n" not in stripped and len(stripped) < 1024:
        try:
            candidate = Path(stripped).expanduser()
            if candidate.exists():
                if candidate.is_file():
                    return _read_log_file(candidate, max_chars=max_chars)
                if candidate.is_dir():
                    files: list[tuple[int, Path]] = []
                    for child in candidate.rglob("*"):
                        if child.is_file() and child.suffix.lower() in _LOG_FILE_EXTENSIONS:
                            try:
                                files.append((child.stat().st_mtime_ns, child))
                            except OSError:
                                continue
                    files.sort(key=lambda x: x[0], reverse=True)
                    chunks: list[str] = []
                    total_len = 0
                    for _, fpath in files[:8]:
                        chunk = _read_log_file(fpath, max_chars=max_chars // 2)
                        if chunk:
                            chunks.append(chunk)
                            total_len += len(chunk)
                            if total_len >= max_chars:
                                break
                    # Files were selected newest-first, but the caller keeps the
                    # *tail* of this string. Join chronologically so truncation
                    # drops the oldest content instead of the newest log.
                    chunks.reverse()
                    return "\n\n".join(chunks)
        except OSError:
            pass
    return stripped


def _jsonl_record_text(value: Any) -> str:
    """
    Extract readable text from a JSONL record.

    Chat exports nest the text differently: OpenAI-style
    `{"message": {"content": "..."}}`, Anthropic-style
    `{"message": {"content": [{"type": "text", "text": "..."}]}}`, and simple
    `{"content": "..."}` / `{"text": "..."}` records. Returning "" for an object
    or list (the old behaviour) silently dropped entire session logs.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_jsonl_record_text(item) for item in value]
        return "\n".join(part for part in parts if part.strip())
    if isinstance(value, dict):
        for key in ("text", "content", "message"):
            if key in value:
                text = _jsonl_record_text(value[key])
                if text.strip():
                    return text
    return ""


def _read_log_file(path: Path, max_chars: int = 16000) -> str:
    """Read text from a log or JSONL file, keeping the tail if the file is very large."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > max_chars * 4:
                f.seek(max(0, size - max_chars * 4))
            raw = f.read().decode("utf-8", errors="ignore")
        if path.suffix.lower() == ".jsonl":
            extracted_lines: list[str] = []
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        text = _jsonl_record_text(obj)
                        if text.strip():
                            extracted_lines.append(text.strip())
                            continue
                    elif isinstance(obj, str) and obj.strip():
                        extracted_lines.append(obj.strip())
                        continue
                except Exception:
                    pass
                # Unknown structure: keep the raw line rather than silently
                # dropping a record that may contain the only useful fact.
                extracted_lines.append(line)
            return "\n".join(extracted_lines)[-max_chars:]
        return raw[-max_chars:]
    except OSError:
        return ""


def detect_provider() -> dict[str, Any]:
    """
    Auto-detect the best available AI provider.
    Returns a dict with: provider, api_key, model, base_url, available
    """
    explicit = os.environ.get("MESH_AI_PROVIDER", "").lower().strip()
    api_key_override = os.environ.get("MESH_AI_API_KEY", "").strip()
    model_override = os.environ.get("MESH_AI_MODEL", "").strip()
    base_url_override = os.environ.get("MESH_AI_BASE_URL", "").strip()

    # 1. Custom endpoint / base_url override checked first so MESH_AI_API_KEY + MESH_AI_BASE_URL work properly
    if api_key_override and base_url_override:
        return {
            "provider": "openai-compatible",
            "api_key": api_key_override,
            "model": model_override or "gpt-4o-mini",
            "base_url": base_url_override,
            "available": True,
        }

    if explicit == "anthropic":
        key = api_key_override or os.environ.get("ANTHROPIC_API_KEY", "")
        return {
            "provider": "anthropic",
            "api_key": key,
            "model": model_override or "claude-haiku-4-5-20251001",
            "base_url": None,
            "available": bool(key),
        }
    if explicit in ("openai",):
        key = api_key_override or os.environ.get("OPENAI_API_KEY", "")
        return {
            "provider": "openai",
            "api_key": key,
            "model": model_override or "gpt-4o-mini",
            "base_url": base_url_override or "https://api.openai.com/v1",
            "available": bool(key),
        }
    if explicit == "groq":
        key = api_key_override or os.environ.get("GROQ_API_KEY", "")
        return {
            "provider": "openai-compatible",
            "api_key": key,
            "model": model_override or "llama3-8b-8192",
            "base_url": base_url_override or "https://api.groq.com/openai/v1",
            "available": bool(key),
        }
    if explicit == "ollama":
        base = base_url_override or "http://localhost:11434/v1"
        return {
            "provider": "openai-compatible",
            "api_key": "ollama",
            "model": model_override or "llama3",
            "base_url": base,
            "available": _ollama_is_running(base),
        }
    if explicit == "openai-compatible":
        key = api_key_override or ""
        base = base_url_override or ""
        return {
            "provider": "openai-compatible",
            "api_key": key,
            "model": model_override or "gpt-4o-mini",
            "base_url": base,
            "available": bool(key and base),
        }
    if explicit == "local":
        return {
            "provider": "local",
            "api_key": None,
            "model": None,
            "base_url": None,
            "available": True,
        }

    # --- Auto-detect: try in priority order ---
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if anthropic_key and not api_key_override:
        return {
            "provider": "anthropic",
            "api_key": anthropic_key,
            "model": model_override or "claude-haiku-4-5-20251001",
            "base_url": None,
            "available": True,
        }

    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if openai_key and not api_key_override:
        return {
            "provider": "openai-compatible",
            "api_key": openai_key,
            "model": model_override or "gpt-4o-mini",
            "base_url": "https://api.openai.com/v1",
            "available": True,
        }

    groq_key = os.environ.get("GROQ_API_KEY", "")
    if groq_key and not api_key_override:
        return {
            "provider": "openai-compatible",
            "api_key": groq_key,
            "model": model_override or "llama3-8b-8192",
            "base_url": "https://api.groq.com/openai/v1",
            "available": True,
        }

    ollama_base = base_url_override or "http://localhost:11434/v1"
    if _ollama_is_running(ollama_base):
        return {
            "provider": "openai-compatible",
            "api_key": "ollama",
            "model": model_override or _detect_ollama_model(ollama_base),
            "base_url": ollama_base,
            "available": True,
        }

    if api_key_override:
        return {
            "provider": "openai-compatible",
            "api_key": api_key_override,
            "model": model_override or "gpt-4o-mini",
            "base_url": base_url_override or "https://api.openai.com/v1",
            "available": True,
        }

    return {
        "provider": "local",
        "api_key": None,
        "model": None,
        "base_url": None,
        "available": True,
    }


def _ollama_is_running(base_url: str) -> bool:
    try:
        import httpx

        r = httpx.get(base_url.replace("/v1", ""), timeout=1.5)
        return r.status_code < 500
    except Exception:
        return False


def _detect_ollama_model(base_url: str) -> str:
    try:
        import httpx

        r = httpx.get(base_url.replace("/v1", "/api/tags"), timeout=2.0)
        if r.status_code == 200:
            models = r.json().get("models", [])
            if models:
                return models[0]["name"]
    except Exception:
        pass
    return "llama3"


def _extract_with_anthropic(log_text: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    import httpx

    safe_log_text = _redact_secrets_for_cloud(log_text)
    response = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": config["api_key"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": config["model"],
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": EXTRACTION_PROMPT + safe_log_text}],
        },
        timeout=30,
    )
    if response.status_code == 401:
        raise ValueError("Invalid Anthropic API key. Check ANTHROPIC_API_KEY.")
    if response.status_code == 429:
        raise ValueError("Anthropic rate limit hit. Try again in a moment.")
    if response.status_code != 200:
        raise ValueError(f"Anthropic API error {response.status_code}: {response.text[:200]}")
    raw = response.json()["content"][0]["text"].strip()
    return _parse_json_response(raw)


def _extract_with_openai_compatible(log_text: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    import httpx

    safe_log_text = _redact_secrets_for_cloud(log_text)
    base_url = (config.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config['api_key']}",
    }
    response = httpx.post(
        f"{base_url}/chat/completions",
        headers=headers,
        json={
            "model": config["model"],
            "max_tokens": 1000,
            "temperature": 0.2,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a memory extraction assistant. Always respond with valid JSON only.",
                },
                {"role": "user", "content": EXTRACTION_PROMPT + safe_log_text},
            ],
        },
        timeout=30,
    )
    if response.status_code == 401:
        raise ValueError(f"Invalid API key for {base_url}. Check your MESH_AI_API_KEY.")
    if response.status_code == 429:
        raise ValueError("Rate limit hit. Try again in a moment.")
    if response.status_code != 200:
        raise ValueError(f"API error {response.status_code} from {base_url}: {response.text[:200]}")
    raw = response.json()["choices"][0]["message"]["content"].strip()
    return _parse_json_response(raw)


_SIGNAL_PATTERNS = [
    r"\bis at\b",
    r"\bis located at\b",
    r"\brunning on\b",
    r"\bport\s+\d+",
    r"\bURL is\b",
    r"\bpath is\b",
    r"\bscript is\b",
    r"\bcommand is\b",
    r"\balways\b",
    r"\bnever\b",
    r"\bdon't\b",
    r"\bdo not\b",
    r"\bmust\b",
    r"\bshould\b",
    r"\bwe decided\b",
    r"\bdecided to\b",
    r"\bwe use\b",
    r"\bwe're using\b",
    r"\bconfigured to\b",
    r"\bset to\b",
    r"\bdeployed on\b",
    r"\bstored in\b",
    r"\bprefers?\b",
    r"\bcredential",
    r"\bpassword\b",
    r"\bapi key\b",
    r"\btoken\b",
    r"\bsecret\b",
]
_CREDENTIAL_PATTERNS = [
    # Keyword + explicit assignment. A bare mention of "token" or "password"
    # (e.g. "rotate the token before Friday") is *not* a secret; only a
    # value-bearing assignment is. `$VAR` / `{...}` references are exempt so
    # configuration notes such as `token=$GITHUB_TOKEN` stay usable.
    r"(?i)\b(?:password|passwd|pwd|secret|api[_\s-]?key|access[_\s-]?token|"
    r"auth[_\s-]?token|refresh[_\s-]?token|private[_\s-]?key|client[_\s-]?secret|"
    r"aws_secret_access_key|aws_session_token|token)\b"
    r"['\"]?\s*[:=]\s*['\"]?(?!\$|\{)[^\s,'\"`;\[]{4,}",
    # Natural-language assignment ("the API key is sk-live-..."). Requiring 12
    # non-space characters keeps prose like "the token is stored in .env" out.
    r"(?i)\b(?:password|passwd|secret|api[_\s-]?key|private[_\s-]?key|token)\b"
    r"\s+(?:is|was)\s+['\"]?[A-Za-z0-9_\-./+=:@]{12,}",
    # Well-known credential shapes.
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----",
    r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
    r"\b(?:sk-ant-[a-zA-Z0-9_-]{8,}|sk-[a-zA-Z0-9_-]{6,}|gh[pousr]_[a-zA-Z0-9]{12,}|"
    r"github_pat_[a-zA-Z0-9_]{16,}|glpat-[a-zA-Z0-9_-]{12,}|hf_[a-zA-Z0-9]{16,}|"
    r"AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|xox[baprs]-[a-zA-Z0-9-]{10,}|"
    r"AIza[0-9A-Za-z_-]{20,}|rk_live_[0-9a-zA-Z]{16,}|sk_live_[0-9a-zA-Z]{16,})\b",
    r"(?i)\bBearer\s+[^\s\"'`]{8,}",
    # URL-embedded credentials, including empty usernames (`redis://:pass@host`).
    # The scheme length is bounded so a long unbroken token without `://` cannot
    # trigger quadratic backtracking (`[a-zA-Z0-9+.-]*` retried at every offset).
    r"[a-zA-Z][a-zA-Z0-9+.-]{0,30}://[^/\s:@]*:[^@\s]+@",
    # Authorization headers (`Authorization: Basic ...`, `authorization=bearer ...`)
    r"(?i)\bauthorization\s*[:=]\s*(?:basic|bearer|token)\b",
    r"\[REDACTED_(?:SECRET\vert{}API_KEY\vert{}TOKEN\vert{}PASSWORD\vert{}JWT\vert{}PRIVATE_KEY)\]",
]
_NOISE_PATTERNS = [
    r"^\s*(yes|no|ok|okay|sure|thanks|thank you|great|got it|understood|sounds good)\s*[.!]?\s*$",
    r"^\s*okay\s+sounds\s+good\b",
    r"^\s*i\s+(see|understand|think|feel|wonder|believe)\b",
    r"^\s*(can|could|would|should)\s+you\b",
    r"^\s*please\b",
    r"^\s*here\s+(is|are)\b",
    r"^\s*let me\b",
    r"^\s*i'll\b",
]

_POISON_PATTERNS = [
    r"(?i)\bignore\s+(?:all\s+|the\s+)?(?:(?:previous|prior|above|earlier|preceding)\s+)?(?:instructions?|prompts?|rules?)\b",
    r"(?i)\bdisregard\s+(?:all\s+|the\s+)?(?:previous|prior|above|system|earlier|preceding)\b",
    r"(?i)\bforget\s+(?:all\s+|the\s+)?(?:previous|prior|above|earlier)\s+(?:instructions?|prompts?|rules?)\b",
    r"(?i)\byou\s+are\s+now\s+in\s+developer\s+mode\b",
    r"(?i)</?\s*mesh_context\b",
    r"(?i)\bsystem\s*prompt\s*:",
]

# Invisible formatting characters (zero-width spaces/joiners, BOM, bidi controls,
# soft hyphen, tag characters) can split a poison phrase or a credential so the
# regex misses it while the reader (or a downstream prompt renderer) sees it
# normally. Normalize them away before matching or redacting. Escapes are used
# so no editor can silently turn them back into invisible source bytes.
_INVISIBLE_CHAR_CLASS = (
    r"\u00ad"                  # SOFT HYPHEN
    r"\u034f"                  # COMBINING GRAPHEME JOINER
    r"\u061c"                  # ARABIC LETTER MARK
    r"\u115f\u1160"            # Hangul fillers
    r"\u17b4\u17b5"            # Khmer inherent vowels
    r"\u180b-\u180f"           # Mongolian FVS + vowel separator
    r"\u200b-\u200f"           # zero-width space .. right-to-left mark
    r"\u202a-\u202e"           # bidi embeddings / overrides
    r"\u2060-\u2064"           # word joiner .. invisible plus
    r"\u2066-\u2069"           # bidi isolates
    r"\ufeff"                  # BOM / zero-width no-break space
    r"\ufe00-\ufe0f"           # variation selectors
    r"\uffa0"                  # halfwidth Hangul filler
    r"\U000e0000-\U000e007f"   # tag characters
    r"\U000e0100-\U000e01ef"   # variation selectors supplement
)
_ZERO_WIDTH_RE = re.compile(f"[{_INVISIBLE_CHAR_CLASS}]")

_compiled_signals = [re.compile(p, re.IGNORECASE) for p in _SIGNAL_PATTERNS]
_compiled_credentials = [re.compile(p, re.IGNORECASE) for p in _CREDENTIAL_PATTERNS]
_compiled_noise = [re.compile(p, re.IGNORECASE) for p in _NOISE_PATTERNS]
_compiled_poison = [re.compile(p) for p in _POISON_PATTERNS]


def _is_credential(sentence: str) -> bool:
    normalized = _ZERO_WIDTH_RE.sub("", sentence)
    normalized = unicodedata.normalize("NFKC", normalized)
    return any(p.search(normalized) for p in _compiled_credentials)


def _is_noise(sentence: str) -> bool:
    return any(p.match(sentence) for p in _compiled_noise)


def _is_poison_attempt(sentence: str) -> bool:
    # Strip invisible separators, then apply NFKC: full-width / compatibility
    # characters render as their ASCII equivalents downstream, so matching on
    # the raw string would let them split an instruction phrase.
    normalized = _ZERO_WIDTH_RE.sub("", sentence)
    normalized = unicodedata.normalize("NFKC", normalized)
    return any(p.search(normalized) for p in _compiled_poison)


def _has_signal(sentence: str) -> bool:
    return any(p.search(sentence) for p in _compiled_signals)


def _infer_type(sentence: str) -> str:
    s = sentence.lower()
    if any(w in s for w in ["decided", "decision", "chose", "going with", "we'll use"]):
        return "decision"
    if any(w in s for w in ["prefer", "prefers", "preference"]):
        return "preference"
    if any(w in s for w in ["always", "never", "must", "should", "don't", "do not", "process", "workflow"]):
        return "process"
    if any(w in s for w in ["error", "bug", "issue", "problem", "broken", "fails", "crash"]):
        return "error"
    return "fact"


def _extract_local(log_text: str) -> list[dict[str, Any]]:
    sentences = re.split(r"(?<=[.!?])\s+|\n", log_text)
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in sentences:
        sentence = raw.strip()
        if len(sentence) < 20 or len(sentence) > 300:
            continue
        if _is_noise(sentence) or _is_poison_attempt(sentence):
            continue
        key = sentence.lower()[:60]
        if key in seen:
            continue
        if _has_signal(sentence):
            is_private = _is_credential(sentence)
            if is_private:
                sentence = _redact_secrets_for_cloud(sentence)
            results.append(
                {
                    "content": sentence,
                    "type": _infer_type(sentence),
                    "tags": ["auto-captured", "local-extraction"],
                    "private": is_private,
                }
            )
            seen.add(key)
        if len(results) >= 15:
            break
    return results


def _parse_json_response(raw: str) -> list[dict[str, Any]]:
    if "```" in raw:
        parts = raw.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("["):
                raw = part
                break
    raw = raw.strip()
    if not raw.startswith("["):
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if match:
            raw = match.group(0)
        else:
            return []
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        return []
    # A model may return `["some text"]`; callers index these as mappings.
    return [item for item in parsed if isinstance(item, dict)]


def extract_memories_from_log(
    log_text: str,
    namespace: str = "shared",
    agent_id: str = "passive-capture",
    dry_run: bool = False,
    verbose: bool = False,
    force_local: bool = False,
    assume_yes: bool = False,
    default_local_only: bool = False,
    resolve_input: bool = True,
) -> dict[str, Any]:
    """
    Extract and store memories from a session log (or file/directory path).
    Asks the user for `y/n` permission before uploading redacted logs to external LLM providers.

    Reports `captured_count`, `stored_count` (new rows only), `deduplicated_count`,
    `skipped_count`, and `error` separately (Issue M5).
    """
    clean_ns = validate_namespace(namespace)
    resolved_text = _resolve_log_input(log_text) if resolve_input else str(log_text or "")
    if not resolved_text.strip():
        return {
            "captured_count": 0,
            "stored_count": 0,
            "deduplicated_count": 0,
            "skipped_count": 0,
            "provider_used": "none",
            "memories": [],
            "error": None,
        }

    if len(resolved_text) > 8000:
        resolved_text = "[earlier content truncated]\n\n" + resolved_text[-8000:]

    if force_local:
        config: dict[str, Any] = {"provider": "local", "available": True}
    else:
        config = detect_provider()

    # A configured-but-unavailable provider must degrade to local *before* the
    # dispatch below: otherwise the log is POSTed to the cloud endpoint without
    # the consent prompt (which is gated on `available`).
    if config["provider"] != "local" and not config.get("available"):
        if verbose:
            ui.info(
                f"Configured provider '{config['provider']}' is unavailable — using local keyword extraction"
            )
        config = {"provider": "local", "available": True}

    # Prompt user for permission before sending log text to external LLM APIs
    if config["provider"] != "local":
        provider_name = config["provider"]
        if not request_user_permission(
            f"send redacted session log text to external cloud LLM provider '{provider_name}' for memory extraction",
            assume_yes=assume_yes,
        ):
            if verbose:
                ui.info("Cloud extraction declined — using local keyword extraction instead")
            config = {"provider": "local", "available": True}

    if verbose:
        if config["provider"] == "local":
            ui.info("Using local keyword extraction", "No cloud AI calls are made.")
        else:
            provider_label = config["provider"]
            model_label = config.get("model", "")
            base_label = f" ({config['base_url']})" if config.get("base_url") else ""
            ui.info(f"Using {provider_label} / {model_label}", base_label.strip(" ()") or None)

    extracted: list[dict[str, Any]] = []
    extraction_error: Optional[str] = None
    try:
        if config["provider"] == "anthropic":
            extracted = _extract_with_anthropic(resolved_text, config)
            provider_used = f"anthropic/{config['model']}"
        elif config["provider"] in ("openai", "openai-compatible"):
            extracted = _extract_with_openai_compatible(resolved_text, config)
            base = config.get("base_url", "")
            provider_used = f"openai-compatible/{config['model']} ({base})"
        else:
            extracted = _extract_local(resolved_text)
            provider_used = "local-keyword-extractor"
    except Exception as e:
        extraction_error = str(e)
        if verbose:
            ui.warn(f"AI extraction failed: {e}", "Falling back to local keyword extraction.")
        try:
            extracted = _extract_local(resolved_text)
            provider_used = "local-keyword-extractor (fallback)"
        except Exception as fallback_err:
            if verbose:
                ui.error(f"Local fallback also failed: {fallback_err}")
            return {
                "captured_count": 0,
                "stored_count": 0,
                "deduplicated_count": 0,
                "skipped_count": 0,
                "provider_used": "none",
                "memories": [],
                "error": extraction_error,
            }

    if not extracted:
        if verbose:
            ui.info("Nothing worth remembering was found in that log")
        return {
            "captured_count": 0,
            "stored_count": 0,
            "deduplicated_count": 0,
            "skipped_count": 0,
            "provider_used": provider_used,
            "memories": [],
            "error": extraction_error,
        }

    if dry_run:
        ui.header("Capture preview", clean_ns, "dry run")
        ui.kv([("Would store", ui.plural(len(extracted), "memory", "memories")), ("Provider", str(provider_used))])
        print()
        for mem in extracted:
            preview_content = str(mem.get("content", ""))
            private = (
                default_local_only
                or bool(mem.get("private"))
                or _is_credential(preview_content)
            )
            private_flag = "  " + ui.badge("private", "accent") if private else ""
            mtype = _normalize_memory_type(mem.get("type"))
            print(f"{ui.IND}{ui.style(ui.sym('bullet'), 'muted')} {ui.style(mtype, 'info')}{private_flag}  {' '.join(str(mem.get('content', '')).split())}")
        print()
        ui.hint("Nothing was written. Re-run without --dry-run to store these.", stream=sys.stdout)
        return {
            "captured_count": len(extracted),
            "stored_count": 0,
            "deduplicated_count": 0,
            "skipped_count": len(extracted),
            "provider_used": provider_used,
            "memories": extracted,
            "error": extraction_error,
        }

    mesh = Mesh(namespace=clean_ns, agent_id=agent_id)
    captured = 0
    stored = 0
    deduplicated = 0
    skipped = 0
    for mem in extracted:
        content = str(mem.get("content") or "").strip()
        if not content:
            skipped += 1
            continue
        if _is_poison_attempt(content):
            skipped += 1
            if verbose:
                ui.warn(f"Ignored suspicious content: {ui.truncate(content, 60)}")
            continue
        try:
            mtype = _normalize_memory_type(mem.get("type", "general"))
            raw_tags = mem.get("tags") or []
            if not isinstance(raw_tags, list):
                raw_tags = [str(raw_tags)]
            tags = list(dict.fromkeys([*(str(t) for t in raw_tags), "auto-captured"]))
            has_credential = _is_credential(content)
            is_private = (
                default_local_only
                or bool(mem.get("private", False))
                or has_credential
            )
            # Redact only content that actually carries a credential. Redacting
            # every private fact (the old behaviour when `default_local_only`
            # was set) destroyed useful data — emails became [REDACTED_EMAIL]
            # even though the row never leaves the local database.
            if has_credential:
                content = _redact_secrets_for_cloud(content)
            learn_res = mesh.learn(
                content=content,
                memory_type=mtype,
                tags=tags,
                local_only=is_private,
                confidence=0.8,
            )
            captured += 1
            if learn_res.get("deduplicated"):
                deduplicated += 1
            else:
                stored += 1
            if verbose:
                private_flag = "  " + ui.badge("private", "accent") if is_private else ""
                ui.success(f"{ui.truncate(content, 80)}{private_flag}")
        except Exception as e:
            skipped += 1
            if extraction_error is None:
                extraction_error = str(e)
            if verbose:
                ui.error(f"Couldn't store \"{ui.truncate(content, 60)}\": {e}")

    return {
        "captured_count": captured,
        "stored_count": stored,
        "deduplicated_count": deduplicated,
        "skipped_count": skipped,
        "provider_used": provider_used,
        "memories": extracted,
        "error": extraction_error,
    }


def capture_cmd() -> None:
    """Entry point for: mesh-capture"""
    parser = argparse.ArgumentParser(
        description="Extract and store memories from a session log automatically.",
        epilog=""" Examples:   mesh-capture --file session.log   mesh-capture --file session.log --dry-run --verbose   cat output.txt | mesh-capture --stdin   mesh-capture --file session.log --local-only        """,
    )
    parser.add_argument("--file", help="Path to session log file or directory")
    parser.add_argument("--stdin", action="store_true", help="Read log from stdin")
    parser.add_argument(
        "--namespace",
        default=None,
        help="Mesh namespace (default: MESH_NAMESPACE env var or 'shared')",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be stored without writing anything",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed progress and provider info",
    )
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="Force local keyword extraction, skip all AI providers",
    )
    parser.add_argument(
        "--show-provider",
        action="store_true",
        help="Print which provider would be used and exit",
    )
    parser.add_argument("--yes", "-y", action="store_true", help="Skip interactive y/n permission prompts")
    args = parser.parse_args()

    if args.show_provider:
        config = detect_provider()
        if config["provider"] == "local":
            ui.header("Capture provider")
            print()
            ui.kv([("Provider", "local keyword extractor"), ("AI key", ui.style("none found", "muted"))])
            ui.hint("For AI extraction set ANTHROPIC_API_KEY, OPENAI_API_KEY or GROQ_API_KEY, or start Ollama.", stream=sys.stdout)
        else:
            ui.header("Capture provider")
            print()
            ui.kv(
                [
                    ("Provider", str(config["provider"])),
                    ("Model", str(config.get("model", "")) or None),
                    ("Base URL", str(config["base_url"]) if config.get("base_url") else None),
                ]
            )
        return

    if not args.file and not args.stdin:
        parser.print_help()
        ui.error("Tell Mesh where to read from", "Use --file <path> or pipe a log in with --stdin.")
        sys.exit(1)

    if args.stdin:
        # Bound memory use: a piped multi-GB log must not be read wholesale.
        log_text = sys.stdin.read(1_000_000)
    else:
        path = Path(args.file).expanduser()
        if not path.exists():
            ui.fail(f"File not found: {args.file}", "Check the path, or pipe the log in with --stdin.")
        log_text = _resolve_log_input(str(path))

    ns = validate_namespace(args.namespace or os.environ.get("MESH_NAMESPACE", "shared"))
    result = extract_memories_from_log(
        log_text=log_text,
        namespace=ns,
        dry_run=args.dry_run,
        verbose=args.verbose,
        force_local=args.local_only,
        assume_yes=args.yes,
        # Both branches above already read the file/stdin; re-resolving would
        # treat log *content* that looks like a path as another file to read.
        resolve_input=False,
    )
    if not args.dry_run:
        provider_label = result.get("provider_used", "unknown")
        dedup_cnt = int(result.get("deduplicated_count", 0))
        detail_parts = [f"{result['skipped_count']} skipped"]
        if dedup_cnt:
            detail_parts.append(f"{dedup_cnt} deduplicated")
        detail_parts.append(f"provider: {provider_label}")
        detail_str = f"  {ui.sym('dot')}  ".join(detail_parts)
        print()
        if result["stored_count"] or dedup_cnt:
            ui.success(
                f"Captured {ui.plural(result['stored_count'], 'new memory', 'new memories')} into {ns}",
                detail_str,
            )
            ui.hint("See them with `mesh inspect`", stream=sys.stdout)
        else:
            ui.info("Nothing was stored", detail_str)
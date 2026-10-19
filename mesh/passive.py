"""
mesh/passive.py — Passive memory capture from agent session logs.

Reads a conversation log (raw text, file path, directory path, or JSONL)
and extracts facts worth storing in Mesh.

Addresses Findings:
  - S2, S3, S4, S10, N2: Delegates credential detection, local redaction, cloud redaction, and
                         poison detection to `mesh.secrets` and `mesh.guards`
  - S7: Never implicitly uploads session logs to cloud providers merely because `MESH_AUTO_APPROVE=1`
        and an ambient API key are present; requires explicit cloud provider opt-in or dedicated
        per-invocation consent (`MESH_ALLOW_CLOUD_CAPTURE=1` or interactive terminal confirmation)
  - S8: Tags passively captured memories with `trust_tier="log_captured"` and supports automatic
        quarantine (`quarantine=True` when `MESH_QUARANTINE_CAPTURE=1` or `quarantine=True` is passed)
  - N1: Confines `_resolve_log_input` to non-symlinked `.log`/`.txt`/`.jsonl`/`.md` files and
        bounded directory scans (skipping hidden/dot directories like `.ssh`, `.aws`, `.gnupg`)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

from . import ui
from .audit import reject_symlink, validate_namespace
from .envutil import env_bool
from .guards import (
    INVISIBLE_CHAR_CLASS,
    ZERO_WIDTH_RE,
    is_poison_attempt,
)
from .memory import VALID_MEMORY_TYPES, Mesh
from .secrets import (
    is_credential,
    redact_secrets_for_cloud,
    redact_secrets_local,
)

# Backwards-compatible aliases for internal callers and existing test suites
_INVISIBLE_CHAR_CLASS = INVISIBLE_CHAR_CLASS
_ZERO_WIDTH_RE = ZERO_WIDTH_RE
_is_credential = is_credential
_is_poison_attempt = is_poison_attempt
_redact_secrets_for_cloud = redact_secrets_for_cloud
_redact_secrets_local = redact_secrets_local

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
_FORBIDDEN_FILENAMES = {
    ".env", ".env.local", ".env.production", ".netrc", ".pgpass",
    "id_rsa", "id_ed25519", "id_ecdsa", "authorized_keys", "known_hosts",
    "credentials", "config", "shadow", "passwd", "master.key", "auth_token",
}
_FORBIDDEN_DIRS = {
    ".ssh", ".aws", ".gnupg", ".kube", ".docker", ".config", ".git", ".mesh",
}


def _normalize_memory_type(raw_type: Optional[str]) -> str:
    """Ensure any LLM- or heuristic-generated memory type maps cleanly to VALID_MEMORY_TYPES."""
    if not raw_type:
        return "fact"
    cleaned = str(raw_type).strip().lower()
    if cleaned in VALID_MEMORY_TYPES:
        return cleaned
    return _TYPE_ALIASES.get(cleaned, "general")


def _is_safe_log_file_path(path: Path) -> bool:
    """
    Finding N1: Verify that `path` is a non-symlinked log file with an allowed extension
    (`.log`, `.txt`, `.jsonl`, `.md`) and does not reside inside sensitive dot-directories
    (`.ssh`, `.aws`, `.gnupg`, etc.).
    """
    try:
        if path.is_symlink():
            return False
        name_low = path.name.lower()
        if name_low in _FORBIDDEN_FILENAMES or name_low.startswith(".env"):
            return False
        if path.suffix.lower() not in _LOG_FILE_EXTENSIONS:
            return False
        for part in path.parts:
            if part.lower() in _FORBIDDEN_DIRS:
                return False
        return path.is_file()
    except OSError:
        return False


def _resolve_log_input(log_input: str, max_chars: int = 16000) -> str:
    """
    Accept either raw log text OR a verified log file/directory path string.
    Finding N1: Refuses symlinks, non-log file extensions (e.g. `~/.ssh/config`, `.env`),
    and bounds directory traversal depth and file count.
    """
    if not log_input or not log_input.strip():
        return ""
    stripped = log_input.strip()
    if "\n" not in stripped and len(stripped) < 1024:
        try:
            candidate = Path(stripped).expanduser()
            if not candidate.is_symlink() and candidate.exists():
                if candidate.is_file():
                    if _is_safe_log_file_path(candidate):
                        return _read_log_file(candidate, max_chars=max_chars)
                    return stripped
                if candidate.is_dir():
                    files: list[tuple[int, Path]] = []
                    inspected = 0
                    base_depth = len(candidate.resolve().parts)
                    for root, dirnames, filenames in os.walk(candidate, followlinks=False):
                        root_path = Path(root)
                        if len(root_path.parts) - base_depth > 3:
                            dirnames[:] = []
                            continue
                        dirnames[:] = [
                            d
                            for d in dirnames
                            if not d.startswith(".") and d.lower() not in _FORBIDDEN_DIRS
                        ]
                        for fname in filenames:
                            inspected += 1
                            if inspected > 64:
                                break
                            child = root_path / fname
                            if _is_safe_log_file_path(child):
                                try:
                                    files.append((child.stat().st_mtime_ns, child))
                                except OSError:
                                    continue
                        if inspected > 64:
                            break
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
                    chunks.reverse()
                    return "\n\n".join(chunks)
        except OSError:
            pass
    return stripped


def _jsonl_record_text(value: Any) -> str:
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
    """Read text from a verified log or JSONL file, keeping the tail if the file is very large."""
    try:
        reject_symlink(path)
        if not _is_safe_log_file_path(path):
            return ""
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
                extracted_lines.append(line)
            return "\n".join(extracted_lines)[-max_chars:]
        return raw[-max_chars:]
    except (OSError, ValueError):
        return ""


def detect_provider(*, allow_implicit: bool = True) -> dict[str, Any]:
    """
    Detect the AI provider configuration.
    Finding S7: When `allow_implicit=False` (or when `MESH_NO_IMPLICIT_CLOUD=1` is set),
    ambient `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GROQ_API_KEY` environment variables are
    ignored unless `MESH_AI_PROVIDER` (or `MESH_AI_API_KEY` + `MESH_AI_BASE_URL`) is explicitly set.
    """
    explicit = os.environ.get("MESH_AI_PROVIDER", "").lower().strip()
    api_key_override = os.environ.get("MESH_AI_API_KEY", "").strip()
    model_override = os.environ.get("MESH_AI_MODEL", "").strip()
    base_url_override = os.environ.get("MESH_AI_BASE_URL", "").strip()

    if api_key_override and base_url_override:
        return {
            "provider": "openai-compatible",
            "api_key": api_key_override,
            "model": model_override or "gpt-4o-mini",
            "base_url": base_url_override,
            "available": True,
            "explicit": True,
        }

    if explicit == "anthropic":
        key = api_key_override or os.environ.get("ANTHROPIC_API_KEY", "")
        return {
            "provider": "anthropic",
            "api_key": key,
            "model": model_override or "claude-haiku-4-5-20251001",
            "base_url": None,
            "available": bool(key),
            "explicit": True,
        }
    if explicit in ("openai",):
        key = api_key_override or os.environ.get("OPENAI_API_KEY", "")
        return {
            "provider": "openai",
            "api_key": key,
            "model": model_override or "gpt-4o-mini",
            "base_url": base_url_override or "https://api.openai.com/v1",
            "available": bool(key),
            "explicit": True,
        }
    if explicit == "groq":
        key = api_key_override or os.environ.get("GROQ_API_KEY", "")
        return {
            "provider": "openai-compatible",
            "api_key": key,
            "model": model_override or "llama3-8b-8192",
            "base_url": base_url_override or "https://api.groq.com/openai/v1",
            "available": bool(key),
            "explicit": True,
        }
    if explicit == "ollama":
        base = base_url_override or "http://localhost:11434/v1"
        return {
            "provider": "openai-compatible",
            "api_key": "ollama",
            "model": model_override or "llama3",
            "base_url": base,
            "available": _ollama_is_running(base),
            "explicit": True,
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
            "explicit": True,
        }
    if explicit == "local":
        return {
            "provider": "local",
            "api_key": None,
            "model": None,
            "base_url": None,
            "available": True,
            "explicit": True,
        }

    # Finding S7: Do not implicitly select cloud providers when disabled or when MESH_AUTO_APPROVE=1
    # is set without an explicit MESH_AI_PROVIDER (prevents silent background log exfiltration).
    if not allow_implicit or env_bool("MESH_NO_IMPLICIT_CLOUD", False) or env_bool("MESH_AUTO_APPROVE", False):
        return {
            "provider": "local",
            "api_key": None,
            "model": None,
            "base_url": None,
            "available": True,
            "explicit": False,
        }

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if anthropic_key and not api_key_override:
        return {
            "provider": "anthropic",
            "api_key": anthropic_key,
            "model": model_override or "claude-haiku-4-5-20251001",
            "base_url": None,
            "available": True,
            "explicit": False,
        }

    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if openai_key and not api_key_override:
        return {
            "provider": "openai-compatible",
            "api_key": openai_key,
            "model": model_override or "gpt-4o-mini",
            "base_url": "https://api.openai.com/v1",
            "available": True,
            "explicit": False,
        }

    groq_key = os.environ.get("GROQ_API_KEY", "")
    if groq_key and not api_key_override:
        return {
            "provider": "openai-compatible",
            "api_key": groq_key,
            "model": model_override or "llama3-8b-8192",
            "base_url": "https://api.groq.com/openai/v1",
            "available": True,
            "explicit": False,
        }

    ollama_base = base_url_override or "http://localhost:11434/v1"
    if _ollama_is_running(ollama_base):
        return {
            "provider": "openai-compatible",
            "api_key": "ollama",
            "model": model_override or _detect_ollama_model(ollama_base),
            "base_url": ollama_base,
            "available": True,
            "explicit": False,
        }

    if api_key_override:
        return {
            "provider": "openai-compatible",
            "api_key": api_key_override,
            "model": model_override or "gpt-4o-mini",
            "base_url": base_url_override or "https://api.openai.com/v1",
            "available": True,
            "explicit": False,
        }

    return {
        "provider": "local",
        "api_key": None,
        "model": None,
        "base_url": None,
        "available": True,
        "explicit": False,
    }


def _request_cloud_upload_consent(provider_name: str, assume_yes: bool = False) -> bool:
    """
    Finding S7 / 2g: Cloud log upload consent that `MESH_AUTO_APPROVE=1` alone CANNOT grant.
    Requires:
      1. `assume_yes=True` (explicit per-invocation parameter/flag), OR
      2. `MESH_ALLOW_CLOUD_CAPTURE=1` (dedicated cloud upload consent env var), OR
      3. Interactive TTY confirmation at the keyboard (with `MESH_AUTO_APPROVE` temporarily ignored).
    """
    if assume_yes or env_bool("MESH_ALLOW_CLOUD_CAPTURE", False):
        return True
    if env_bool("MESH_NON_INTERACTIVE", False) or not ui.interactive():
        return False
    prev_auto = os.environ.pop("MESH_AUTO_APPROVE", None)
    try:
        return ui.confirm(
            f"Allow Mesh to send redacted session log text to external cloud LLM provider '{provider_name}' for memory extraction?",
            default=False,
            assume_yes=False,
        )
    finally:
        if prev_auto is not None:
            os.environ["MESH_AUTO_APPROVE"] = prev_auto


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

    safe_log_text = redact_secrets_for_cloud(log_text)
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

    safe_log_text = redact_secrets_for_cloud(log_text)
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

_compiled_signals = [re.compile(p, re.IGNORECASE) for p in _SIGNAL_PATTERNS]
_compiled_noise = [re.compile(p, re.IGNORECASE) for p in _NOISE_PATTERNS]


def _is_noise(sentence: str) -> bool:
    return any(p.match(sentence) for p in _compiled_noise)


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
        if _is_noise(sentence) or is_poison_attempt(sentence):
            continue
        key = sentence.lower()[:60]
        if key in seen:
            continue
        if _has_signal(sentence):
            is_private = is_credential(sentence)
            if is_private:
                sentence = redact_secrets_local(sentence)
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
    quarantine: Optional[bool] = None,
) -> dict[str, Any]:
    """
    Extract and store memories from a session log (or verified log file/directory path).
    Finding S7: Requires explicit cloud consent (`assume_yes=True`, `MESH_ALLOW_CLOUD_CAPTURE=1`,
    or interactive TTY prompt) that `MESH_AUTO_APPROVE=1` alone cannot grant.
    Finding S8: Marks captured memories with `trust_tier="log_captured"` and supports quarantine.
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

    if config["provider"] != "local" and not config.get("available"):
        if verbose:
            ui.info(
                f"Configured provider '{config['provider']}' is unavailable — using local keyword extraction"
            )
        config = {"provider": "local", "available": True}

    # Finding S7: Require dedicated cloud consent (`MESH_AUTO_APPROVE` alone does not grant cloud upload)
    if config["provider"] != "local":
        provider_name = config["provider"]
        if not _request_cloud_upload_consent(provider_name, assume_yes=assume_yes):
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
                or is_credential(preview_content)
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

    should_quarantine = (
        env_bool("MESH_QUARANTINE_CAPTURE", False)
        if quarantine is None
        else bool(quarantine)
    )

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
        if is_poison_attempt(content):
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
            has_credential = is_credential(content)
            is_private = (
                default_local_only
                or bool(mem.get("private", False))
                or has_credential
            )
            # Finding S10: Use local redaction so non-secret emails are preserved in local DB
            if has_credential:
                content = redact_secrets_local(content)
            learn_res = mesh.learn(
                content=content,
                memory_type=mtype,
                tags=tags,
                local_only=is_private,
                confidence=0.8,
                trust_tier="log_captured",
                quarantined=should_quarantine,
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
        "quarantined": should_quarantine,
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
        "--quarantine",
        action="store_true",
        help="Store captured memories in quarantine until approved with `mesh approve`",
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
            ui.hint("For AI extraction set MESH_AI_PROVIDER and ANTHROPIC_API_KEY, OPENAI_API_KEY or GROQ_API_KEY.", stream=sys.stdout)
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
        log_text = sys.stdin.read(1_000_000)
    else:
        path = Path(args.file).expanduser()
        if not path.exists():
            ui.fail(f"File not found: {args.file}", "Check the path, or pipe the log in with --stdin.")
        if not path.is_dir() and not _is_safe_log_file_path(path):
            ui.fail(
                f"Refusing to read '{args.file}': only non-symlinked .log, .txt, .jsonl, and .md files are allowed."
            )
        log_text = _resolve_log_input(str(path))

    ns = validate_namespace(args.namespace or os.environ.get("MESH_NAMESPACE", "shared"))
    result = extract_memories_from_log(
        log_text=log_text,
        namespace=ns,
        dry_run=args.dry_run,
        verbose=args.verbose,
        force_local=args.local_only,
        assume_yes=args.yes,
        resolve_input=False,
        quarantine=True if args.quarantine else None,
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

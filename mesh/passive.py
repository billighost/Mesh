"""
mesh/passive.py — Passive memory capture from agent session logs.

Reads a conversation log (text or JSON lines) and extracts facts worth
storing in Mesh. Works with any AI provider, or falls back to local
keyword extraction if no API key is configured.

Provider auto-detection order:
  1. MESH_AI_PROVIDER env var (explicit override)
  2. ANTHROPIC_API_KEY found → use Anthropic
  3. OPENAI_API_KEY found → use OpenAI
  4. Ollama running on localhost:11434 → use Ollama (free, local)
  5. No provider found → use local keyword extractor (always free)

Configuration env vars:
  MESH_AI_PROVIDER    = anthropic | openai | ollama | groq | openai-compatible
  MESH_AI_API_KEY     = API key (overrides provider-specific keys)
  MESH_AI_MODEL       = override the default model for your provider
  MESH_AI_BASE_URL    = base URL for openai-compatible providers
                        (default for ollama: http://localhost:11434/v1)

Usage:
    mesh-capture --file session.log --namespace shared
    cat session.log | mesh-capture --stdin
    mesh-capture --file session.log --dry-run --verbose
"""

import os
import sys
import re
import json
import argparse
from pathlib import Path
from typing import Optional

from .memory import Mesh


# ---------------------------------------------------------------------------
# Extraction prompt (same for all AI providers)
# ---------------------------------------------------------------------------

EXTRACTION_PROMPT = """You are a memory extraction assistant for Mesh, a shared agent memory system.

Given a conversation or session log between a human and an AI agent, extract facts, decisions, processes, and discoveries that would be valuable to remember for future sessions.

Rules:
- Only extract concrete, specific, reusable facts
- Do NOT extract opinions, casual conversation, or things only relevant in the moment
- Do NOT extract anything that looks like a password, API key, or credential — mark those as private=true instead
- Each memory should be a single, self-contained sentence
- Prefer specifics over generalities: "the deploy script is at ./scripts/deploy.sh" not "there is a deploy script"

Return ONLY a JSON array of objects. No preamble, no explanation, no markdown fences. Example:
[
  {"content": "the staging server URL is staging.example.com", "type": "fact", "tags": ["infrastructure"], "private": false},
  {"content": "always run tests before merging to main", "type": "process", "tags": ["workflow"], "private": false},
  {"content": "API key is sk-...", "type": "fact", "tags": ["credentials"], "private": true}
]

Valid types: fact, process, decision, error, general
If nothing worth remembering was found, return an empty array: []

Session log to analyze:
"""


# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

def detect_provider() -> dict:
    """
    Auto-detect the best available AI provider.
    Returns a dict with: provider, api_key, model, base_url
    """
    explicit = os.environ.get("MESH_AI_PROVIDER", "").lower().strip()
    api_key_override = os.environ.get("MESH_AI_API_KEY", "").strip()
    model_override = os.environ.get("MESH_AI_MODEL", "").strip()
    base_url_override = os.environ.get("MESH_AI_BASE_URL", "").strip()

    # --- Explicit provider set ---
    if explicit == "anthropic":
        key = api_key_override or os.environ.get("ANTHROPIC_API_KEY", "")
        return {
            "provider": "anthropic",
            "api_key": key,
            "model": model_override or "claude-haiku-4-5-20251001",
            "base_url": None,
            "available": bool(key)
        }

    if explicit in ("openai",):
        key = api_key_override or os.environ.get("OPENAI_API_KEY", "")
        return {
            "provider": "openai",
            "api_key": key,
            "model": model_override or "gpt-4o-mini",
            "base_url": "https://api.openai.com/v1",
            "available": bool(key)
        }

    if explicit == "groq":
        key = api_key_override or os.environ.get("GROQ_API_KEY", "")
        return {
            "provider": "openai-compatible",
            "api_key": key,
            "model": model_override or "llama3-8b-8192",
            "base_url": "https://api.groq.com/openai/v1",
            "available": bool(key)
        }

    if explicit == "ollama":
        base = base_url_override or "http://localhost:11434/v1"
        return {
            "provider": "openai-compatible",
            "api_key": "ollama",  # Ollama accepts any non-empty string
            "model": model_override or "llama3",
            "base_url": base,
            "available": _ollama_is_running(base)
        }

    if explicit == "openai-compatible":
        key = api_key_override or ""
        base = base_url_override or ""
        return {
            "provider": "openai-compatible",
            "api_key": key,
            "model": model_override or "gpt-4o-mini",
            "base_url": base,
            "available": bool(key and base)
        }

    # --- Auto-detect: try in priority order ---

    # 1. Anthropic
    anthropic_key = api_key_override or os.environ.get("ANTHROPIC_API_KEY", "")
    if anthropic_key:
        return {
            "provider": "anthropic",
            "api_key": anthropic_key,
            "model": model_override or "claude-haiku-4-5-20251001",
            "base_url": None,
            "available": True
        }

    # 2. OpenAI
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if openai_key:
        return {
            "provider": "openai-compatible",
            "api_key": openai_key,
            "model": model_override or "gpt-4o-mini",
            "base_url": "https://api.openai.com/v1",
            "available": True
        }

    # 3. Groq
    groq_key = os.environ.get("GROQ_API_KEY", "")
    if groq_key:
        return {
            "provider": "openai-compatible",
            "api_key": groq_key,
            "model": model_override or "llama3-8b-8192",
            "base_url": "https://api.groq.com/openai/v1",
            "available": True
        }

    # 4. Ollama (local, free)
    ollama_base = base_url_override or "http://localhost:11434/v1"
    if _ollama_is_running(ollama_base):
        return {
            "provider": "openai-compatible",
            "api_key": "ollama",
            "model": model_override or _detect_ollama_model(ollama_base),
            "base_url": ollama_base,
            "available": True
        }

    # 5. Any other OpenAI-compatible key
    generic_key = api_key_override
    generic_base = base_url_override
    if generic_key and generic_base:
        return {
            "provider": "openai-compatible",
            "api_key": generic_key,
            "model": model_override or "gpt-4o-mini",
            "base_url": generic_base,
            "available": True
        }

    # 6. No AI provider found — use local fallback
    return {
        "provider": "local",
        "api_key": None,
        "model": None,
        "base_url": None,
        "available": True  # local always available
    }


def _ollama_is_running(base_url: str) -> bool:
    """Check if Ollama is running at the given base URL."""
    try:
        import httpx
        r = httpx.get(base_url.replace("/v1", ""), timeout=2)
        return r.status_code < 500
    except Exception:
        return False


def _detect_ollama_model(base_url: str) -> str:
    """Return the first available Ollama model, or 'llama3' as default."""
    try:
        import httpx
        r = httpx.get(base_url.replace("/v1", "/api/tags"), timeout=3)
        if r.status_code == 200:
            models = r.json().get("models", [])
            if models:
                return models[0]["name"]
    except Exception:
        pass
    return "llama3"


# ---------------------------------------------------------------------------
# AI extraction — Anthropic
# ---------------------------------------------------------------------------

def _extract_with_anthropic(log_text: str, config: dict) -> list:
    """Call Anthropic API to extract memories."""
    import httpx

    response = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": config["api_key"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        },
        json={
            "model": config["model"],
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": EXTRACTION_PROMPT + log_text}]
        },
        timeout=30
    )

    if response.status_code == 401:
        raise ValueError("Invalid Anthropic API key. Check ANTHROPIC_API_KEY.")
    if response.status_code == 429:
        raise ValueError("Anthropic rate limit hit. Try again in a moment.")
    if response.status_code != 200:
        raise ValueError(f"Anthropic API error {response.status_code}: {response.text[:200]}")

    raw = response.json()["content"][0]["text"].strip()
    return _parse_json_response(raw)


# ---------------------------------------------------------------------------
# AI extraction — OpenAI-compatible (OpenAI, Groq, Ollama, Together, etc.)
# ---------------------------------------------------------------------------

def _extract_with_openai_compatible(log_text: str, config: dict) -> list:
    """
    Call any OpenAI-compatible API to extract memories.
    Works with: OpenAI, Groq, Ollama, Together AI, Mistral, Perplexity, etc.
    """
    import httpx

    base_url = config["base_url"].rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config['api_key']}"
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
                    "content": "You are a memory extraction assistant. Always respond with valid JSON only."
                },
                {
                    "role": "user",
                    "content": EXTRACTION_PROMPT + log_text
                }
            ]
        },
        timeout=30
    )

    if response.status_code == 401:
        raise ValueError(f"Invalid API key for {base_url}. Check your MESH_AI_API_KEY.")
    if response.status_code == 429:
        raise ValueError("Rate limit hit. Try again in a moment.")
    if response.status_code != 200:
        raise ValueError(f"API error {response.status_code} from {base_url}: {response.text[:200]}")

    raw = response.json()["choices"][0]["message"]["content"].strip()
    return _parse_json_response(raw)


# ---------------------------------------------------------------------------
# Local fallback extractor — no AI required
# ---------------------------------------------------------------------------

# Sentence patterns that commonly signal a storable fact
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
    r"\bcredential",
    r"\bpassword\b",
    r"\bapi key\b",
    r"\btoken\b",
    r"\bsecret\b",
]

_CREDENTIAL_PATTERNS = [
    r"\bpassword\b",
    r"\bapi[_\s]?key\b",
    r"\bsecret\b",
    r"\btoken\b",
    r"\bcredential\b",
    r"\bprivate[_\s]key\b",
    r"sk-[a-zA-Z0-9]{10,}",
    r"Bearer\s+\S{10,}",
]

_NOISE_PATTERNS = [
    r"^\s*(yes|no|ok|okay|sure|thanks|thank you|great|got it|understood)\s*[.!]?\s*$",
    r"^\s*i\s+(see|understand|think|feel|wonder|believe)\b",
    r"^\s*(can|could|would|should)\s+you\b",
    r"^\s*please\b",
    r"^\s*here\s+(is|are)\b",
    r"^\s*let me\b",
    r"^\s*i'll\b",
]

_compiled_signals = [re.compile(p, re.IGNORECASE) for p in _SIGNAL_PATTERNS]
_compiled_credentials = [re.compile(p, re.IGNORECASE) for p in _CREDENTIAL_PATTERNS]
_compiled_noise = [re.compile(p, re.IGNORECASE) for p in _NOISE_PATTERNS]


def _is_credential(sentence: str) -> bool:
    return any(p.search(sentence) for p in _compiled_credentials)


def _is_noise(sentence: str) -> bool:
    return any(p.match(sentence) for p in _compiled_noise)


def _has_signal(sentence: str) -> bool:
    return any(p.search(sentence) for p in _compiled_signals)


def _infer_type(sentence: str) -> str:
    s = sentence.lower()
    if any(w in s for w in ["decided", "decision", "chose", "going with", "we'll use"]):
        return "decision"
    if any(w in s for w in ["always", "never", "must", "should", "don't", "do not", "process", "workflow"]):
        return "process"
    if any(w in s for w in ["error", "bug", "issue", "problem", "broken", "fails", "crash"]):
        return "error"
    return "fact"


def _extract_local(log_text: str) -> list:
    """
    Extract memories locally using regex/keyword heuristics.
    No AI, no API key, always free. Lower quality than AI extraction
    but catches obvious facts, decisions, and process notes.
    """
    # Split into sentences
    sentences = re.split(r'(?<=[.!?])\s+|\n', log_text)
    
    results = []
    seen = set()

    for raw in sentences:
        sentence = raw.strip()

        # Skip short, empty, or noisy sentences
        if len(sentence) < 20 or len(sentence) > 300:
            continue
        if _is_noise(sentence):
            continue

        # Deduplicate
        key = sentence.lower()[:60]
        if key in seen:
            continue

        if _has_signal(sentence):
            is_private = _is_credential(sentence)
            results.append({
                "content": sentence,
                "type": _infer_type(sentence),
                "tags": ["auto-captured", "local-extraction"],
                "private": is_private
            })
            seen.add(key)

        # Limit to 15 memories per session to avoid low-quality noise
        if len(results) >= 15:
            break

    return results


# ---------------------------------------------------------------------------
# JSON response parser (shared by all AI providers)
# ---------------------------------------------------------------------------

def _parse_json_response(raw: str) -> list:
    """Parse JSON array from AI response, stripping any markdown fences."""
    # Strip markdown fences
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
        # Try to find a JSON array anywhere in the response
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if match:
            raw = match.group(0)
        else:
            return []

    return json.loads(raw)


# ---------------------------------------------------------------------------
# Main extraction entry point
# ---------------------------------------------------------------------------

def extract_memories_from_log(
    log_text: str,
    namespace: str = "shared",
    agent_id: str = "passive-capture",
    dry_run: bool = False,
    verbose: bool = False,
    force_local: bool = False
) -> dict:
    """
    Extract and store memories from a session log.

    Auto-detects the best available AI provider. Falls back to local
    keyword extraction if no provider is configured.

    Args:
        log_text: The raw session log text to analyze.
        namespace: Mesh namespace to store memories in.
        agent_id: Agent ID to attribute stored memories to.
        dry_run: If True, print what would be stored without storing anything.
        verbose: If True, print detailed progress.
        force_local: If True, skip AI providers and use local extraction only.

    Returns:
        Dict with: stored_count, skipped_count, provider_used, memories list.
    """
    if not log_text.strip():
        return {"stored_count": 0, "skipped_count": 0, "provider_used": "none", "memories": []}

    # Truncate very long logs (keep last 8000 chars ≈ 2000 tokens)
    if len(log_text) > 8000:
        log_text = "[earlier content truncated]\n\n" + log_text[-8000:]

    # --- Detect provider ---
    if force_local:
        config = {"provider": "local", "available": True}
    else:
        config = detect_provider()

    if verbose:
        if config["provider"] == "local":
            print("  ℹ️  No AI provider found — using local keyword extraction.")
            print("     Set ANTHROPIC_API_KEY, OPENAI_API_KEY, GROQ_API_KEY, or start Ollama for better results.")
        else:
            provider_label = config["provider"]
            model_label = config.get("model", "")
            base_label = f" ({config['base_url']})" if config.get("base_url") else ""
            print(f"  ℹ️  Using provider: {provider_label} / {model_label}{base_label}")

    # --- Extract ---
    extracted = []
    extraction_error = None

    try:
        if config["provider"] == "anthropic":
            extracted = _extract_with_anthropic(log_text, config)
            provider_used = f"anthropic/{config['model']}"

        elif config["provider"] == "openai-compatible":
            extracted = _extract_with_openai_compatible(log_text, config)
            base = config.get("base_url", "")
            provider_used = f"openai-compatible/{config['model']} ({base})"

        else:
            # Local fallback
            extracted = _extract_local(log_text)
            provider_used = "local-keyword-extractor"

    except Exception as e:
        extraction_error = str(e)
        if verbose:
            print(f"  ⚠️  AI extraction failed: {e}", file=sys.stderr)
            print("  ↩️  Falling back to local keyword extraction.", file=sys.stderr)

        # Always fall back to local if AI fails
        try:
            extracted = _extract_local(log_text)
            provider_used = "local-keyword-extractor (fallback)"
        except Exception as fallback_err:
            if verbose:
                print(f"  ✗ Local fallback also failed: {fallback_err}", file=sys.stderr)
            return {
                "stored_count": 0,
                "skipped_count": 0,
                "provider_used": "none",
                "memories": [],
                "error": extraction_error
            }

    if not extracted:
        if verbose:
            print("  No memories worth extracting found in session log.")
        return {"stored_count": 0, "skipped_count": 0, "provider_used": provider_used, "memories": []}

    # --- Dry run ---
    if dry_run:
        print(f"\n[DRY RUN] Would store {len(extracted)} memories into namespace '{namespace}'")
        print(f"          Provider: {provider_used}\n")
        for mem in extracted:
            private = " 🔒 PRIVATE" if mem.get("private") else ""
            print(f"  [{mem.get('type','general')}]{private}  {mem['content']}")
        return {"stored_count": 0, "skipped_count": len(extracted), "provider_used": provider_used, "memories": extracted}

    # --- Store ---
    mesh = Mesh(namespace=namespace, agent_id=agent_id)
    stored = 0
    skipped = 0

    for mem in extracted:
        try:
            mesh.learn(
                content=mem["content"],
                memory_type=mem.get("type", "general"),
                tags=mem.get("tags", []) + ["auto-captured"],
                local_only=mem.get("private", False),
                confidence=0.8  # Slightly lower confidence for auto-extracted memories
            )
            stored += 1
            if verbose:
                private = " 🔒" if mem.get("private") else ""
                print(f"  ✓{private} {mem['content'][:80]}")
        except Exception as e:
            skipped += 1
            if verbose:
                print(f"  ✗ Failed: {mem['content'][:60]} — {e}", file=sys.stderr)

    return {
        "stored_count": stored,
        "skipped_count": skipped,
        "provider_used": provider_used,
        "memories": extracted
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def capture_cmd():
    """Entry point for: mesh-capture"""
    parser = argparse.ArgumentParser(
        description="Extract and store memories from a session log automatically.",
        epilog="""
Provider auto-detection (in priority order):
  1. MESH_AI_PROVIDER env var (anthropic | openai | groq | ollama | openai-compatible)
  2. ANTHROPIC_API_KEY found in environment
  3. OPENAI_API_KEY found in environment
  4. GROQ_API_KEY found in environment
  5. Ollama running on localhost:11434
  6. Local keyword extractor (no AI, always available)

Additional env vars:
  MESH_AI_API_KEY    Override the API key for any provider
  MESH_AI_MODEL      Override the model (e.g. gpt-4o, llama3:70b)
  MESH_AI_BASE_URL   Base URL for openai-compatible providers

Examples:
  mesh-capture --file session.log
  mesh-capture --file session.log --dry-run --verbose
  cat output.txt | mesh-capture --stdin
  mesh-capture --file session.log --local-only
  MESH_AI_PROVIDER=groq GROQ_API_KEY=... mesh-capture --file log.txt
  MESH_AI_PROVIDER=ollama MESH_AI_MODEL=mistral mesh-capture --file log.txt
        """
    )
    parser.add_argument("--file", help="Path to session log file")
    parser.add_argument("--stdin", action="store_true", help="Read log from stdin")
    parser.add_argument("--namespace", default=None,
                        help="Mesh namespace (default: MESH_NAMESPACE env var or 'shared')")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview what would be stored without writing anything")
    parser.add_argument("--verbose", action="store_true",
                        help="Show detailed progress and provider info")
    parser.add_argument("--local-only", action="store_true",
                        help="Force local keyword extraction, skip all AI providers")
    parser.add_argument("--show-provider", action="store_true",
                        help="Print which provider would be used and exit")
    args = parser.parse_args()

    # --show-provider: just print detection result and exit
    if args.show_provider:
        config = detect_provider()
        if config["provider"] == "local":
            print("Provider: local-keyword-extractor (no AI key found)")
            print("Set ANTHROPIC_API_KEY, OPENAI_API_KEY, GROQ_API_KEY, or start Ollama for AI extraction.")
        else:
            print(f"Provider:  {config['provider']}")
            print(f"Model:     {config.get('model','')}")
            if config.get("base_url"):
                print(f"Base URL:  {config['base_url']}")
        return

    if not args.file and not args.stdin:
        parser.print_help()
        sys.exit(1)

    if args.stdin:
        log_text = sys.stdin.read()
    else:
        path = Path(args.file)
        if not path.exists():
            print(f"File not found: {args.file}", file=sys.stderr)
            sys.exit(1)
        log_text = path.read_text(encoding="utf-8")

    ns = args.namespace or os.environ.get("MESH_NAMESPACE", "shared")

    result = extract_memories_from_log(
        log_text=log_text,
        namespace=ns,
        dry_run=args.dry_run,
        verbose=args.verbose,
        force_local=args.local_only
    )

    if not args.dry_run:
        provider_label = result.get("provider_used", "unknown")
        print(
            f"✓ Passive capture complete — "
            f"{result['stored_count']} stored, "
            f"{result['skipped_count']} skipped "
            f"(provider: {provider_label})"
        )

"""
mesh/cli.py — Enterprise Command-Line Interface for Mesh.

Features:
  - Zero-overhead lazy command routing (never imports FastAPI, Uvicorn, or MCP on CLI operations)
  - Interactive `y/n` user permission prompts before starting background processes, modifying OS startup,
    deleting namespaces, or installing shell RC completions
  - Authenticated, idempotent local daemon proxy across read and write commands
  - Full namespace management (`list`, `stats`, `rename`, `delete`) with confirmation
  - Multi-shell autocompletion (`bash`, `zsh`, `fish`, `powershell`) with idempotent `--install`
  - Comprehensive `mesh doctor` diagnostics (SQLite WAL/FTS5, permissions, ONNX SHA-256, daemon, MCP)
  - Non-recursive interactive REPL shell (`mesh shell` / `mesh i`) with namespace switching
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import shlex
import socket
import sqlite3
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from .audit import validate_namespace
from .version import VERSION, check_for_updates

# Ensure Windows cp1252 / legacy consoles never crash on Unicode output
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(errors="replace")
    except Exception:
        pass

# --- ANSI Truecolor UI Theme ---
_C_RESET = "\033[0m"
_C_BOLD = "\033[1m"
_C_DIM = "\033[2m"
_C_ACCENT = "\033[38;2;16;185;129m"   # #10b981 Emerald
_C_BLUE = "\033[38;2;59;130;246m"     # #3b82f6 Blue
_C_PURPLE = "\033[38;2;139;92;246m"   # #8b5cf6 Purple
_C_AMBER = "\033[38;2;245;158;11m"    # #f59e0b Amber
_C_RED = "\033[38;2;239;68;68m"       # #ef4444 Red
_C_MUTED = "\033[38;2;139;149;167m"   # #8b95a7 Muted


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR") or os.environ.get("MESH_NO_COLOR"):
        return False
    if os.name == "nt":
        return True
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _c(color_code: str, text: str) -> str:
    if not _supports_color():
        return text
    return f"{color_code}{text}{_C_RESET}"


def _sim_bar(similarity: float) -> str:
    pct = round(max(0.0, min(1.0, similarity)) * 100)
    filled = round(pct / 10)
    try:
        "█░".encode(sys.stdout.encoding or "utf-8")
        bar = "█" * filled + "░" * (10 - filled)
    except (UnicodeEncodeError, LookupError):
        bar = "#" * filled + "-" * (10 - filled)
    return f"{bar} {pct}%"


def _resolve_namespace(ns_arg: Optional[str]) -> str:
    raw = (ns_arg or os.environ.get("MESH_NAMESPACE", "shared")).strip() or "shared"
    try:
        return validate_namespace(raw)
    except ValueError as e:
        print(_c(_C_RED, f"  Error: {e}"), file=sys.stderr)
        sys.exit(1)


def _daemon_request(
    path: str,
    payload: Optional[dict[str, Any]] = None,
    method: str = "POST",
    timeout: float = 4.0,
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """
    Send an authenticated request to the running local daemon if reachable.
    - Never silently spawns a background daemon if the socket is closed.
    - Returns `{"_http_error": code, "detail": ...}` on 4xx/5xx or mid-request timeout
      so callers never re-run a mutating operation locally a second time.
    """
    if os.environ.get("MESH_NO_DAEMON_PROXY", "").strip().lower() in ("1", "true", "yes"):
        return None
    req_host = host or os.environ.get("MESH_HTTP_HOST", "127.0.0.1")
    req_port = int(port or os.environ.get("MESH_HTTP_PORT", 7701))

    try:
        with socket.create_connection((req_host, req_port), timeout=0.08):
            pass
    except OSError:
        return None

    from .daemon import get_auth_headers, invalidate_daemon_identity_cache, verify_daemon_identity

    # Rule: the bearer token is never sent until verify_daemon_identity succeeds.
    if not verify_daemon_identity(host=req_host, port=req_port, timeout=0.5):
        return None

    url = f"http://{req_host}:{req_port}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = get_auth_headers()
    if data is not None:
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode("utf-8"))
            detail = err_body.get("detail") if isinstance(err_body, dict) else str(e)
        except Exception:
            detail = str(e)
        return {"_http_error": e.code, "detail": detail}
    except (TimeoutError, socket.timeout) as e:
        invalidate_daemon_identity_cache(host, port)
        return {
            "_http_error": 504,
            "detail": f"Daemon timed out after {timeout}s while processing {path}: {e}",
        }
    except (urllib.error.URLError, OSError, ValueError):
        invalidate_daemon_identity_cache(host, port)
        return None
    return None


# --- Smart Command & Flag Typo Recovery ---
_COMMAND_ALIASES = {
    "learn": "add",
    "save": "add",
    "remember": "add",
    "search": "ask",
    "recall": "ask",
    "find": "ask",
    "edit": "update",
    "modify": "update",
    "rm": "forget",
    "del": "forget",
    "delete": "forget",
    "ls": "inspect",
    "list": "inspect",
    "show": "inspect",
    "conflicts": "conflicts",
    "conflict": "conflicts",
    "repl": "shell",
    "i": "shell",
}

def _suggest_did_you_mean(unknown: str, choices: list[str]) -> list[str]:
    return difflib.get_close_matches(unknown.lower(), choices, n=2, cutoff=0.5)


# --- Commands ---

def setup_cmd() -> None:
    parser = argparse.ArgumentParser(description="Launch background worker and warm ONNX model.")
    parser.add_argument("--namespace", default=None)
    parser.add_argument("--no-startup", action="store_true")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip interactive y/n permission prompts")
    args = parser.parse_args()

    from .daemon import (
        DAEMON_LOG_FILE,
        DEFAULT_HOST,
        DEFAULT_IDLE_TIMEOUT,
        DEFAULT_PORT,
        install_startup,
        request_user_permission,
        start_daemon,
    )
    from .memory import _ONNX_MODEL_DIR, _has_local_onnx_pair, ensure_model_downloaded

    ns = _resolve_namespace(args.namespace)
    print(_c(_C_ACCENT, f"    Initializing Mesh v{VERSION} (namespace: '{ns}')..."))

    if not args.no_startup:
        if request_user_permission(
            "register the Mesh daemon for OS login auto-start",
            assume_yes=args.yes,
        ):
            info = install_startup(
                host=DEFAULT_HOST,
                port=DEFAULT_PORT,
                idle_timeout=DEFAULT_IDLE_TIMEOUT,
                start_now=False,
            )
            print(f"    [OK] Registered OS login auto-start ({info['platform']})")
        else:
            print(_c(_C_DIM, "    [..] Skipped OS login auto-start registration."))

    if request_user_permission(
        f"start the background Mesh HTTP daemon on {DEFAULT_HOST}:{DEFAULT_PORT}",
        assume_yes=args.yes,
    ):
        res = start_daemon(
            host=DEFAULT_HOST,
            port=DEFAULT_PORT,
            idle_timeout=DEFAULT_IDLE_TIMEOUT,
            lazy=True,
            wait=args.wait,
        )
        if res.get("running"):
            print(f"    [OK] Background daemon active on http://{DEFAULT_HOST}:{DEFAULT_PORT} (PID: {res.get('pid')})")
        else:
            print(
                _c(
                    _C_RED,
                    f"    [ERROR] Background daemon failed to start on http://{DEFAULT_HOST}:{DEFAULT_PORT}. "
                    f"Check {DAEMON_LOG_FILE}.",
                ),
                file=sys.stderr,
            )
    else:
        print(_c(_C_DIM, "    [..] Skipped starting background daemon."))

    pair = _has_local_onnx_pair()
    if pair is not None:
        print(_c(_C_ACCENT, f"    [OK] Local ONNX model cached: {pair[0].parent}"))
    elif request_user_permission(
        f"download the ONNX embedding model into {_ONNX_MODEL_DIR}",
        assume_yes=args.yes,
    ):
        if args.wait:
            pair = ensure_model_downloaded(blocking=True, timeout=120.0, assume_yes=True)
            if pair is not None:
                print(_c(_C_ACCENT, f"    [OK] ONNX model downloaded: {pair[0].parent}"))
                _daemon_request("/warmup", payload={"namespace": ns}, method="POST")
            else:
                print(_c(_C_AMBER, "    [..] ONNX model download still in progress or unavailable (using local hash fallback)."))
        else:
            ensure_model_downloaded(blocking=False, assume_yes=True)
            print(_c(_C_DIM, f"    [..] Downloading ONNX weights into {_ONNX_MODEL_DIR} in background..."))

    # Install the Mesh agent skill into all detected AI coding agent folders
    print()
    _run_install_skills(assume_yes=args.yes)



def _run_install_skills(assume_yes: bool = False) -> None:
    """Shared helper: install skill into all agent folders, print results."""
    from .skill_installer import install_all

    print(_c(_C_ACCENT, "    Installing Mesh agent skill..."))
    results = install_all(
        assume_yes=assume_yes,
        permission_fn=None,  # uses daemon.request_user_permission when available
    )
    installed = 0
    skipped = 0
    errors = 0
    for r in results:
        agent = r["agent"]
        status = r["status"]
        if "ERROR" in status:
            errors += 1
            print(_c(_C_RED, f"    [ERROR] {agent}: {status}"))
        elif r["changed"]:
            installed += 1
            print(_c(_C_ACCENT, f"    [OK]    {agent}: {status}"))
        else:
            skipped += 1
            print(_c(_C_DIM, f"    [..]    {agent}: {status}"))
    summary_parts = []
    if installed:
        summary_parts.append(f"{installed} installed")
    if skipped:
        summary_parts.append(f"{skipped} skipped")
    if errors:
        summary_parts.append(f"{errors} errors")
    summary = ", ".join(summary_parts) or "nothing to do"
    color = _C_RED if errors else (_C_ACCENT if installed else _C_DIM)
    print(_c(color, f"    Skill install complete: {summary}."))


def install_skills_cmd() -> None:
    """Install the Mesh agent skill into all detected AI coding agent skill folders."""
    parser = argparse.ArgumentParser(
        description="Install the Mesh agent skill into all detected AI coding agent skill folders."
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip interactive y/n permission prompts",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List detected agent skill directories and current install status, then exit",
    )
    args = parser.parse_args()

    if args.list:
        from .skill_installer import _candidate_skill_roots, _SKILL_NAME
        print(_c(_C_ACCENT, "\n    Detected agent skill directories:\n"))
        candidates = _candidate_skill_roots()
        if not candidates:
            print(_c(_C_DIM, "    None detected."))
        for label, skills_root in candidates:
            dst = skills_root / _SKILL_NAME
            installed_mark = _c(_C_ACCENT, "[installed]") if dst.is_dir() else _c(_C_DIM, "[not installed]")
            print(f"    {installed_mark}  {label}")
            print(_c(_C_DIM, f"              {skills_root}"))
        print()
        return

    _run_install_skills(assume_yes=args.yes)


def add_cmd() -> None:
    from .memory import VALID_MEMORY_TYPES

    parser = argparse.ArgumentParser(description="Store a memory directly into Mesh.")
    parser.add_argument("content", nargs="?", default=None, help="The memory content to store")
    parser.add_argument("--type", dest="memory_type", default="fact", choices=sorted(VALID_MEMORY_TYPES))
    parser.add_argument("--tags", default="")
    parser.add_argument("--private", action="store_true")
    parser.add_argument("--force-public", action="store_true", default=False, help="Force memory to be public even if credentials detected")
    parser.add_argument("--namespace", default=None)
    parser.add_argument("--confidence", type=float, default=1.0)
    parser.add_argument("--ttl", type=float, default=None)
    args = parser.parse_args()

    content = args.content
    if not content:
        try:
            content = input(_c(_C_BLUE, "  Enter memory content: ")).strip()
        except (KeyboardInterrupt, EOFError):
            print()
            return
        if not content:
            print(_c(_C_RED, "    Error: content cannot be empty."))
            sys.exit(1)

    from .guards import guard_ingest

    guard = guard_ingest(content, local_only=args.private)
    if guard.rejected:
        print(_c(_C_RED, f"    Error: {guard.reason or 'Rejected by ingestion guard.'}"), file=sys.stderr)
        sys.exit(1)

    final_content = guard.content
    final_private = guard.local_only
    if guard.local_only and not args.private:
        if args.force_public and sys.stdin.isatty():
            final_private = False
            print(_c(_C_YELLOW, "    [WARNING] Credential pattern detected, but --force-public specified on terminal: saving as public memory."), file=sys.stderr)
        else:
            final_private = True
            print(_c(_C_YELLOW, "    [WARNING] Credential pattern detected: content was redacted and saved as private (local_only). Use --force-public on a terminal to override."), file=sys.stderr)

    ns = _resolve_namespace(args.namespace)
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []

    proxied = _daemon_request(
        "/learn",
        payload={
            "content": final_content,
            "memory_type": args.memory_type,
            "confidence": args.confidence,
            "tags": tags,
            "local_only": final_private,
            "ttl_days": args.ttl,
            "namespace": ns,
            "agent_id": "human-cli",
        },
    )
    if proxied is not None:
        if "_http_error" in proxied:
            print(_c(_C_RED, f"    Error: {proxied.get('detail')}"), file=sys.stderr)
            sys.exit(1)
        if proxied.get("status") == "ok":
            result = proxied["result"]
        else:
            print(_c(_C_RED, f"    Error: unexpected daemon response: {proxied}"), file=sys.stderr)
            sys.exit(1)
    else:
        from .memory import Mesh

        mesh = Mesh(namespace=ns, agent_id="human-cli")
        try:
            result = mesh.learn(
                content=final_content,
                memory_type=args.memory_type,
                confidence=args.confidence,
                tags=tags,
                local_only=final_private,
                ttl_days=args.ttl,
            )
        except ValueError as e:
            print(_c(_C_RED, f"    Error: {e}"), file=sys.stderr)
            sys.exit(1)

    priv_str = _c(_C_RED, " [PRIVATE]") if args.private else ""
    dedup_str = _c(_C_AMBER, " (reinforced existing memory)") if result.get("deduplicated") else ""
    print(_c(_C_ACCENT, f"\n    Stored [{args.memory_type}] in namespace '{ns}'{priv_str}{dedup_str}"))
    if result.get("memory_id"):
        print(_c(_C_DIM, f"    ID: {result['memory_id']}"))
    if result.get("conflict"):
        existing = result["conflict"].get("existing_content", "")
        print(_c(_C_AMBER, f'\n    Warning: Contradicts existing memory:\n     "{existing}"'))


def ask_cmd() -> None:
    from .memory import VALID_MEMORY_TYPES

    parser = argparse.ArgumentParser(description="Search Mesh using 5-stage hybrid search.")
    parser.add_argument("query", nargs="?", default=None, help="What to search for")
    parser.add_argument("--count", "-n", "--n", dest="count", type=int, default=5)
    parser.add_argument("--namespace", default=None)
    parser.add_argument("--type", dest="memory_type", choices=sorted(VALID_MEMORY_TYPES), default=None)
    parser.add_argument("--tags", default="")
    parser.add_argument("--min-confidence", type=float, default=0.0)
    parser.add_argument("--min-similarity", type=float, default=0.0)
    parser.add_argument("--no-stale", action="store_true")
    parser.add_argument("--no-other", action="store_true")
    parser.add_argument("--no-private", action="store_true", help="Exclude local_only private memories")
    args = parser.parse_args()

    query = args.query
    if not query:
        try:
            query = input(_c(_C_BLUE, "  Search query: ")).strip()
        except (KeyboardInterrupt, EOFError):
            print()
            return
        if not query:
            print(_c(_C_RED, "    Error: query cannot be empty."))
            sys.exit(1)

    ns = _resolve_namespace(args.namespace)
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None

    results = []
    other_possible = []
    filter_hint = None

    proxied = _daemon_request(
        "/recall",
        payload={
            "query": query,
            "n": args.count,
            "min_similarity": args.min_similarity,
            "min_confidence": args.min_confidence,
            "include_stale": not args.no_stale,
            "include_private": not args.no_private,
            "include_other_possible": not args.no_other,
            "memory_type": args.memory_type,
            "tags": tags,
            "namespace": ns,
            "agent_id": "human-cli",
        },
    )
    if proxied is not None and "_http_error" in proxied:
        print(_c(_C_RED, f"    Error: {proxied.get('detail')}"), file=sys.stderr)
        sys.exit(1)

    if proxied and proxied.get("status") == "ok":
        results = proxied.get("results", [])
        other_possible = proxied.get("other_possible_memories", []) or []
        filter_hint = proxied.get("filter_hint")
    else:
        from .memory import Mesh

        mesh = Mesh(namespace=ns, agent_id="human-cli")
        if not args.no_other and hasattr(mesh, "recall_with_context"):
            bundle = mesh.recall_with_context(
                query=query,
                n=args.count,
                min_similarity=args.min_similarity,
                min_confidence=args.min_confidence,
                include_stale=not args.no_stale,
                include_private=not args.no_private,
                memory_type=args.memory_type,
                tags=tags,
            )
            results = bundle["results"]
            other_possible = bundle.get("other_possible_memories", [])
            filter_hint = bundle.get("filter_hint")
        else:
            results = mesh.recall(
                query=query,
                n=args.count,
                min_similarity=args.min_similarity,
                min_confidence=args.min_confidence,
                include_stale=not args.no_stale,
                include_private=not args.no_private,
                memory_type=args.memory_type,
                tags=tags,
            )

    if not results:
        print(_c(_C_AMBER, f"\n    No direct memories found for: '{query}'"))
        if filter_hint:
            print(_c(_C_DIM, f"    Hint: {filter_hint}"))
    else:
        print(_c(_C_ACCENT, f"\n    Found {len(results)} matches in namespace '{ns}':\n"))
        for i, mem in enumerate(results, 1):
            sim = float(mem.get("similarity", 0.0))
            conf = float(mem.get("effective_confidence", mem.get("confidence", 1.0)))
            stale = _c(_C_AMBER, " [STALE]") if mem.get("is_stale") else ""
            priv = _c(_C_RED, " [PRIVATE]") if mem.get("local_only") else ""
            tags_list = mem.get("tags") or []
            tags_str = f" {_c(_C_DIM, '#' + ' #'.join(tags_list))}" if tags_list else ""
            reason = mem.get("match_reason", "hybrid")
            short_id = str(mem.get("id", ""))[:8]
            print(f"  {i}. {mem['content']}{stale}{priv}")
            print(
                f"     {_sim_bar(sim)}  {_c(_C_BLUE, reason)}     conf {conf:.2f}     "
                f"id {_c(_C_BOLD, short_id)}     by {mem.get('source_agent', 'agent')}{tags_str}"
            )
            print()

    if not args.no_other and other_possible:
        print(_c(_C_DIM, f"  Other possible memories in '{ns}' (peripheral snippets):"))
        for item in other_possible[:4]:
            snip = item.get("snippet") or item.get("content", "")[:100]
            mtype = item.get("memory_type", "fact")
            short_id = str(item.get("id", ""))[:8]
            reason = item.get("reason", "context")
            print(f"      [{mtype.upper()}] {snip}")
            print(f"      id {_c(_C_BOLD, short_id)}     {reason}")
        print()


def inspect_cmd() -> None:
    from .memory import VALID_MEMORY_TYPES

    parser = argparse.ArgumentParser(description="Browse memories in namespace.")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--type", dest="memory_type", choices=sorted(VALID_MEMORY_TYPES), default=None)
    parser.add_argument("--tag", default=None)
    parser.add_argument("--no-private", action="store_true", help="Exclude local_only private memories")
    parser.add_argument("--namespace", default=None)
    args = parser.parse_args()

    ns = _resolve_namespace(args.namespace)
    query_params: dict[str, Any] = {
        "namespace": ns,
        "limit": args.limit,
        "include_private": "false" if args.no_private else "true",
    }
    if args.memory_type:
        query_params["memory_type"] = args.memory_type
    if args.tag:
        query_params["tag"] = args.tag

    proxied = _daemon_request(
        f"/inspect?{urllib.parse.urlencode(query_params)}",
        payload=None,
        method="GET",
    )
    if proxied is not None and "_http_error" not in proxied and proxied.get("status") == "ok":
        mems = proxied.get("memories", [])
    else:
        from .memory import Mesh

        mesh = Mesh(namespace=ns, agent_id="cli")
        mems = mesh.inspect(
            limit=args.limit,
            memory_type=args.memory_type,
            tag=args.tag,
            include_private=not args.no_private,
        )

    if not mems:
        print(_c(_C_AMBER, f"  No memories found in namespace '{ns}'."))
        return

    print(_c(_C_ACCENT, f"\n    Memories in '{ns}' ({len(mems)} shown):\n"))
    for i, m in enumerate(mems, 1):
        short_id = str(m.get("id", ""))[:8]
        mtype = m.get("memory_type", "fact")
        conf = float(m.get("effective_confidence", m.get("confidence", 1.0)))
        tags = m.get("tags_list") or []
        tags_str = f" #{' #'.join(tags)}" if tags else ""
        priv_str = _c(_C_RED, " [PRIVATE]") if m.get("local_only") else ""
        print(f"  {i}. [{mtype.upper()}] {m['content']}{priv_str}")
        print(f"     id {short_id}     conf {conf:.2f}     {m.get('access_count', 0)}x read{tags_str}")
        print()


def update_cmd() -> None:
    parser = argparse.ArgumentParser(description="Update memory in-place by ID or 8-char short ID.")
    parser.add_argument("memory_id", help="Full UUID or 4+ char short ID")
    parser.add_argument("--content", default=None)
    parser.add_argument("--type", dest="memory_type", default=None)
    parser.add_argument("--confidence", type=float, default=None)
    parser.add_argument("--tags", default=None)
    parser.add_argument("--ttl", type=float, default=None)
    parser.add_argument("--clear-ttl", action="store_true", help="Remove TTL and make memory permanent")
    parser.add_argument("--private", dest="local_only", action="store_true", default=None)
    parser.add_argument("--public", dest="local_only", action="store_false", default=None)
    parser.add_argument("--namespace", default=None)
    args = parser.parse_args()

    ns = _resolve_namespace(args.namespace)
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags is not None else None
    should_clear_ttl = bool(args.clear_ttl) or (args.ttl is not None and args.ttl <= 0)

    proxied = _daemon_request(
        "/update",
        payload={
            "memory_id": args.memory_id,
            "content": args.content,
            "memory_type": args.memory_type,
            "confidence": args.confidence,
            "tags": tags,
            "ttl_days": None if should_clear_ttl else args.ttl,
            "clear_ttl": should_clear_ttl,
            "local_only": args.local_only,
            "namespace": ns,
            "agent_id": "human-cli",
        },
    )
    if proxied is not None:
        if "_http_error" in proxied:
            print(_c(_C_RED, f"    Error: {proxied.get('detail')}"), file=sys.stderr)
            sys.exit(1)
        if proxied.get("status") == "ok":
            print(_c(_C_ACCENT, f"    Updated memory '{args.memory_id}' in namespace '{ns}'"))
            return

    from .memory import Mesh

    mesh = Mesh(namespace=ns, agent_id="human-cli")
    try:
        res = mesh.update(
            memory_id=args.memory_id,
            content=args.content,
            memory_type=args.memory_type,
            confidence=args.confidence,
            tags=tags,
            ttl_days=None if should_clear_ttl else args.ttl,
            clear_ttl=should_clear_ttl,
            local_only=args.local_only,
        )
        print(_c(_C_ACCENT, f"    Updated memory '{res['memory_id'][:8]}' in namespace '{ns}'"))
    except ValueError as e:
        print(_c(_C_RED, f"    Error: {e}"), file=sys.stderr)
        sys.exit(1)


def forget_cmd() -> None:
    parser = argparse.ArgumentParser(description="Delete memory by ID or 8-char short ID.")
    parser.add_argument("memory_id", help="Full UUID or 4+ char short ID")
    parser.add_argument("--namespace", default=None)
    args = parser.parse_args()

    ns = _resolve_namespace(args.namespace)
    proxied = _daemon_request(
        "/forget",
        payload={"memory_id": args.memory_id, "namespace": ns, "agent_id": "human-cli"},
    )
    if proxied is not None:
        if "_http_error" in proxied:
            print(_c(_C_RED, f"    Error: {proxied.get('detail')}"), file=sys.stderr)
            sys.exit(1)
        if proxied.get("status") == "ok":
            print(_c(_C_ACCENT, f"    Deleted memory '{args.memory_id}' from namespace '{ns}'"))
            return
        if proxied.get("status") == "not_found":
            print(_c(_C_RED, f"    Memory '{args.memory_id}' not found in namespace '{ns}'"), file=sys.stderr)
            sys.exit(1)

    from .memory import Mesh

    mesh = Mesh(namespace=ns, agent_id="human-cli")
    try:
        deleted = mesh.forget(args.memory_id)
    except ValueError as e:
        print(_c(_C_RED, f"    Error: {e}"), file=sys.stderr)
        sys.exit(1)
    if deleted:
        print(_c(_C_ACCENT, f"    Deleted memory '{args.memory_id}' from namespace '{ns}'"))
    else:
        print(_c(_C_RED, f"    Memory '{args.memory_id}' not found in namespace '{ns}'"), file=sys.stderr)
        sys.exit(1)


def conflicts_cmd() -> None:
    parser = argparse.ArgumentParser(description="Inspect unresolved memory contradictions.")
    parser.add_argument("--resolve", default=None, help="Resolve contradiction by ID or short ID")
    parser.add_argument("--namespace", default=None)
    args = parser.parse_args()

    ns = _resolve_namespace(args.namespace)

    if args.resolve:
        proxied = _daemon_request(
            "/contradictions/resolve",
            payload={"contradiction_id": args.resolve, "namespace": ns},
        )
        if proxied is not None:
            if "_http_error" in proxied:
                print(_c(_C_RED, f"    Error: {proxied.get('detail')}"), file=sys.stderr)
                sys.exit(1)
            if proxied.get("status") == "ok":
                print(_c(_C_ACCENT, f"    Resolved contradiction '{args.resolve}'"))
                return

        from .memory import Mesh

        mesh = Mesh(namespace=ns, agent_id="cli")
        try:
            mesh.resolve_contradiction(args.resolve)
            print(_c(_C_ACCENT, f"    Resolved contradiction '{args.resolve}'"))
        except ValueError as e:
            print(_c(_C_RED, f"    Error: {e}"), file=sys.stderr)
            sys.exit(1)
        return

    proxied_list = _daemon_request(
        f"/contradictions?{urllib.parse.urlencode({'namespace': ns, 'resolved': 'false'})}",
        payload=None,
        method="GET",
    )
    if proxied_list is not None and "_http_error" not in proxied_list and proxied_list.get("status") == "ok":
        conflicts = proxied_list.get("contradictions", [])
    else:
        from .memory import Mesh

        mesh = Mesh(namespace=ns, agent_id="cli")
        conflicts = mesh.contradictions(resolved=False)

    if not conflicts:
        print(_c(_C_ACCENT, f"    No unresolved conflicts in namespace '{ns}'."))
        return

    print(_c(_C_AMBER, f"\n    {len(conflicts)} Unresolved Conflict(s) in '{ns}':\n"))
    for c in conflicts:
        cid = str(c.get("id", ""))[:8]
        print(f"    ID: {cid}  (similarity: {float(c.get('similarity', 0)):.2f})")
        print(f"    EXISTING: {c.get('existing_content')}")
        print(f"    NEW:      {c.get('new_content')}")
        print()


def patterns_cmd() -> None:
    parser = argparse.ArgumentParser(description="Surface query patterns, memory gaps & stale items.")
    parser.add_argument("--namespace", default=None)
    parser.add_argument(
        "--include-private",
        action="store_true",
        default=False,
        help="Include private (local_only) memories and queries in pattern analysis",
    )
    args = parser.parse_args()

    ns = _resolve_namespace(args.namespace)
    query_params = {"namespace": ns}
    if args.include_private:
        query_params["include_private"] = "1"

    proxied = _daemon_request(
        f"/patterns?{urllib.parse.urlencode(query_params)}",
        payload=None,
        method="GET",
    )
    if proxied is not None and "_http_error" not in proxied and proxied.get("status") == "ok":
        p = proxied.get("patterns", {})
    else:
        from .memory import Mesh

        mesh = Mesh(namespace=ns, agent_id="cli")
        p = mesh.patterns(include_private=args.include_private)

    print(_c(_C_ACCENT, f"\n    Memory Patterns & Gaps for '{ns}':\n"))
    print("  Top Topics:")
    for t in p.get("top_topics", [])[:5]:
        print(f"      {t['query_text']} ({t['query_count']} queries)")
    print("\n  Memory Gaps (Searched often with low similarity):")
    for g in p.get("memory_gaps", [])[:5]:
        print(f"      {g['query_text']} (asked {g['query_count']}x, avg sim {g['avg_similarity']:.2f})")
    print()


def doctor_cmd() -> None:
    """Comprehensive diagnostic check for SQLite WAL/FTS5, permissions, ONNX SHA-256, daemon, and MCP."""
    print(_c(_C_ACCENT, "\n    Mesh Diagnostic Doctor v" + VERSION + "\n"))
    from .audit import MESH_DIR, ensure_secure_mesh_dir
    from .daemon import get_daemon_health
    from .memory import (
        _ALLOWED_MODEL_SHA256,
        _ALLOWED_TOKENIZER_SHA256,
        _has_local_onnx_pair,
        _verify_sha256,
        get_engine_status,
    )
    from .namespaces import list_namespaces

    warnings = 0
    failures = 0

    # 1. Directory & Permissions check
    try:
        from .audit import ensure_secure_mesh_dir, get_mesh_dir

        mesh_dir = ensure_secure_mesh_dir()
        if os.name != "nt" and mesh_dir.exists():
            mode = stat.S_IMODE(mesh_dir.stat().st_mode)
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                warnings += 1
                print(f"    Data Directory: {_c(_C_AMBER, f'{mesh_dir} (WARN: mode {oct(mode)}, expected 0o700)')}")
            else:
                print(f"    Data Directory: {_c(_C_ACCENT, f'{mesh_dir} (mode 0700 OK)')}")
        else:
            print(f"    Data Directory: {_c(_C_ACCENT, f'{mesh_dir} (OK)')}")
    except Exception as e:
        failures += 1
        print(f"    Data Directory: {_c(_C_RED, f'FAIL ({e})')}")

    # 2. SQLite WAL & FTS5 check
    try:
        with sqlite3.connect(":memory:") as conn:
            conn.execute("CREATE VIRTUAL TABLE _doc_fts USING fts5(content, tokenize='porter unicode61')")
            conn.execute("INSERT INTO _doc_fts(content) VALUES ('mesh doctor fts5 check')")
            row = conn.execute("SELECT COUNT(*) FROM _doc_fts WHERE _doc_fts MATCH 'mesh'").fetchone()
            assert row and int(row[0]) == 1
        print(f"    SQLite & FTS5:  {_c(_C_ACCENT, f'OK (SQLite {sqlite3.sqlite_version} with FTS5 Porter)')}")
    except Exception as e:
        failures += 1
        print(f"    SQLite & FTS5:  {_c(_C_RED, f'FAIL ({e})')}")

    # 3. Daemon Server check
    health = get_daemon_health(timeout=0.4)
    if health:
        print(f"    Daemon Server:  {_c(_C_ACCENT, 'RUNNING (PID ' + str(health.get('pid')) + ')')}")
    else:
        print(f"    Daemon Server:  {_c(_C_DIM, 'STOPPED (optional — run `mesh daemon start` to enable)')}")

    # 4. ONNX Model & SHA-256 verification
    pair = _has_local_onnx_pair()
    eng = get_engine_status()
    if pair is not None:
        model_ok = _verify_sha256(pair[0], _ALLOWED_MODEL_SHA256, "MESH_MODEL_SHA256")
        tok_ok = _verify_sha256(pair[1], _ALLOWED_TOKENIZER_SHA256, "MESH_TOKENIZER_SHA256")
        if model_ok and tok_ok:
            print(f"    ONNX on disk:   {_c(_C_ACCENT, 'YES (SHA-256 verified)')}")
        else:
            failures += 1
            print(f"    ONNX on disk:   {_c(_C_RED, 'CHECKSUM MISMATCH (corrupted model/tokenizer files)')}")
    else:
        warnings += 1
        print(f"    ONNX on disk:   {_c(_C_AMBER, 'Not downloaded yet (using local hash-384 fallback)')}")

    print(f"    Model State:    {eng.get('backend', 'unloaded')}")

    # 5. MCP Config discovery
    home = Path.home()
    if os.name == "nt":
        claude_mcp = Path(os.environ.get("APPDATA", "")) / "Claude" / "claude_desktop_config.json"
        windsurf_mcp = home / ".codeium" / "windsurf" / "mcp_config.json"
    elif sys.platform == "darwin":
        claude_mcp = home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        windsurf_mcp = home / ".codeium" / "windsurf" / "mcp_config.json"
    else:
        config_home = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
        claude_mcp = config_home / "Claude" / "claude_desktop_config.json"
        windsurf_mcp = home / ".codeium" / "windsurf" / "mcp_config.json"

    mcp_configs = [
        Path.cwd() / ".cursor" / "mcp.json",
        home / ".cursor" / "mcp.json",
        claude_mcp,
        windsurf_mcp,
    ]
    found_mcp = [str(p) for p in mcp_configs if p.exists()]
    if found_mcp:
        print(f"    MCP Configs:    {_c(_C_ACCENT, f'{len(found_mcp)} detected')} ({found_mcp[0]})")
    else:
        print(f"    MCP Configs:    {_c(_C_DIM, 'None detected in default Claude/Cursor paths')}")

    # 6. Namespaces summary
    ns_list = list_namespaces()
    ns_preview = ", ".join(n["namespace"] for n in ns_list[:3]) if ns_list else "none"
    print(f"    Namespaces:     {len(ns_list)} found ({ns_preview})")

    if failures > 0:
        print(_c(_C_RED, f"\n    Doctor finished with {failures} failure(s) and {warnings} warning(s).\n"))
        sys.exit(1)
    elif warnings > 0:
        print(_c(_C_AMBER, f"\n    Core checks passed with {warnings} warning(s).\n"))
    else:
        print(_c(_C_ACCENT, "\n    All diagnostic checks passed!\n"))


def shell_cmd() -> None:
    """Interactive REPL shell with shlex argument parsing and active namespace tracking."""
    parser = argparse.ArgumentParser(description="Interactive Mesh REPL shell.")
    parser.add_argument("--namespace", default=None)
    args, _ = parser.parse_known_args()

    ns = _resolve_namespace(args.namespace)
    print(_c(_C_ACCENT, f"\n    Mesh Interactive Shell (namespace: '{ns}')"))
    print(_c(_C_DIM, "  Type 'help' for commands, 'ns <name>' to switch namespace, 'exit' to leave.\n"))

    cmd_map = _get_command_mapping()

    while True:
        try:
            line = input(_c(_C_BLUE, f"mesh ({ns})> ")).strip()
        except (KeyboardInterrupt, EOFError):
            print()
            break
        if not line:
            continue
        if line.lower() in ("exit", "quit", "q"):
            break

        try:
            parts = shlex.split(line)
        except ValueError as e:
            print(_c(_C_RED, f"  Parse error: {e}"))
            continue
        if not parts:
            continue

        cmd = parts[0].lower()
        cmd_args = parts[1:]

        if cmd == "clear":
            print("\033[H\033[J", end="")
            continue
        if cmd in ("ns", "use"):
            if not cmd_args:
                print(f"  Active namespace: '{ns}'")
            else:
                try:
                    ns = validate_namespace(cmd_args[0])
                    print(_c(_C_ACCENT, f"  Switched active namespace to '{ns}'"))
                except ValueError as e:
                    print(_c(_C_RED, f"  Error: {e}"))
            continue
        if cmd in ("shell", "repl", "i"):
            print(_c(_C_DIM, "  Already inside Mesh interactive shell."))
            continue
        if cmd in ("version", "-v", "--version"):
            print(f"mesh {VERSION}")
            continue

        resolved_cmd = _COMMAND_ALIASES.get(cmd, cmd)
        handler = cmd_map.get(resolved_cmd)
        if handler is None:
            print(_c(_C_RED, f"  Unknown command '{cmd}'. Type 'help' for available commands."))
            continue

        # Inject active REPL namespace if `--namespace` wasn't explicitly passed
        if (
            resolved_cmd in (
                "add",
                "ask",
                "inspect",
                "update",
                "forget",
                "status",
                "why",
                "context",
                "conflicts",
                "patterns",
                "digest",
                "export",
                "import",
                "capture",
            )
            and not any(a == "--namespace" or a.startswith("--namespace=") for a in cmd_args)
        ):
            cmd_args = [*cmd_args, "--namespace", ns]

        prev_argv = sys.argv
        sys.argv = [f"mesh-{resolved_cmd}", *cmd_args]
        try:
            handler()
        except SystemExit:
            pass
        finally:
            sys.argv = prev_argv
        print()


_BASH_COMPLETION_SCRIPT = """# Added by Mesh
_mesh_completion() {
    local cur prev words cword
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    local commands="add ask inspect update forget status why context setup install-skills daemon server dashboard http namespaces export import capture digest conflicts patterns doctor shell completion help version"
    local types="fact preference context result instruction process decision error general"
    if [[ ${COMP_CWORD} -eq 1 ]]; then
        COMPREPLY=( $(compgen -W "${commands}" -- "${cur}") )
        return 0
    fi
    case "${COMP_WORDS[1]}" in
        ask|search|find|add|learn|inspect|context)
            if [[ "${prev}" == "--type" ]]; then
                COMPREPLY=( $(compgen -W "${types}" -- "${cur}") )
                return 0
            fi
            if [[ "${prev}" == "--namespace" ]]; then
                local ns_list=$(python3 -c "from mesh.namespaces import list_namespaces; print(' '.join(n['namespace'] for n in list_namespaces()))" 2>/dev/null)
                COMPREPLY=( $(compgen -W "${ns_list}" -- "${cur}") )
                return 0
            fi
        ;;
    esac
}
complete -F _mesh_completion mesh mesh-add mesh-ask mesh-inspect
"""

_ZSH_COMPLETION_SCRIPT = """# Added by Mesh
#compdef mesh
_mesh_zsh_completion() {
    local -a commands types
    commands=(add ask inspect update forget status why context setup install-skills daemon server dashboard http namespaces export import capture digest conflicts patterns doctor shell completion help version)
    types=(fact preference context result instruction process decision error general)
    if (( CURRENT == 2 )); then
        _describe 'command' commands
        return
    fi
    if [[ "${words[CURRENT-1]}" == "--type" ]]; then
        _describe 'memory type' types
        return
    fi
}
if ! (( $+functions[compdef] )); then
    autoload -Uz compinit && compinit -i
fi
compdef _mesh_zsh_completion mesh
"""

_FISH_COMPLETION_SCRIPT = """# Added by Mesh
complete -c mesh -f
complete -c mesh -n "__fish_use_subcommand" -a "add ask inspect update forget status why context setup install-skills daemon server dashboard http namespaces export import capture digest conflicts patterns doctor shell completion help version"
complete -c mesh -l type -a "fact preference context result instruction process decision error general"
"""

_POWERSHELL_COMPLETION_SCRIPT = """# Added by Mesh
Register-ArgumentCompleter -Native -CommandName mesh -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)
    $commands = @('add','ask','inspect','update','forget','status','why','context','setup','install-skills','daemon','server','dashboard','http','namespaces','export','import','capture','digest','conflicts','patterns','doctor','shell','completion','help','version')
    $commands | Where-Object { $_ -like "$wordToComplete*" } | ForEach-Object {
        [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
    }
}
"""


def completion_cmd() -> None:
    parser = argparse.ArgumentParser(description="Generate shell autocompletion script.")
    parser.add_argument("shell", nargs="?", choices=["bash", "zsh", "fish", "powershell"], default="zsh")
    parser.add_argument("--install", action="store_true", help="Append completion to shell rc file (idempotent)")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip interactive y/n permission prompt")
    args = parser.parse_args()

    scripts = {
        "bash": _BASH_COMPLETION_SCRIPT,
        "zsh": _ZSH_COMPLETION_SCRIPT,
        "fish": _FISH_COMPLETION_SCRIPT,
        "powershell": _POWERSHELL_COMPLETION_SCRIPT,
    }
    script = scripts[args.shell]

    if args.install:
        from .daemon import request_user_permission

        home = Path.home()
        if args.shell == "zsh":
            rc_file = home / ".zshrc"
        elif args.shell == "bash":
            rc_file = home / ".bashrc"
        elif args.shell == "fish":
            rc_file = home / ".config" / "fish" / "completions" / "mesh.fish"
        else:
            rc_file = home / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1"

        if rc_file.exists():
            try:
                existing_rc = rc_file.read_text(encoding="utf-8", errors="ignore")
                if "# Added by Mesh" in existing_rc:
                    print(_c(_C_ACCENT, f"    Completion is already installed in {rc_file}"))
                    return
            except OSError:
                pass

        if not request_user_permission(
            f"append Mesh shell completion to {rc_file}",
            assume_yes=args.yes,
        ):
            print(_c(_C_DIM, "    Aborted installing shell completion."))
            return

        rc_file.parent.mkdir(parents=True, exist_ok=True)
        with open(rc_file, "a", encoding="utf-8") as f:
            f.write(f"\n{script.strip()}\n")
        print(_c(_C_ACCENT, f"    Installed {args.shell} completion into {rc_file}"))
    else:
        print(script)


def export_cmd() -> None:
    parser = argparse.ArgumentParser(description="Export Mesh memories to JSON.")
    parser.add_argument("output")
    parser.add_argument("--namespace", default=None)
    parser.add_argument("--no-embeddings", action="store_true")
    args = parser.parse_args()

    from .io import export_namespace

    export_namespace(
        namespace=_resolve_namespace(args.namespace),
        output_path=args.output,
        include_embeddings=not args.no_embeddings,
    )


def import_cmd() -> None:
    parser = argparse.ArgumentParser(description="Import Mesh memories from JSON.")
    parser.add_argument("input")
    parser.add_argument("--namespace", default=None)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from .io import import_namespace

    target_ns = _resolve_namespace(args.namespace) if args.namespace else None
    try:
        import_namespace(
            input_path=args.input,
            target_namespace=target_ns,
            merge_strategy="overwrite" if args.overwrite else "skip_existing",
            dry_run=args.dry_run,
        )
    except (FileNotFoundError, ValueError) as e:
        print(_c(_C_RED, f"  Error: {e}"), file=sys.stderr)
        sys.exit(1)


def namespaces_cmd() -> None:
    parser = argparse.ArgumentParser(description="Manage Mesh namespaces.")
    parser.add_argument(
        "command",
        nargs="?",
        default="list",
        choices=["list", "delete", "rename", "stats"],
    )
    parser.add_argument("args", nargs="*")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt on delete")
    args = parser.parse_args()

    from .namespaces import delete_namespace, list_namespaces, namespace_stats, rename_namespace

    if args.command == "list":
        items = list_namespaces()
        if not items:
            print("  No namespaces found.")
            return
        for ns in items:
            print(
                f"  {ns['namespace']:<25} {ns['memory_count']:>5} memories "
                f"({ns['local_only_count']} private, updated {str(ns['last_updated'])[:19]})"
            )
    elif args.command == "stats":
        target = _resolve_namespace(args.args[0] if args.args else None)
        try:
            st = namespace_stats(target)
            print(_c(_C_ACCENT, f"\n    Namespace Stats: '{target}'"))
            print(f"    Total Memories:   {st['memory_count']}")
            print(f"    Private:          {st['local_only_count']}")
            print(f"    Exportable:       {st['exportable_count']}")
            print(f"    Stale:            {st['stale_count']}")
            print(f"    Avg Confidence:   {st['avg_confidence']:.2f}")
            print(f"    Total Recalls:    {st['total_recalls']}")
            print(f"    Types:            {st['type_breakdown']}\n")
        except ValueError as e:
            print(_c(_C_RED, f"  Error: {e}"), file=sys.stderr)
            sys.exit(1)
    elif args.command == "delete":
        if not args.args:
            print(_c(_C_RED, "  Error: specify a namespace to delete."), file=sys.stderr)
            sys.exit(1)
        target = _resolve_namespace(args.args[0])
        from .daemon import request_user_permission

        if not request_user_permission(
            f"permanently delete namespace '{target}' and all its memories",
            assume_yes=args.yes,
        ):
            print(_c(_C_DIM, f"  Aborted deleting namespace '{target}'."))
            return
        removed = delete_namespace(target)
        print(_c(_C_ACCENT, f"  Deleted namespace '{target}' ({removed} memories removed)."))
    elif args.command == "rename":
        if len(args.args) < 2:
            print(_c(_C_RED, "  Error: usage: mesh namespaces rename <old> <new>"), file=sys.stderr)
            sys.exit(1)
        old_ns = _resolve_namespace(args.args[0])
        new_ns = _resolve_namespace(args.args[1])
        try:
            moved = rename_namespace(old_ns, new_ns)
            print(_c(_C_ACCENT, f"  Renamed namespace '{old_ns}' -> '{new_ns}' ({moved} memories moved)."))
        except ValueError as e:
            print(_c(_C_RED, f"  Error: {e}"), file=sys.stderr)
            sys.exit(1)


def context_cmd() -> None:
    from .memory import VALID_MEMORY_TYPES

    parser = argparse.ArgumentParser(description="Output Mesh memories as a system prompt block.")
    parser.add_argument("--query", default=None)
    parser.add_argument("--namespace", default=None)
    parser.add_argument("--count", "-n", "--n", dest="count", type=int, default=10)
    parser.add_argument("--format", choices=["markdown", "plain", "xml", "json"], default="markdown")
    parser.add_argument("--min-confidence", type=float, default=0.3)
    parser.add_argument("--type", dest="memory_type", choices=sorted(VALID_MEMORY_TYPES), default=None)
    parser.add_argument("--tags", default="")
    parser.add_argument("--include-private", action="store_true", help="Include local_only private memories")
    args = parser.parse_args()

    ns = _resolve_namespace(args.namespace)
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None

    proxied = _daemon_request(
        "/context",
        payload={
            "query": args.query,
            "namespace": ns,
            "count": args.count,
            "format": args.format,
            "min_confidence": args.min_confidence,
            "memory_type": args.memory_type,
            "tags": tags,
            "include_private": args.include_private,
        },
    )
    if proxied is not None and "_http_error" not in proxied and proxied.get("status") == "ok":
        print(proxied.get("context", ""))
        return

    from .context_builder import build_context

    res = build_context(
        query=args.query,
        namespace=ns,
        count=args.count,
        format=args.format,
        min_confidence=args.min_confidence,
        memory_type=args.memory_type,
        tags=tags,
        include_private=args.include_private,
    )
    print(res)


def why_cmd() -> None:
    parser = argparse.ArgumentParser(description="Show recent audit trail.")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--namespace", default=None)
    parser.add_argument("--action", choices=["read", "write", "update", "forget", "delete", "all"], default="all")
    args = parser.parse_args()

    from .audit import get_audit_log

    entries = get_audit_log(
        namespace=_resolve_namespace(args.namespace),
        limit=args.limit,
        action_filter=args.action,
    )
    if not entries:
        print("  No audit entries found.")
        return
    for e in entries:
        ts = str(e.get("timestamp", ""))[:19]
        action = e.get("action", "unknown")
        agent = e.get("agent_id") or "unknown"
        detail = e.get("query") if action == "recall" else e.get("content", "")
        print(f"  [{ts}] {action:<7} by {agent:<14} {str(detail or '')[:70]}")


def status_cmd() -> None:
    parser = argparse.ArgumentParser(description="Show Mesh status.")
    parser.add_argument("--namespace", default=None)
    parser.add_argument("--check-updates", action="store_true", help="Check PyPI for newer Mesh versions")
    args = parser.parse_args()

    check_for_updates(force=args.check_updates, blocking=bool(args.check_updates))
    from .namespaces import namespace_stats

    ns = _resolve_namespace(args.namespace)
    try:
        st = namespace_stats(ns)
        print(_c(_C_ACCENT, f"\n    Mesh Status — namespace: '{ns}'"))
        print(
            f"    Memories: {st['memory_count']} total ({st['local_only_count']} private, {st['stale_count']} stale)"
        )
        print(f"    Recalls:  {st['total_recalls']}\n")
    except ValueError:
        print(f"Namespace '{ns}' does not exist yet.")


def help_cmd() -> None:
    check_for_updates(force=False)
    print(_c(_C_ACCENT, f"\n    Mesh v{VERSION} — Shared Semantic & Hybrid Memory for AI Agents\n"))
    print("  CORE MEMORY")
    print("    mesh add <text>         Store a memory (aliases: learn, save)")
    print("    mesh ask <query>        5-stage hybrid search (aliases: search, find)")
    print("    mesh inspect            Browse & filter memories (alias: ls)")
    print("    mesh update <id>        Update memory by ID or 4+ char short ID (--clear-ttl supported)")
    print("    mesh forget <id>        Delete memory by ID or 4+ char short ID (alias: rm)")
    print()
    print("  AI & UTILITIES")
    print("    mesh shell              Interactive REPL shell with autocomplete (alias: i)")
    print("    mesh context            Generate system prompt context block")
    print("    mesh doctor             Diagnose ONNX model, daemon, SQLite & MCP configs")
    print("    mesh conflicts          Inspect & resolve memory contradictions")
    print("    mesh patterns           Surface top queries, knowledge gaps & stale items")
    print("    mesh completion         Install shell Tab autocompletion (bash, zsh, fish, powershell)")
    print()
    print("  SYSTEM & SERVERS")
    print("    mesh setup              Interactive background ONNX download & daemon setup")
    print("    mesh install-skills     Install Mesh agent skill to all AI coding agent folders")
    print("    mesh daemon <action>    start | stop | restart | status | warmup | unload")
    print("    mesh dashboard          Open web UI on http://localhost:7433")
    print("    mesh http               Start REST API server on http://localhost:7701")
    print("    mesh namespaces         List, stats, rename, or delete namespaces")
    print("    mesh export / import    Backup or restore namespaces to/from JSON")
    print()


def _lazy_daemon_cmd() -> None:
    from .daemon import daemon_cmd

    daemon_cmd()


def _lazy_dashboard_cmd() -> None:
    from .dashboard import run as dashboard_cmd

    dashboard_cmd()


def _lazy_digest_cmd() -> None:
    from .digest import digest_cmd

    digest_cmd()


def _lazy_http_cmd() -> None:
    from .http_server import start as http_cmd

    http_cmd()


def _lazy_server_cmd() -> None:
    from .mcp_server import run as server_cmd

    server_cmd()


def _lazy_capture_cmd() -> None:
    from .passive import capture_cmd

    capture_cmd()


def _get_command_mapping() -> dict[str, Callable[[], None]]:
    return {
        "setup": setup_cmd,
        "warmup": setup_cmd,
        "add": add_cmd,
        "ask": ask_cmd,
        "inspect": inspect_cmd,
        "update": update_cmd,
        "forget": forget_cmd,
        "status": status_cmd,
        "why": why_cmd,
        "context": context_cmd,
        "help": help_cmd,
        "export": export_cmd,
        "import": import_cmd,
        "namespaces": namespaces_cmd,
        "capture": _lazy_capture_cmd,
        "digest": _lazy_digest_cmd,
        "daemon": _lazy_daemon_cmd,
        "server": _lazy_server_cmd,
        "dashboard": _lazy_dashboard_cmd,
        "http": _lazy_http_cmd,
        "conflicts": conflicts_cmd,
        "patterns": patterns_cmd,
        "doctor": doctor_cmd,
        "shell": shell_cmd,
        "completion": completion_cmd,
        "install-skills": install_skills_cmd,
    }


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        help_cmd()
        return

    if sys.argv[1] in ("-v", "--version", "version"):
        print(f"mesh {VERSION}")
        return

    cmd = sys.argv[1].lower()
    if cmd.startswith("mesh-"):
        cmd = cmd[5:]
    cmd = _COMMAND_ALIASES.get(cmd, cmd)

    mapping = _get_command_mapping()
    if cmd in mapping:
        sys.argv = [f"mesh-{cmd}"] + sys.argv[2:]
        mapping[cmd]()
    else:
        suggestions = _suggest_did_you_mean(cmd, list(mapping.keys()))
        print(_c(_C_RED, f"    Unknown command '{cmd}'."))
        if suggestions:
            print(_c(_C_ACCENT, "\n  Did you mean?"))
            for s in suggestions:
                print(f"      mesh {s}")
        print(_c(_C_DIM, "\n  Run 'mesh help' to see available commands."))
        sys.exit(1)


if __name__ == "__main__":
    main()
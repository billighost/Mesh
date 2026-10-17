"""
mesh/mcp_server.py — Production MCP Server for Mesh.

Tools exposed:
  mesh_learn                   store a memory (with deduplication & contradiction detection)
  mesh_recall                  5-stage hybrid search + peripheral `other_possible_memories` snippets
  mesh_update                  update an existing memory in-place by ID (supports `clear_ttl`)
  mesh_forget                  delete a memory by ID
  mesh_inspect                 list memories in the namespace with optional filters
  mesh_context                 generate a formatted system prompt context block (excludes private by default)
  mesh_patterns                surface top topics, memory gaps, unused & stale memories
  mesh_contradictions          list unresolved (or resolved) memory contradictions
  mesh_resolve_contradiction   mark a conflict as resolved
  mesh_sync                    capture memories from session summary and incremental agent logs

Security & Privacy Hardening:
  - Background passive log ingestion is strictly opt-in (`MESH_ENABLE_AUTO_SYNC=1`) and asks
    for `y/n` user permission when started interactively
  - `mesh_recall` and `mesh_inspect` exclude `local_only=True` memories by default (`include_private=False`)
    unless explicitly requested or `MESH_MCP_INCLUDE_PRIVATE=1` is set
  - Daemon proxy detects stale keep-alive sockets before sending and uses fresh connections for mutating calls
"""
from __future__ import annotations

import asyncio
import contextlib
import glob
import http.client
import json
import os
import select
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Optional

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .audit import validate_namespace
from .context_builder import build_context_bundle
from .daemon import get_auth_headers, request_user_permission
from .memory import VALID_MEMORY_TYPES, Mesh, warmup_model

def _get_default_namespace() -> str:
    return (os.environ.get("MESH_NAMESPACE") or "shared").strip() or "shared"


def _get_default_agent_id() -> str:
    return (os.environ.get("MESH_AGENT_ID") or "mcp-agent").strip() or "mcp-agent"


def _get_default_include_private() -> bool:
    return os.environ.get("MESH_MCP_INCLUDE_PRIVATE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


NAMESPACE = _get_default_namespace()
AGENT_ID = _get_default_agent_id()
_DEFAULT_INCLUDE_PRIVATE = _get_default_include_private()

app = Server("mesh")

_MESH_LOCK = threading.RLock()
_MESH_INSTANCES: dict[tuple[str, str], Mesh] = {}
_mesh: Optional[Mesh] = None

# Thread-safe reference-counted stdout redirection for MCP stdio safety
_STDIO_REDIRECT_LOCK = threading.Lock()
_STDIO_REDIRECT_DEPTH = 0
_ORIG_STDOUT: Optional[Any] = None

# Tracks foreground tool activity so background sync never contends with IDE tool calls
_ACTIVITY_LOCK = threading.Lock()
_ACTIVE_TOOL_CALLS = 0
_LAST_TOOL_CALL_TS = 0.0
_STDIO_SERVER_ACTIVE = False

# Daemon proxy state & persistent HTTP keep-alive connection
_DAEMON_LOCK = threading.Lock()
_DAEMON_CHECK_TS = 0.0
_DAEMON_LAST_USE_TS = 0.0
_DAEMON_AVAILABLE = False
_DAEMON_CONN: Optional[http.client.HTTPConnection] = None

# Tracks (mtime_ns, byte_offset) per log file so passive sync only reads new log output
_LOG_WATERMARKS: dict[str, tuple[int, int]] = {}
_ALLOWED_LOG_EXTENSIONS = {".log", ".txt", ".jsonl", ".md"}

_SORTED_MEMORY_TYPES = sorted(VALID_MEMORY_TYPES)

_MUTATING_TOOLS = {
    "mesh_learn",
    "mesh_update",
    "mesh_forget",
    "mesh_resolve_contradiction",
    "mesh_sync",
}


def evict_mcp_mesh_cache(namespace: Optional[str] = None) -> None:
    """Evict cached `Mesh` instances for `namespace` (or all namespaces if None)."""
    global _mesh
    with _MESH_LOCK:
        if namespace is None:
            _MESH_INSTANCES.clear()
            _mesh = None
            return
        stale_keys = [k for k in _MESH_INSTANCES if k[0] == namespace]
        for k in stale_keys:
            _MESH_INSTANCES.pop(k, None)
        if _mesh is not None and _mesh.namespace == namespace:
            _mesh = None


@contextlib.contextmanager
def _safe_redirect_stdout_to_stderr() -> Iterator[None]:
    """
    Thread-safe, reference-counted stdout-to-stderr redirector.
    Prevents concurrent `asyncio.to_thread` tool executions or background sync workers
    from corrupting the global `sys.stdout` used by `stdio_server`.
    """
    global _STDIO_REDIRECT_DEPTH, _ORIG_STDOUT
    with _STDIO_REDIRECT_LOCK:
        if _STDIO_REDIRECT_DEPTH == 0:
            _ORIG_STDOUT = sys.stdout
            sys.stdout = sys.stderr
        _STDIO_REDIRECT_DEPTH += 1
    try:
        yield
    finally:
        with _STDIO_REDIRECT_LOCK:
            _STDIO_REDIRECT_DEPTH -= 1
            if _STDIO_REDIRECT_DEPTH == 0 and _ORIG_STDOUT is not None:
                sys.stdout = _ORIG_STDOUT
                _ORIG_STDOUT = None


@contextlib.contextmanager
def _track_foreground_tool_call() -> Iterator[None]:
    """Mark a foreground MCP tool call as active so background sync yields."""
    global _ACTIVE_TOOL_CALLS, _LAST_TOOL_CALL_TS
    with _ACTIVITY_LOCK:
        _ACTIVE_TOOL_CALLS += 1
        _LAST_TOOL_CALL_TS = time.monotonic()
    try:
        yield
    finally:
        with _ACTIVITY_LOCK:
            _ACTIVE_TOOL_CALLS = max(0, _ACTIVE_TOOL_CALLS - 1)
            _LAST_TOOL_CALL_TS = time.monotonic()


def _is_foreground_busy(cooldown_seconds: float = 10.0) -> bool:
    """Return True if a tool call is currently executing or completed within `cooldown_seconds`."""
    with _ACTIVITY_LOCK:
        if _ACTIVE_TOOL_CALLS > 0:
            return True
        if _LAST_TOOL_CALL_TS > 0 and (time.monotonic() - _LAST_TOOL_CALL_TS) < cooldown_seconds:
            return True
    return False


def _is_http_conn_stale(conn: Optional[http.client.HTTPConnection], idle_seconds: float) -> bool:
    """
    Detect whether a pooled keep-alive `HTTPConnection` has been closed by Uvicorn
    (which drops idle connections after ~5s) before attempting to write a request to it.
    """
    if conn is None or getattr(conn, "sock", None) is None:
        return True
    if idle_seconds >= 3.5:
        return True
    try:
        readable, _, exceptional = select.select([conn.sock], [], [conn.sock], 0.0)
        if readable or exceptional:
            return True
    except Exception:
        return True
    return False


def _close_daemon_conn_locked() -> None:
    global _DAEMON_CONN
    if _DAEMON_CONN is not None:
        try:
            _DAEMON_CONN.close()
        except Exception:
            pass
        _DAEMON_CONN = None


def _try_proxy_via_daemon(name: str, arguments: dict[str, Any]) -> Optional[dict[str, Any]]:
    """
    If running as a stdio MCP process and a local Mesh HTTP daemon is active on
    127.0.0.1:7701 (or MESH_HTTP_PORT), proxy the tool execution over an authenticated
    connection.
    - Always uses a fresh connection for mutating tools (`_MUTATING_TOOLS`) or after >3.5s idle
      so Uvicorn's keep-alive timeout never drops the first write after idle.
    - Surfaces 4xx/5xx daemon errors directly instead of re-executing locally.
    """
    global _DAEMON_CHECK_TS, _DAEMON_LAST_USE_TS, _DAEMON_AVAILABLE, _DAEMON_CONN
    if not _STDIO_SERVER_ACTIVE:
        return None
    if os.environ.get("MESH_NO_DAEMON_PROXY", "").strip().lower() in ("1", "true", "yes"):
        return None

    now = time.monotonic()
    if not _DAEMON_AVAILABLE and (now - _DAEMON_CHECK_TS) < 3.0:
        return None
    _DAEMON_CHECK_TS = now

    host = os.environ.get("MESH_HTTP_HOST", "127.0.0.1")
    port = int(os.environ.get("MESH_HTTP_PORT", 7701))
    is_mutating = name in _MUTATING_TOOLS

    payload_bytes = json.dumps(
        {
            "name": name,
            "arguments": arguments,
            "default_namespace": _get_default_namespace(),
            "agent_id": _get_default_agent_id(),
        }
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Connection": "close" if is_mutating else "keep-alive",
        **get_auth_headers(),
    }

    with _DAEMON_LOCK:
        for attempt in range(2):
            request_sent = False
            reused_conn = False
            try:
                idle_secs = (time.monotonic() - _DAEMON_LAST_USE_TS) if _DAEMON_LAST_USE_TS > 0 else 999.0
                if is_mutating or _is_http_conn_stale(_DAEMON_CONN, idle_secs):
                    _close_daemon_conn_locked()
                    conn = http.client.HTTPConnection(host, port, timeout=10.0)
                    conn.connect()
                    _DAEMON_CONN = conn
                else:
                    reused_conn = True

                assert _DAEMON_CONN is not None
                _DAEMON_CONN.request(
                    "POST",
                    "/mcp",
                    body=payload_bytes,
                    headers=headers,
                )
                request_sent = True
                resp = _DAEMON_CONN.getresponse()
                raw_body = resp.read()
                _DAEMON_LAST_USE_TS = time.monotonic()
                _DAEMON_AVAILABLE = True

                if is_mutating:
                    _close_daemon_conn_locked()

                if resp.status == 200:
                    data = json.loads(raw_body.decode("utf-8"))
                    if isinstance(data, dict) and "payload" in data:
                        return data["payload"]
                else:
                    try:
                        err_json = json.loads(raw_body.decode("utf-8"))
                        detail = err_json.get("detail") if isinstance(err_json, dict) else str(err_json)
                    except Exception:
                        detail = f"Daemon HTTP error {resp.status}"
                    return {"error": str(detail), "status": "error"}
                break
            except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError) as exc:
                _close_daemon_conn_locked()
                # If a reused keep-alive connection was closed by the server before reading,
                # retry once on a fresh socket.
                if attempt == 0 and reused_conn:
                    continue
                if request_sent and is_mutating:
                    return {
                        "error": f"Daemon connection interrupted during '{name}': {exc}",
                        "status": "unknown",
                    }
                _DAEMON_AVAILABLE = False
                break
            except Exception as exc:
                _close_daemon_conn_locked()
                if request_sent and is_mutating:
                    return {
                        "error": f"Daemon connection interrupted during '{name}': {exc}",
                        "status": "unknown",
                    }
                if attempt == 0 and _DAEMON_AVAILABLE and not request_sent:
                    continue
                _DAEMON_AVAILABLE = False
                break
    return None


def get_mesh(namespace: Optional[str] = None, agent_id: Optional[str] = None) -> Mesh:
    """Return a cached Mesh instance for the requested namespace and agent_id."""
    global _mesh
    raw_ns = (namespace or _get_default_namespace()).strip() or "shared"
    ns = validate_namespace(raw_ns)
    aid = (agent_id or _get_default_agent_id()).strip() or "mcp-agent"

    key = (ns, aid)
    instance = _MESH_INSTANCES.get(key)
    if instance is not None:
        return instance

    with _MESH_LOCK:
        instance = _MESH_INSTANCES.get(key)
        if instance is None:
            instance = Mesh(namespace=ns, agent_id=aid)
            _MESH_INSTANCES[key] = instance
        _mesh = instance
        return instance


def detect_agent_logs() -> list[str]:
    """Auto-detect log directories for various AI agents."""
    paths: list[str] = []
    home = str(Path.home())

    antigravity_base = os.path.join(home, ".gemini", "antigravity", "brain")
    if os.path.exists(antigravity_base):
        paths.extend(
            glob.glob(os.path.join(antigravity_base, "*", ".system_generated", "logs"))
        )

    if os.name == "nt":
        claude_logs = os.path.join(os.environ.get("APPDATA", ""), "Claude", "logs")
        cursor_logs = os.path.join(os.environ.get("APPDATA", ""), "Cursor", "logs")
    elif sys.platform == "darwin":
        claude_logs = os.path.join(home, "Library", "Application Support", "Claude", "logs")
        cursor_logs = os.path.join(home, "Library", "Application Support", "Cursor", "logs")
    else:
        config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        claude_logs = os.path.join(config_home, "Claude", "logs")
        cursor_logs = os.path.join(config_home, "Cursor", "logs")

    if os.path.exists(claude_logs):
        paths.append(claude_logs)
    if os.path.exists(cursor_logs):
        paths.append(cursor_logs)

    return [p for p in paths if os.path.isdir(p)]


def _seed_log_watermarks(paths: list[str]) -> None:
    """
    Record current (mtime_ns, st_size) watermarks for existing log files on startup
    so the background worker only ingests newly appended log lines.
    """
    for raw_path in paths:
        p = Path(raw_path).expanduser()
        if not p.exists():
            continue
        if p.is_file():
            try:
                st = p.stat()
                _LOG_WATERMARKS.setdefault(str(p), (st.st_mtime_ns, st.st_size))
            except OSError:
                pass
        elif p.is_dir():
            try:
                for child in p.rglob("*"):
                    if child.is_file() and child.suffix.lower() in _ALLOWED_LOG_EXTENSIONS:
                        try:
                            st = child.stat()
                            _LOG_WATERMARKS.setdefault(str(child), (st.st_mtime_ns, st.st_size))
                        except OSError:
                            continue
            except OSError:
                continue


def _collect_incremental_logs(
    paths: list[str],
    max_files: int = 10,
    max_bytes_per_file: int = 16384,
) -> str:
    """
    Read newly appended content from log files inside `paths`, discarding any partial
    leading line when seeking into a large file.
    """
    candidate_files: list[tuple[int, int, Path]] = []
    for raw_path in paths:
        p = Path(raw_path).expanduser()
        if not p.exists():
            continue
        if p.is_file():
            try:
                st = p.stat()
                if _LOG_WATERMARKS.get(str(p)) != (st.st_mtime_ns, st.st_size):
                    candidate_files.append((st.st_mtime_ns, st.st_size, p))
            except OSError:
                pass
        elif p.is_dir():
            try:
                for child in p.rglob("*"):
                    if (
                        child.is_file()
                        and child.suffix.lower() in _ALLOWED_LOG_EXTENSIONS
                    ):
                        try:
                            st = child.stat()
                            if _LOG_WATERMARKS.get(str(child)) != (st.st_mtime_ns, st.st_size):
                                candidate_files.append((st.st_mtime_ns, st.st_size, child))
                        except OSError:
                            continue
            except OSError:
                continue

    if not candidate_files:
        return ""

    candidate_files.sort(key=lambda item: item[0], reverse=True)
    chunks: list[str] = []

    for mtime_ns, size, file_path in candidate_files[:max_files]:
        key = str(file_path)
        try:
            prev_mtime, prev_offset = _LOG_WATERMARKS.get(key, (0, 0))
            if mtime_ns == prev_mtime and size == prev_offset:
                continue
            start_offset = prev_offset if size >= prev_offset else 0
            truncated_mid_file = False
            if size - start_offset > max_bytes_per_file:
                start_offset = max(0, size - max_bytes_per_file)
                truncated_mid_file = start_offset > 0
            with open(file_path, "rb") as f:
                f.seek(start_offset)
                raw_bytes = f.read(max_bytes_per_file)
            _LOG_WATERMARKS[key] = (mtime_ns, size)
            text = raw_bytes.decode("utf-8", errors="ignore")
            if truncated_mid_file and "\n" in text:
                text = text.split("\n", 1)[1]
            text = text.strip()
            if text:
                chunks.append(text)
        except OSError:
            continue

    return "\n\n".join(chunks)


def background_sync_worker() -> None:
    """
    Warms up the local namespace cache and (ONLY if explicitly opted in via
    `MESH_ENABLE_AUTO_SYNC=1`) periodically runs local passive capture on newly
    appended agent logs when idle.
    """
    from .passive import extract_memories_from_log

    try:
        log_dirs = detect_agent_logs()
        if log_dirs:
            _seed_log_watermarks(log_dirs)
    except Exception:
        pass

    time.sleep(0.15)
    with _safe_redirect_stdout_to_stderr():
        if not _is_foreground_busy(cooldown_seconds=2.0):
            try:
                def_ns = _get_default_namespace()
                def_aid = _get_default_agent_id()
                get_mesh(def_ns, def_aid)
                warmup_model(def_ns, assume_yes=False)
            except Exception:
                pass

    auto_sync_enabled = os.environ.get("MESH_ENABLE_AUTO_SYNC", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if not auto_sync_enabled or os.environ.get("MESH_DISABLE_AUTO_SYNC", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return

    while True:
        time.sleep(120)
        if _is_foreground_busy(cooldown_seconds=10.0):
            continue
        try:
            log_dirs = detect_agent_logs()
            if log_dirs and not _is_foreground_busy(cooldown_seconds=10.0):
                log_text = _collect_incremental_logs(log_dirs, max_files=4, max_bytes_per_file=8192)
                if log_text and not _is_foreground_busy(cooldown_seconds=10.0):
                    with _safe_redirect_stdout_to_stderr():
                        extract_memories_from_log(
                            log_text,
                            namespace=_get_default_namespace(),
                            agent_id="auto-sync-worker",
                            force_local=True,
                        )
        except Exception:
            pass


_MCP_TOOLS: list[types.Tool] = [
    types.Tool(
        name="mesh_learn",
        description=(
            "Store information in shared agent memory. "
            "Automatically deduplicates identical facts and checks for contradictions "
            "with existing memories. If a conflicting memory is found, the new memory "
            "is still stored and a conflict warning is returned for review. "
            "Use ttl_days for time-sensitive facts (e.g. staging IPs, sprint goals). "
            "Set local_only=true for sensitive or private data so it is excluded from exports, "
            "context blocks, and default agent recalls."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": (
                        "The specific fact, decision, process, or instruction to remember. "
                        "Good: 'Production PostgreSQL runs on port 5432.' "
                        "Bad: 'Database info.'"
                    ),
                },
                "memory_type": {
                    "type": "string",
                    "enum": _SORTED_MEMORY_TYPES,
                    "default": "fact",
                    "description": (
                        "fact: objective info | preference: user/system prefs | "
                        "context: project background | result: task outcome | "
                        "instruction: rules to follow | process: workflow steps | "
                        "decision: architectural choice | error: bug/fix note | general: other."
                    ),
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "default": 1.0,
                    "description": "Confidence score (0.0 to 1.0). Use <0.7 for tentative info.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional topic labels, e.g. ['api', 'auth', 'db'].",
                },
                "ttl_days": {
                    "type": "number",
                    "description": (
                        "Optional days before confidence decays to 0. "
                        "Example: 30 for a temporary server IP, 7 for a sprint goal."
                    ),
                },
                "local_only": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Set true to mark private and exclude from exports, system prompt context blocks, "
                        "and default agent recalls unless include_private=true is explicitly passed."
                    ),
                },
                "namespace": {
                    "type": "string",
                    "description": "Optional namespace override (defaults to active namespace).",
                },
            },
            "required": ["content"],
        },
    ),
    types.Tool(
        name="mesh_recall",
        description=(
            "Search shared agent memory using 5-stage hybrid search "
            "(semantic meaning + Robertson-Sparck Jones BM25+ + domain synonyms + exact identifiers). "
            "Excludes `local_only=true` private memories by default unless `include_private=true` is passed. "
            "Also returns `other_possible_memories` snippets so you can see other related or "
            "filter-excluded memories in the namespace. Call this at the start of tasks."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What you want to find (natural language, synonyms, or exact keywords).",
                },
                "n": {
                    "type": "integer",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Maximum number of primary results to return.",
                },
                "min_similarity": {
                    "type": "number",
                    "default": 0.0,
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "Minimum similarity threshold (0.0 to 1.0).",
                },
                "min_confidence": {
                    "type": "number",
                    "default": 0.0,
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "Minimum effective confidence after decay (0.0 to 1.0).",
                },
                "include_stale": {
                    "type": "boolean",
                    "default": True,
                    "description": "Set false to exclude decayed/stale memories.",
                },
                "include_private": {
                    "type": "boolean",
                    "default": False,
                    "description": "Set true only if you explicitly need local_only private memories returned.",
                },
                "memory_type": {
                    "type": "string",
                    "enum": _SORTED_MEMORY_TYPES,
                    "description": "Optional filter to restrict primary results to a single memory_type.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional filter requiring returned memories to have these tags.",
                },
                "source_agent": {
                    "type": "string",
                    "description": "Optional filter by the agent ID that stored the memory.",
                },
                "include_other_possible": {
                    "type": "boolean",
                    "default": True,
                    "description": "Include peripheral snippets of other related or filter-excluded memories.",
                },
                "max_other_possible": {
                    "type": "integer",
                    "default": 5,
                    "minimum": 0,
                    "maximum": 20,
                    "description": "Max peripheral memory snippets to include in `other_possible_memories`.",
                },
                "namespace": {
                    "type": "string",
                    "description": "Optional namespace override.",
                },
            },
            "required": ["query"],
        },
    ),
    types.Tool(
        name="mesh_update",
        description=(
            "Update an existing memory in-place by its ID without losing its UUID, "
            "creation timestamp, original author, or access count. Only provide the fields you want to change."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "UUID or 4+ character short ID prefix of the memory to update.",
                },
                "content": {
                    "type": "string",
                    "description": "New text content (re-embeds automatically if changed).",
                },
                "memory_type": {
                    "type": "string",
                    "enum": _SORTED_MEMORY_TYPES,
                    "description": "Updated memory type.",
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "Updated confidence score.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Updated list of tags.",
                },
                "ttl_days": {
                    "type": "number",
                    "description": "Updated TTL in days (resets decay timer). Pass 0 or set clear_ttl=true to clear TTL.",
                },
                "clear_ttl": {
                    "type": "boolean",
                    "default": False,
                    "description": "Set true to remove an existing TTL and make the memory permanent.",
                },
                "local_only": {
                    "type": "boolean",
                    "description": "Updated privacy flag.",
                },
                "namespace": {
                    "type": "string",
                    "description": "Optional namespace override.",
                },
            },
            "required": ["memory_id"],
        },
    ),
    types.Tool(
        name="mesh_forget",
        description="Delete a specific memory permanently by its ID (scrubbing audit & conflict logs).",
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "The UUID or 4+ character short ID prefix of the memory to delete.",
                },
                "namespace": {
                    "type": "string",
                    "description": "Optional namespace override.",
                },
            },
            "required": ["memory_id"],
        },
    ),
    types.Tool(
        name="mesh_inspect",
        description=(
            "List memories in the namespace sorted by most recently accessed. "
            "Excludes `local_only=true` private memories by default unless `include_private=true` is passed."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 200,
                },
                "memory_type": {
                    "type": "string",
                    "enum": _SORTED_MEMORY_TYPES,
                    "description": "Optional filter by memory type.",
                },
                "tag": {
                    "type": "string",
                    "description": "Optional filter by a single tag.",
                },
                "include_private": {
                    "type": "boolean",
                    "default": False,
                    "description": "Set true only if you explicitly need local_only private memories returned.",
                },
                "namespace": {
                    "type": "string",
                    "description": "Optional namespace override.",
                },
            },
        },
    ),
    types.Tool(
        name="mesh_context",
        description=(
            "Generate a formatted context block of top project memories ready to prime "
            "an agent session. Excludes `local_only` private memories by default."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Optional topic query to focus the context block.",
                },
                "count": {
                    "type": "integer",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 50,
                },
                "max_memories": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Optional alias for count.",
                },
                "format": {
                    "type": "string",
                    "enum": ["markdown", "plain", "xml", "json"],
                    "default": "markdown",
                },
                "min_confidence": {
                    "type": "number",
                    "default": 0.3,
                    "minimum": 0.0,
                    "maximum": 1.0,
                },
                "memory_type": {
                    "type": "string",
                    "enum": _SORTED_MEMORY_TYPES,
                    "description": "Optional filter by memory type.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional filter by tags.",
                },
                "include_private": {
                    "type": "boolean",
                    "default": False,
                    "description": "Set true only if you explicitly want local_only memories included.",
                },
                "namespace": {
                    "type": "string",
                    "description": "Optional namespace override.",
                },
            },
        },
    ),
    types.Tool(
        name="mesh_patterns",
        description=(
            "Analyse memory usage patterns across agents. "
            "Returns top_topics, memory_gaps (frequently searched but poorly answered), "
            "unused_memories, and stale_memories."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace": {
                    "type": "string",
                    "description": "Optional namespace override.",
                },
            },
        },
    ),
    types.Tool(
        name="mesh_contradictions",
        description="List unresolved (or resolved) contradictions between stored memories.",
        inputSchema={
            "type": "object",
            "properties": {
                "resolved": {
                    "type": "boolean",
                    "default": False,
                    "description": "Set true to list already-resolved contradictions.",
                },
                "namespace": {
                    "type": "string",
                    "description": "Optional namespace override.",
                },
            },
        },
    ),
    types.Tool(
        name="mesh_resolve_contradiction",
        description="Mark a specific contradiction ID as resolved after reviewing the conflict.",
        inputSchema={
            "type": "object",
            "properties": {
                "contradiction_id": {
                    "type": "string",
                    "description": "UUID or 4+ character short ID prefix of the contradiction record.",
                },
                "namespace": {
                    "type": "string",
                    "description": "Optional namespace override.",
                },
            },
            "required": ["contradiction_id"],
        },
    ),
    types.Tool(
        name="mesh_sync",
        description=(
            "Store a session turn summary and capture concrete facts "
            "from recent agent session logs into the namespace."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "A brief summary of discoveries or work completed in this turn.",
                },
                "namespace": {
                    "type": "string",
                    "description": "Optional namespace override.",
                },
            },
        },
    ),
]


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return _MCP_TOOLS


def _execute_tool_sync(
    name: str,
    arguments: dict[str, Any],
    default_namespace: Optional[str] = None,
    agent_id: Optional[str] = None,
    allow_daemon_proxy: bool = True,
) -> dict[str, Any]:
    """Execute a tool synchronously with thread-safe stdout redirection for MCP stdio safety."""
    with _track_foreground_tool_call():
        if allow_daemon_proxy:
            proxied = _try_proxy_via_daemon(name, arguments)
            if proxied is not None:
                return proxied

        ns_override = arguments.get("namespace") or default_namespace
        mesh = get_mesh(namespace=ns_override, agent_id=agent_id)

        with _safe_redirect_stdout_to_stderr():
            if name == "mesh_learn":
                result = mesh.learn(
                    content=arguments["content"],
                    memory_type=arguments.get("memory_type", "fact"),
                    confidence=float(arguments.get("confidence", 1.0)),
                    tags=arguments.get("tags", []),
                    ttl_days=arguments.get("ttl_days"),
                    local_only=bool(arguments.get("local_only", False)),
                )
                response: dict[str, Any] = {
                    "status": result["status"],
                    "memory_id": result["memory_id"],
                    "deduplicated": result.get("deduplicated", False),
                    "namespace": mesh.namespace,
                    "total_memories": mesh.count(),
                }
                if result.get("conflict"):
                    response["conflict_warning"] = (
                        "A potentially contradictory memory already exists. "
                        "Review and delete or update the old one if this new memory supersedes it."
                    )
                    response["conflict"] = result["conflict"]
                return response

            elif name == "mesh_recall":
                limit_n = int(arguments.get("n") or arguments.get("count") or 5)
                include_other = bool(arguments.get("include_other_possible", True))
                max_other = int(arguments.get("max_other_possible", 5)) if include_other else 0
                inc_priv = (
                    bool(arguments["include_private"])
                    if "include_private" in arguments and arguments["include_private"] is not None
                    else _get_default_include_private()
                )
                return mesh.recall_with_context(
                    query=arguments["query"],
                    n=limit_n,
                    min_similarity=float(arguments.get("min_similarity", 0.0)),
                    min_confidence=float(arguments.get("min_confidence", 0.0)),
                    include_stale=bool(arguments.get("include_stale", True)),
                    memory_type=arguments.get("memory_type"),
                    tags=arguments.get("tags"),
                    source_agent=arguments.get("source_agent"),
                    max_other_possible=max_other,
                    include_private=inc_priv,
                )

            elif name == "mesh_update":
                updated = mesh.update(
                    memory_id=arguments["memory_id"],
                    content=arguments.get("content"),
                    memory_type=arguments.get("memory_type"),
                    confidence=arguments.get("confidence"),
                    tags=arguments.get("tags"),
                    ttl_days=arguments.get("ttl_days"),
                    clear_ttl=bool(arguments.get("clear_ttl", False)),
                    local_only=arguments.get("local_only"),
                )
                return {
                    "status": "updated",
                    "memory_id": updated["memory_id"],
                    "namespace": mesh.namespace,
                    "memory": updated["memory"],
                }

            elif name == "mesh_forget":
                deleted = mesh.forget(arguments["memory_id"])
                return {
                    "status": "deleted" if deleted else "not_found",
                    "memory_id": arguments["memory_id"],
                    "remaining": mesh.count(),
                }

            elif name == "mesh_inspect":
                inc_priv = (
                    bool(arguments["include_private"])
                    if "include_private" in arguments and arguments["include_private"] is not None
                    else _get_default_include_private()
                )
                memories = mesh.inspect(
                    limit=int(arguments.get("limit", 10)),
                    memory_type=arguments.get("memory_type"),
                    tag=arguments.get("tag"),
                    include_private=inc_priv,
                )
                return {
                    "namespace": mesh.namespace,
                    "total": mesh.count(),
                    "memories": memories,
                }

            elif name == "mesh_context":
                bundle = build_context_bundle(
                    query=arguments.get("query"),
                    namespace=mesh.namespace,
                    count=int(arguments.get("count") or 10),
                    max_memories=arguments.get("max_memories"),
                    n=arguments.get("n"),
                    format=arguments.get("format", "markdown"),
                    min_confidence=float(arguments.get("min_confidence", 0.3)),
                    memory_type=arguments.get("memory_type"),
                    tags=arguments.get("tags"),
                    include_private=bool(arguments.get("include_private", False)),
                    mesh_instance=mesh,
                )
                return {
                    "namespace": bundle["namespace"],
                    "format": bundle["format"],
                    "context": bundle["context"],
                }

            elif name == "mesh_patterns":
                patterns = mesh.patterns()
                return {
                    "namespace": mesh.namespace,
                    **patterns,
                }

            elif name == "mesh_contradictions":
                resolved = bool(arguments.get("resolved", False))
                conflicts = mesh.contradictions(resolved=resolved)
                return {
                    "namespace": mesh.namespace,
                    "resolved": resolved,
                    "count": len(conflicts),
                    "contradictions": conflicts,
                }

            elif name == "mesh_resolve_contradiction":
                mesh.resolve_contradiction(arguments["contradiction_id"])
                return {
                    "status": "resolved",
                    "contradiction_id": arguments["contradiction_id"],
                    "namespace": mesh.namespace,
                }

            elif name == "mesh_sync":
                from .passive import (
                    extract_memories_from_log,
                    _is_poison_attempt,
                    _is_credential,
                    _redact_secrets_for_cloud,
                )

                summary_text = (arguments.get("summary") or "").strip()
                if summary_text:
                    if _is_poison_attempt(summary_text):
                        return {
                            "status": "rejected",
                            "error": "Memory rejected: input contains prompt injection or adversarial patterns.",
                            "namespace": mesh.namespace,
                        }
                    is_cred = _is_credential(summary_text)
                    cleaned_summary = _redact_secrets_for_cloud(summary_text) if is_cred else summary_text
                    mesh.learn(
                        content=cleaned_summary,
                        memory_type="result",
                        tags=["session-sync"],
                        local_only=is_cred,
                    )

                log_dirs = detect_agent_logs()
                log_text = _collect_incremental_logs(log_dirs) if log_dirs else ""
                captured_count = 0
                if log_text:
                    allow_cloud = os.environ.get("MESH_ALLOW_CLOUD_SYNC", "").strip().lower() in (
                        "1",
                        "true",
                        "yes",
                    )
                    capture_res = extract_memories_from_log(
                        log_text,
                        namespace=mesh.namespace,
                        agent_id=mesh.agent_id,
                        force_local=not allow_cloud,
                        assume_yes=allow_cloud,
                    )
                    captured_count = int(capture_res.get("stored_count", 0))

                return {
                    "status": "synchronized",
                    "namespace": mesh.namespace,
                    "manual_summary": summary_text or "none",
                    "passive_memories_captured": captured_count,
                    "total_memories": mesh.count(),
                }

            else:
                return {"error": f"Unknown tool: {name}"}


@app.call_tool()
async def call_tool(name: str, arguments: Optional[dict[str, Any]]) -> list[types.TextContent]:
    args = arguments or {}
    try:
        payload = await asyncio.to_thread(_execute_tool_sync, name, args)
        return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]
    except Exception as e:
        return [
            types.TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": name}, indent=2),
            )
        ]


async def _run() -> None:
    global _STDIO_SERVER_ACTIVE
    _STDIO_SERVER_ACTIVE = True
    try:
        async with stdio_server() as (read_stream, write_stream):
            await app.run(
                read_stream,
                write_stream,
                app.create_initialization_options(),
            )
    finally:
        _STDIO_SERVER_ACTIVE = False


def run() -> None:
    """Entry point for: mesh-server"""
    auto_sync = os.environ.get("MESH_ENABLE_AUTO_SYNC", "").strip().lower() in ("1", "true", "yes")
    if auto_sync:
        threading.Thread(target=background_sync_worker, daemon=True).start()
    elif request_user_permission("start the background MCP cache warmup worker"):
        threading.Thread(target=background_sync_worker, daemon=True).start()
    asyncio.run(_run())


if __name__ == "__main__":
    run()
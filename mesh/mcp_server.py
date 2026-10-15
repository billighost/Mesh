"""
mesh/mcp_server.py — Production MCP Server for Mesh.

Tools exposed:
  mesh_learn                 — store a memory (with deduplication & contradiction detection)
  mesh_recall                — hybrid search (dense cosine + FTS5 BM25 + exact token match)
  mesh_update                — update an existing memory in-place by ID
  mesh_forget                — delete a memory by ID
  mesh_inspect               — list memories in the namespace with optional filters
  mesh_context               — generate a formatted system prompt context block
  mesh_patterns              — surface top topics, memory gaps, unused & stale memories
  mesh_contradictions        — list unresolved (or resolved) memory contradictions
  mesh_resolve_contradiction — mark a conflict as resolved
  mesh_sync                  — capture memories from session summary and incremental agent logs
"""

from __future__ import annotations

import asyncio
import contextlib
import glob
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .audit import log_forget, log_learn, log_recall, log_update
from .context_builder import build_context
from .memory import VALID_MEMORY_TYPES, Mesh, warmup_model

NAMESPACE = os.environ.get("MESH_NAMESPACE", "shared")
AGENT_ID = os.environ.get("MESH_AGENT_ID", "mcp-agent")

app = Server("mesh")

_MESH_LOCK = threading.RLock()
_MESH_INSTANCES: dict[tuple[str, str], Mesh] = {}
_mesh: Optional[Mesh] = None

# Tracks (mtime_ns, byte_offset) per log file so passive sync only reads new log output
_LOG_WATERMARKS: dict[str, tuple[int, int]] = {}
_ALLOWED_LOG_EXTENSIONS = {".log", ".txt", ".jsonl", ".md"}


def get_mesh(namespace: Optional[str] = None, agent_id: Optional[str] = None) -> Mesh:
    """Return a cached Mesh instance for the requested namespace and agent_id."""
    global _mesh
    ns = (namespace or NAMESPACE).strip() or "shared"
    aid = (agent_id or AGENT_ID).strip() or "mcp-agent"

    with _MESH_LOCK:
        if ns == NAMESPACE and aid == AGENT_ID and _mesh is not None:
            return _mesh
        key = (ns, aid)
        instance = _MESH_INSTANCES.get(key)
        if instance is None:
            instance = Mesh(namespace=ns, agent_id=aid)
            _MESH_INSTANCES[key] = instance
            if ns == NAMESPACE and aid == AGENT_ID:
                _mesh = instance
        return instance


def detect_agent_logs() -> list[str]:
    """Auto-detect log directories for various AI agents."""
    paths: list[str] = []
    home = str(Path.home())

    # Antigravity logs
    antigravity_base = os.path.join(home, ".gemini", "antigravity", "brain")
    if os.path.exists(antigravity_base):
        paths.extend(
            glob.glob(os.path.join(antigravity_base, "*", ".system_generated", "logs"))
        )

    # Claude Desktop logs (macOS / Windows)
    if os.name == "nt":
        claude_logs = os.path.join(os.environ.get("APPDATA", ""), "Claude", "logs")
    else:
        claude_logs = os.path.join(home, "Library", "Application Support", "Claude", "logs")

    if os.path.exists(claude_logs):
        paths.append(claude_logs)

    return [p for p in paths if os.path.isdir(p)]


def _collect_incremental_logs(
    paths: list[str],
    max_files: int = 10,
    max_bytes_per_file: int = 16384,
) -> str:
    """
    Read newly appended content from log files inside `paths`.
    Uses `_LOG_WATERMARKS` so already-processed log segments are not re-ingested.
    """
    candidate_files: list[tuple[int, Path]] = []

    for raw_path in paths:
        p = Path(raw_path).expanduser()
        if not p.exists():
            continue
        if p.is_file():
            try:
                candidate_files.append((p.stat().st_mtime_ns, p))
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
                            candidate_files.append((child.stat().st_mtime_ns, child))
                        except OSError:
                            continue
            except OSError:
                continue

    if not candidate_files:
        return ""

    # Process most recently modified files first
    candidate_files.sort(key=lambda item: item[0], reverse=True)
    chunks: list[str] = []

    for mtime_ns, file_path in candidate_files[:max_files]:
        key = str(file_path)
        try:
            size = file_path.stat().st_size
            prev_mtime, prev_offset = _LOG_WATERMARKS.get(key, (0, 0))
            if mtime_ns == prev_mtime and size == prev_offset:
                continue

            start_offset = prev_offset if size >= prev_offset else 0
            if size - start_offset > max_bytes_per_file:
                start_offset = max(0, size - max_bytes_per_file)

            with open(file_path, "rb") as f:
                f.seek(start_offset)
                raw_bytes = f.read(max_bytes_per_file)

            _LOG_WATERMARKS[key] = (mtime_ns, size)
            text = raw_bytes.decode("utf-8", errors="ignore").strip()
            if text:
                chunks.append(text)
        except OSError:
            continue

    return "\n\n".join(chunks)


def background_sync_worker() -> None:
    """Periodically runs passive capture on newly appended agent logs."""
    from .passive import extract_memories_from_log

    # Warm up the embedding model immediately on background thread start
    warmup_model()
    time.sleep(10)

    while True:
        try:
            log_dirs = detect_agent_logs()
            if log_dirs:
                log_text = _collect_incremental_logs(log_dirs)
                if log_text:
                    with contextlib.redirect_stdout(sys.stderr):
                        extract_memories_from_log(
                            log_text,
                            namespace=NAMESPACE,
                            agent_id="auto-sync-worker",
                            force_local=True,
                        )
        except Exception:
            pass
        time.sleep(120)


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="mesh_learn",
            description=(
                "Store information in shared agent memory. "
                "Automatically deduplicates identical facts and checks for contradictions "
                "with existing memories. If a conflicting memory is found, the new memory "
                "is still stored and a conflict warning is returned for review. "
                "Use ttl_days for time-sensitive facts (e.g. staging IPs, sprint goals)."
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
                        "enum": sorted(VALID_MEMORY_TYPES),
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
                            "Set true to mark private and exclude from exports/sync. "
                            "Use for credentials, secrets, or local paths."
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
                "Search shared agent memory using 3-stage hybrid search "
                "(semantic meaning + BM25 keywords + exact identifiers). "
                "Call this at the start of tasks to check what other agents have already discovered."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What you want to find (natural language or exact keywords).",
                    },
                    "n": {
                        "type": "integer",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 50,
                        "description": "Maximum number of results to return.",
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
                    "memory_type": {
                        "type": "string",
                        "enum": sorted(VALID_MEMORY_TYPES),
                        "description": "Optional filter to restrict results to a single memory_type.",
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
                "creation timestamp, or access count. Only provide the fields you want to change."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "string",
                        "description": "UUID of the memory to update.",
                    },
                    "content": {
                        "type": "string",
                        "description": "New text content (re-embeds automatically if changed).",
                    },
                    "memory_type": {
                        "type": "string",
                        "enum": sorted(VALID_MEMORY_TYPES),
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
                        "description": "Updated TTL in days (resets decay timer).",
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
            description="Delete a specific memory permanently by its ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "string",
                        "description": "The UUID of the memory to delete.",
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
            description="List memories in the namespace sorted by most recently accessed.",
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
                        "enum": sorted(VALID_MEMORY_TYPES),
                        "description": "Optional filter by memory type.",
                    },
                    "tag": {
                        "type": "string",
                        "description": "Optional filter by a single tag.",
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
                "an agent session. Optionally focus it on a topic query."
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
                        "description": "UUID of the contradiction record to mark resolved.",
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
                "Store a session turn summary and automatically capture concrete facts "
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


def _execute_tool_sync(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Execute a tool synchronously with stdout redirected to stderr for MCP stdio safety."""
    ns_override = arguments.get("namespace")
    mesh = get_mesh(namespace=ns_override)

    with contextlib.redirect_stdout(sys.stderr):
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
            log_learn(
                content=arguments["content"],
                namespace=mesh.namespace,
                agent_id=mesh.agent_id,
                result=result,
            )
            return response

        elif name == "mesh_recall":
            memories = mesh.recall(
                query=arguments["query"],
                n=int(arguments.get("n", 5)),
                min_similarity=float(arguments.get("min_similarity", 0.0)),
                min_confidence=float(arguments.get("min_confidence", 0.0)),
                include_stale=bool(arguments.get("include_stale", True)),
                memory_type=arguments.get("memory_type"),
                tags=arguments.get("tags"),
                source_agent=arguments.get("source_agent"),
            )
            log_recall(
                query=arguments["query"],
                namespace=mesh.namespace,
                agent_id=mesh.agent_id,
                results=memories,
            )
            stale_count = sum(1 for m in memories if m.get("is_stale"))
            return {
                "query": arguments["query"],
                "namespace": mesh.namespace,
                "count": len(memories),
                "stale_count": stale_count,
                "results": memories,
            }

        elif name == "mesh_update":
            updated = mesh.update(
                memory_id=arguments["memory_id"],
                content=arguments.get("content"),
                memory_type=arguments.get("memory_type"),
                confidence=arguments.get("confidence"),
                tags=arguments.get("tags"),
                ttl_days=arguments.get("ttl_days"),
                local_only=arguments.get("local_only"),
            )
            log_update(
                memory_id=arguments["memory_id"],
                content=arguments.get("content"),
                namespace=mesh.namespace,
                agent_id=mesh.agent_id,
            )
            return {
                "status": "updated",
                "memory_id": arguments["memory_id"],
                "namespace": mesh.namespace,
                "memory": updated["memory"],
            }

        elif name == "mesh_forget":
            deleted = mesh.forget(arguments["memory_id"])
            log_forget(
                memory_id=arguments["memory_id"],
                namespace=mesh.namespace,
                agent_id=mesh.agent_id,
            )
            return {
                "status": "deleted" if deleted else "not_found",
                "memory_id": arguments["memory_id"],
                "remaining": mesh.count(),
            }

        elif name == "mesh_inspect":
            memories = mesh.inspect(
                limit=int(arguments.get("limit", 10)),
                memory_type=arguments.get("memory_type"),
                tag=arguments.get("tag"),
            )
            return {
                "namespace": mesh.namespace,
                "total": mesh.count(),
                "memories": memories,
            }

        elif name == "mesh_context":
            ctx = build_context(
                query=arguments.get("query"),
                namespace=mesh.namespace,
                count=int(arguments.get("count", 10)),
                format=arguments.get("format", "markdown"),
                min_confidence=float(arguments.get("min_confidence", 0.3)),
                agent_id=mesh.agent_id,
            )
            return {
                "namespace": mesh.namespace,
                "format": arguments.get("format", "markdown"),
                "context": ctx,
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
            from .passive import extract_memories_from_log

            summary_text = (arguments.get("summary") or "").strip()
            if summary_text:
                res = mesh.learn(
                    content=summary_text,
                    memory_type="result",
                    tags=["session-sync"],
                )
                log_learn(
                    content=summary_text,
                    namespace=mesh.namespace,
                    agent_id=mesh.agent_id,
                    result=res,
                )

            log_dirs = detect_agent_logs()
            log_text = _collect_incremental_logs(log_dirs) if log_dirs else ""
            captured_count = 0
            if log_text:
                capture_res = extract_memories_from_log(
                    log_text,
                    namespace=mesh.namespace,
                    agent_id=mesh.agent_id,
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
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


def run() -> None:
    """Entry point for: mesh-server"""
    threading.Thread(target=background_sync_worker, daemon=True).start()
    asyncio.run(_run())


if __name__ == "__main__":
    run()
"""
Mesh MCP Server v0.2

Tools exposed:
  mesh_learn                — store a memory (with contradiction detection)
  mesh_recall               — retrieve memories (with decay info)
  mesh_forget               — delete a memory
  mesh_inspect              — list all memories
  mesh_patterns             — surface usage patterns and gaps
  mesh_resolve_contradiction — mark a conflict as resolved
"""

import asyncio
import json
import os
import time
import threading
import glob
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

from .memory import Mesh, VALID_MEMORY_TYPES
from .audit import log_recall, log_learn

NAMESPACE = os.environ.get("MESH_NAMESPACE", "shared")
AGENT_ID = os.environ.get("MESH_AGENT_ID", "mcp-agent")

app = Server("mesh")
_mesh: Mesh | None = None


def get_mesh() -> Mesh:
    global _mesh
    if _mesh is None:
        _mesh = Mesh(namespace=NAMESPACE, agent_id=AGENT_ID)
    return _mesh


def detect_agent_logs() -> list[str]:
    """Auto-detect log directories for various AI agents."""
    paths = []
    home = str(Path.home())
    
    # Antigravity logs
    antigravity_base = os.path.join(home, ".gemini", "antigravity", "brain")
    if os.path.exists(antigravity_base):
        # Find all session log folders
        paths.extend(glob.glob(os.path.join(antigravity_base, "*", ".system_generated", "logs")))
    
    # Claude Desktop logs (MacOS/Windows)
    if os.name == "nt": # Windows
        claude_logs = os.path.join(os.environ.get("APPDATA", ""), "Claude", "logs")
    else: # Mac
        claude_logs = os.path.join(home, "Library", "Application Support", "Claude", "logs")
    
    if os.path.exists(claude_logs):
        paths.append(claude_logs)
        
    return [p for p in paths if os.path.isdir(p)]


def background_sync_worker():
    """Periodically runs passive capture on detected logs."""
    from .passive import extract_memories_from_log
    
    # Give the server time to start up
    time.sleep(10)
    
    while True:
        try:
            log_dirs = detect_agent_logs()
            for log_dir in log_dirs:
                extract_memories_from_log(log_dir, namespace=NAMESPACE, agent_id="auto-sync-worker")
        except Exception:
            pass
        # Sync every 2 minutes
        time.sleep(120)


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="mesh_learn",
            description=(
                "Store information in shared agent memory. "
                "Automatically checks for contradictions with existing memories. "
                "If a conflicting memory is found, the new memory is still stored "
                "but a conflict warning is returned for you to review. "
                "Use ttl_days for information that will go stale (e.g. IP addresses, "
                "API keys, temporary configs)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": (
                            "The text to remember. Be specific. "
                            "Good: 'The user prefers tabs over spaces in Python.' "
                            "Bad: 'User has a preference.'"
                        )
                    },
                    "memory_type": {
                        "type": "string",
                        "enum": list(VALID_MEMORY_TYPES),
                        "default": "fact",
                        "description": (
                            "fact: objective information. "
                            "preference: user or system preferences. "
                            "context: situational background. "
                            "result: outcome of a task. "
                            "instruction: rules to follow."
                        )
                    },
                    "confidence": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                        "default": 1.0,
                        "description": "How confident you are. Use <0.7 for uncertain info."
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional labels, e.g. ['api', 'auth']"
                    },
                    "ttl_days": {
                        "type": "number",
                        "description": (
                            "Optional. If set, this memory's confidence decays to 0 "
                            "over this many days. Use for time-sensitive facts. "
                            "Example: 30 for a staging server IP, 7 for a sprint goal."
                        )
                    },
                    "local_only": {
                        "type": "boolean",
                        "description": "Set to true to mark this memory as private and exclude it from any future cloud sync. Use for API keys, credentials, or sensitive information.",
                        "default": False
                    }
                },
                "required": ["content"]
            }
        ),
        types.Tool(
            name="mesh_recall",
            description=(
                "Search shared agent memory by meaning. "
                "Results include effective_confidence (after decay) and is_stale flag. "
                "Treat is_stale=true results with caution — they may be outdated. "
                "Call this at the start of tasks to check what's already known."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What you want to know, in natural language."
                    },
                    "n": {
                        "type": "integer",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 20
                    },
                    "min_similarity": {
                        "type": "number",
                        "default": 0.0,
                        "minimum": 0.0,
                        "maximum": 1.0
                    },
                    "include_stale": {
                        "type": "boolean",
                        "default": True,
                        "description": "Set false to exclude decayed memories from results."
                    }
                },
                "required": ["query"]
            }
        ),
        types.Tool(
            name="mesh_forget",
            description="Delete a specific memory by its ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string"}
                },
                "required": ["memory_id"]
            }
        ),
        types.Tool(
            name="mesh_inspect",
            description="List all memories in the current namespace.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "default": 10,
                        "minimum": 1,
                        "maximum": 100
                    }
                }
            }
        ),
        types.Tool(
            name="mesh_patterns",
            description=(
                "Analyse memory usage patterns across all agents. "
                "Returns: top_topics (what agents recall most), "
                "memory_gaps (topics frequently searched but poorly answered — "
                "these are what you should document next), "
                "unused_memories (candidates for deletion), "
                "stale_memories (decayed below 30 percent confidence)."
            ),
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        types.Tool(
            name="mesh_sync",
            description=(
                "Automatically captures memories from the current session logs and syncs them to the namespace. "
                "Run this at the end of every turn to ensure no information is lost."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "A brief summary of the work done in this turn."
                    }
                }
            }
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    mesh = get_mesh()

    try:
        if name == "mesh_learn":
            result = mesh.learn(
                content=arguments["content"],
                memory_type=arguments.get("memory_type", "fact"),
                confidence=float(arguments.get("confidence", 1.0)),
                tags=arguments.get("tags", []),
                ttl_days=arguments.get("ttl_days"),
                local_only=bool(arguments.get("local_only", False)),
            )
            response = {
                "status": result["status"],
                "memory_id": result["memory_id"],
                "namespace": mesh.namespace,
                "total_memories": mesh.count(),
            }
            if result["conflict"]:
                response["conflict_warning"] = (
                    "A potentially contradictory memory already exists. "
                    "Review and delete the old one if this new memory supersedes it."
                )
                response["conflict"] = result["conflict"]
            log_learn(content=arguments["content"], namespace=mesh.namespace, agent_id=AGENT_ID, result=result)
            return [types.TextContent(type="text", text=json.dumps(response, indent=2))]

        elif name == "mesh_recall":
            memories = mesh.recall(
                query=arguments["query"],
                n=int(arguments.get("n", 5)),
                min_similarity=float(arguments.get("min_similarity", 0.0)),
                include_stale=bool(arguments.get("include_stale", True)),
            )
            log_recall(query=arguments["query"], namespace=mesh.namespace, agent_id=AGENT_ID, results=memories)
            stale_count = sum(1 for m in memories if m.get("is_stale"))
            return [types.TextContent(
                type="text",
                text=json.dumps({
                    "query": arguments["query"],
                    "namespace": mesh.namespace,
                    "count": len(memories),
                    "stale_count": stale_count,
                    "results": memories
                }, indent=2)
            )]

        elif name == "mesh_forget":
            mesh.forget(arguments["memory_id"])
            return [types.TextContent(
                type="text",
                text=json.dumps({
                    "status": "deleted",
                    "memory_id": arguments["memory_id"],
                    "remaining": mesh.count()
                }, indent=2)
            )]

        elif name == "mesh_inspect":
            memories = mesh.inspect(limit=int(arguments.get("limit", 10)))
            return [types.TextContent(
                type="text",
                text=json.dumps({
                    "namespace": mesh.namespace,
                    "total": mesh.count(),
                    "memories": memories
                }, indent=2)
            )]

        elif name == "mesh_patterns":
            patterns = mesh.patterns()
            return [types.TextContent(
                type="text",
                text=json.dumps({
                    "namespace": mesh.namespace,
                    **patterns
                }, indent=2)
            )]

        elif name == "mesh_sync":
            from .passive import extract_memories_from_log
            
            # 1. Store the manual summary if provided
            if "summary" in arguments:
                mesh.learn(content=arguments["summary"], memory_type="result", tags=["session-sync"])
            
            # 2. Trigger passive capture from logs
            # We assume the log path is consistent for Antigravity
            log_dir = os.path.expanduser("~/.gemini/antigravity/brain")
            memories = extract_memories_from_log(log_dir, namespace=mesh.namespace)
            
            return [types.TextContent(
                type="text",
                text=json.dumps({
                    "status": "synchronized",
                    "manual_summary": arguments.get("summary", "none"),
                    "passive_memories_captured": len(memories),
                    "total_memories": mesh.count()
                }, indent=2)
            )]

        elif name == "mesh_resolve_contradiction":
            mesh.resolve_contradiction(arguments["contradiction_id"])
            return [types.TextContent(
                type="text",
                text=json.dumps({
                    "status": "resolved",
                    "contradiction_id": arguments["contradiction_id"]
                }, indent=2)
            )]

        else:
            return [types.TextContent(
                type="text",
                text=json.dumps({"error": f"Unknown tool: {name}"})
            )]

    except Exception as e:
        return [types.TextContent(
            type="text",
            text=json.dumps({"error": str(e), "tool": name})
        )]


async def _run():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options()
        )


def run():
    # Start the background sync thread
    threading.Thread(target=background_sync_worker, daemon=True).start()
    
    asyncio.run(_run())


if __name__ == "__main__":
    run()

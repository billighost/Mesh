"""
mesh/http_server.py — High-speed HTTP REST API & Background Daemon Server for Mesh.
Runs on localhost:7701 by default. Separate from the dashboard (port 7433).
Any tool, script, or MCP stdio proxy can use Mesh through this server.
"""
from __future__ import annotations

import argparse
import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .audit import get_audit_log, log_forget, log_learn, log_recall, log_update
from .context_builder import _format
from .memory import (
    Mesh,
    ensure_model_downloaded,
    get_engine_status,
    set_idle_unload_seconds,
    unload_model,
    warmup_model,
)

app = FastAPI(
    title="Mesh HTTP API",
    description="Local HTTP interface for the Mesh shared agent memory layer.",
    version="0.6.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_MESH_LOCK = threading.RLock()
_MESH_REGISTRY: dict[tuple[str, str], Mesh] = {}


def get_mesh(namespace: Optional[str] = None, agent_id: Optional[str] = None) -> Mesh:
    """Return a cached Mesh instance for the given namespace and agent_id."""
    ns = (namespace or os.environ.get("MESH_NAMESPACE", "shared")).strip() or "shared"
    aid = (agent_id or os.environ.get("MESH_AGENT_ID", "http-client")).strip() or "http-client"
    key = (ns, aid)
    mesh = _MESH_REGISTRY.get(key)
    if mesh is not None:
        return mesh
    with _MESH_LOCK:
        mesh = _MESH_REGISTRY.get(key)
        if mesh is None:
            mesh = Mesh(namespace=ns, agent_id=aid)
            _MESH_REGISTRY[key] = mesh
        return mesh


# --- Request Models ---


class LearnRequest(BaseModel):
    content: str
    memory_type: Optional[str] = "fact"
    confidence: Optional[float] = 1.0
    tags: Optional[list[str]] = Field(default_factory=list)
    local_only: Optional[bool] = False
    ttl_days: Optional[float] = None
    namespace: Optional[str] = None
    agent_id: Optional[str] = None


class RecallRequest(BaseModel):
    query: str
    count: Optional[int] = 5
    n: Optional[int] = None
    memory_type: Optional[str] = None
    tags: Optional[list[str]] = None
    source_agent: Optional[str] = None
    min_similarity: Optional[float] = 0.0
    min_confidence: Optional[float] = 0.0
    include_stale: Optional[bool] = True
    namespace: Optional[str] = None
    agent_id: Optional[str] = None


class UpdateRequest(BaseModel):
    memory_id: str
    content: Optional[str] = None
    memory_type: Optional[str] = None
    confidence: Optional[float] = None
    tags: Optional[list[str]] = None
    local_only: Optional[bool] = None
    ttl_days: Optional[float] = None
    namespace: Optional[str] = None
    agent_id: Optional[str] = None


class ForgetRequest(BaseModel):
    memory_id: str
    namespace: Optional[str] = None
    agent_id: Optional[str] = None


class ResolveContradictionRequest(BaseModel):
    contradiction_id: str
    namespace: Optional[str] = None


class ContextRequest(BaseModel):
    query: Optional[str] = None
    count: Optional[int] = 10
    max_memories: Optional[int] = None
    n: Optional[int] = None
    namespace: Optional[str] = None
    format: Optional[str] = "markdown"  # "markdown" | "plain" | "xml" | "json"
    min_confidence: Optional[float] = 0.3
    memory_type: Optional[str] = None
    tags: Optional[list[str]] = None


class McpProxyRequest(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    default_namespace: Optional[str] = None
    agent_id: Optional[str] = None


# --- Endpoints ---


@app.get("/health")
async def health() -> dict[str, Any]:
    """Health check. Returns ok if the server is running."""
    return {
        "status": "ok",
        "version": "0.4",
        "pid": os.getpid(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "engine": get_engine_status(),
    }


@app.get("/engine")
async def engine_status() -> dict[str, Any]:
    """Return current embedding engine and idle-unload status."""
    return {"status": "ok", "engine": get_engine_status()}


@app.post("/unload")
async def unload_engine() -> dict[str, Any]:
    """Explicitly unload the neural embedding model from RAM to free memory immediately."""
    unloaded = unload_model()
    return {"status": "ok", "unloaded": unloaded, "engine": get_engine_status()}


@app.post("/mcp")
async def mcp_proxy(req: McpProxyRequest) -> dict[str, Any]:
    """Execute an MCP tool request in the shared daemon process."""
    from .mcp_server import _execute_tool_sync

    payload = _execute_tool_sync(
        name=req.name,
        arguments=req.arguments,
        default_namespace=req.default_namespace,
        agent_id=req.agent_id,
    )
    return {"status": "ok", "payload": payload}


@app.post("/learn")
async def learn(req: LearnRequest) -> dict[str, Any]:
    """Store a memory. Returns the memory ID and any contradiction warnings."""
    try:
        mesh = get_mesh(req.namespace, req.agent_id)
        result = mesh.learn(
            content=req.content,
            memory_type=req.memory_type or "fact",
            confidence=req.confidence if req.confidence is not None else 1.0,
            tags=req.tags or [],
            local_only=bool(req.local_only),
            ttl_days=req.ttl_days,
        )
        log_learn(
            content=req.content,
            namespace=mesh.namespace,
            agent_id=mesh.agent_id,
            result=result,
        )
        return {"status": "ok", "result": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/recall")
async def recall(req: RecallRequest) -> dict[str, Any]:
    """Retrieve memories relevant to a query using hybrid search."""
    try:
        mesh = get_mesh(req.namespace, req.agent_id)
        limit_n = req.n if req.n is not None else (req.count or 5)
        results = mesh.recall(
            query=req.query,
            n=limit_n,
            min_similarity=req.min_similarity or 0.0,
            min_confidence=req.min_confidence or 0.0,
            include_stale=True if req.include_stale is None else req.include_stale,
            memory_type=req.memory_type,
            tags=req.tags,
            source_agent=req.source_agent,
        )
        log_recall(
            query=req.query,
            namespace=mesh.namespace,
            agent_id=mesh.agent_id,
            results=results,
        )
        return {"status": "ok", "results": results, "count": len(results)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/update")
async def update(req: UpdateRequest) -> dict[str, Any]:
    """Update an existing memory in-place by ID."""
    try:
        mesh = get_mesh(req.namespace, req.agent_id)
        result = mesh.update(
            memory_id=req.memory_id,
            content=req.content,
            memory_type=req.memory_type,
            confidence=req.confidence,
            tags=req.tags,
            ttl_days=req.ttl_days,
            local_only=req.local_only,
        )
        log_update(
            memory_id=req.memory_id,
            content=req.content,
            namespace=mesh.namespace,
            agent_id=mesh.agent_id,
        )
        return {"status": "ok", "result": result}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/forget")
async def forget(req: ForgetRequest) -> dict[str, Any]:
    """Delete a memory by ID."""
    try:
        mesh = get_mesh(req.namespace, req.agent_id)
        deleted = mesh.forget(req.memory_id)
        if deleted:
            log_forget(
                memory_id=req.memory_id,
                namespace=mesh.namespace,
                agent_id=mesh.agent_id,
            )
        return {
            "status": "ok" if deleted else "not_found",
            "deleted": req.memory_id if deleted else None,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/inspect")
async def inspect(
    namespace: Optional[str] = None,
    limit: int = 50,
    memory_type: Optional[str] = None,
    tag: Optional[str] = None,
) -> dict[str, Any]:
    """List memories in the namespace."""
    try:
        mesh = get_mesh(namespace)
        result = mesh.inspect(limit=limit, memory_type=memory_type, tag=tag)
        return {"status": "ok", "memories": result, "count": len(result)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/patterns")
async def patterns(namespace: Optional[str] = None) -> dict[str, Any]:
    """Return pattern analysis: top queries, memory gaps, unused and stale memories."""
    try:
        mesh = get_mesh(namespace)
        result = mesh.patterns()
        return {"status": "ok", "patterns": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/contradictions")
async def contradictions(
    namespace: Optional[str] = None,
    resolved: bool = False,
) -> dict[str, Any]:
    """Return unresolved (or resolved) contradictions in the namespace."""
    try:
        mesh = get_mesh(namespace)
        items = mesh.contradictions(resolved=resolved)
        return {"status": "ok", "contradictions": items, "count": len(items)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/contradictions/resolve")
async def resolve_contradiction(req: ResolveContradictionRequest) -> dict[str, Any]:
    """Mark a contradiction as resolved."""
    try:
        mesh = get_mesh(req.namespace)
        mesh.resolve_contradiction(req.contradiction_id)
        return {"status": "ok", "resolved": req.contradiction_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/context")
async def context(req: ContextRequest) -> dict[str, Any]:
    """
    Return memories formatted as a system prompt block in a single retrieval pass.
    Used by mesh-context CLI command and direct integrations.
    """
    try:
        mesh = get_mesh(req.namespace, "http-context")
        raw_limit = (
            req.max_memories
            if req.max_memories is not None
            else (req.n if req.n is not None else (req.count or 10))
        )
        count = max(1, int(raw_limit))
        min_conf = req.min_confidence if req.min_confidence is not None else 0.3
        fmt = req.format or "markdown"

        if req.query:
            memories = mesh.recall(
                query=req.query,
                n=count,
                min_confidence=min_conf,
                memory_type=req.memory_type,
                tags=req.tags,
            )
        else:
            raw_mems = mesh.inspect(limit=max(count * 3, 30), memory_type=req.memory_type)
            filtered = [
                m
                for m in raw_mems
                if float(m.get("effective_confidence", m.get("confidence", 1.0))) >= min_conf
            ]
            if req.tags:
                tag_set = {str(t).lower() for t in req.tags}
                filtered = [
                    m
                    for m in filtered
                    if tag_set.issubset({str(t).lower() for t in m.get("tags_list", [])})
                ]
            filtered.sort(
                key=lambda x: (
                    int(x.get("access_count", 0)),
                    float(x.get("effective_confidence", x.get("confidence", 1.0))),
                ),
                reverse=True,
            )
            memories = filtered[:count]

        formatted = _format(memories, fmt, mesh.namespace) if memories else ""
        return {"status": "ok", "context": formatted, "memory_count": len(memories)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/audit")
async def audit(
    namespace: Optional[str] = None,
    limit: int = 50,
    action: str = "all",
) -> dict[str, Any]:
    """Return the audit trail: recent memory reads and writes."""
    ns = (namespace or os.environ.get("MESH_NAMESPACE", "shared")).strip() or "shared"
    entries = get_audit_log(namespace=ns, limit=limit, action_filter=action)
    return {"status": "ok", "entries": entries}


def start() -> None:
    """Entry point for: mesh-http"""
    parser = argparse.ArgumentParser(description="Start the Mesh HTTP API server.")
    parser.add_argument(
        "--host",
        default=os.environ.get("MESH_HTTP_HOST", "127.0.0.1"),
        help="Host interface to bind (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MESH_HTTP_PORT", 7701)),
        help="Port to listen on (default: 7701)",
    )
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=float(os.environ.get("MESH_IDLE_UNLOAD_SECONDS", "180")),
        help="Seconds of inactivity before unloading the neural model from RAM (0 = never unload)",
    )
    parser.add_argument(
        "--no-warmup",
        action="store_true",
        help="Start in lazy mode without loading the neural model until the first request arrives",
    )
    args, _ = parser.parse_known_args()

    set_idle_unload_seconds(args.idle_timeout)
    ensure_model_downloaded(blocking=False)
    if not args.no_warmup:
        threading.Thread(target=warmup_model, daemon=True).start()

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    start()
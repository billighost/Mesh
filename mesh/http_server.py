"""
mesh/http_server.py — High-speed HTTP REST API for Mesh.
Runs on localhost:7701 by default. Separate from the dashboard (port 7433).
Any tool or script that can make HTTP requests can use Mesh through this server.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .audit import get_audit_log, log_forget, log_learn, log_recall, log_update
from .context_builder import _format, build_context
from .memory import Mesh, warmup_model

app = FastAPI(
    title="Mesh HTTP API",
    description="Local HTTP interface for the Mesh shared agent memory layer.",
    version="0.4",
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
    namespace: Optional[str] = None
    format: Optional[str] = "markdown"  # "markdown" | "plain" | "xml" | "json"
    min_confidence: Optional[float] = 0.3
    memory_type: Optional[str] = None
    tags: Optional[list[str]] = None


# --- Endpoints ---


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check. Returns ok if the server is running."""
    return {
        "status": "ok",
        "version": "0.4",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


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
        results = mesh.recall(
            query=req.query,
            n=req.count or 5,
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
        mesh.forget(req.memory_id)
        log_forget(
            memory_id=req.memory_id,
            namespace=mesh.namespace,
            agent_id=mesh.agent_id,
        )
        return {"status": "ok", "deleted": req.memory_id}
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
        count = req.count or 10
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
    threading.Thread(target=warmup_model, daemon=True).start()
    port = int(os.environ.get("MESH_HTTP_PORT", 7701))
    uvicorn.run("mesh.http_server:app", host="127.0.0.1", port=port, reload=False)


if __name__ == "__main__":
    start()
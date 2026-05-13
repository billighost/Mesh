"""
mesh/http_server.py — HTTP REST API for Mesh.
Runs on localhost:7701. Separate from the dashboard (port 7700).
Any tool that can POST to localhost can use Mesh through this server.
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import uvicorn
import os
import json
from datetime import datetime, timezone

from .memory import Mesh
from .store import MeshStore
from .audit import log_recall, log_learn

app = FastAPI(
    title="Mesh HTTP API",
    description="Local HTTP interface for the Mesh shared memory layer.",
    version="0.4"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_mesh(namespace: str = None, agent_id: str = None) -> Mesh:
    ns = namespace or os.environ.get("MESH_NAMESPACE", "shared")
    aid = agent_id or os.environ.get("MESH_AGENT_ID", "http-client")
    return Mesh(namespace=ns, agent_id=aid)


# --- Request/Response Models ---

class LearnRequest(BaseModel):
    content: str
    memory_type: Optional[str] = "fact"
    confidence: Optional[float] = 1.0
    tags: Optional[list[str]] = []
    local_only: Optional[bool] = False
    ttl_days: Optional[int] = None
    namespace: Optional[str] = None
    agent_id: Optional[str] = None

class RecallRequest(BaseModel):
    query: str
    count: Optional[int] = 5
    memory_type: Optional[str] = None
    min_confidence: Optional[float] = 0.0
    namespace: Optional[str] = None
    agent_id: Optional[str] = None

class ForgetRequest(BaseModel):
    memory_id: str
    namespace: Optional[str] = None

class ContextRequest(BaseModel):
    query: Optional[str] = None
    count: Optional[int] = 10
    namespace: Optional[str] = None
    format: Optional[str] = "markdown"  # "markdown" | "plain" | "json"


# --- Endpoints ---

@app.get("/health")
async def health():
    """Health check. Returns ok if the server is running."""
    return {
        "status": "ok",
        "version": "0.4",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@app.post("/learn")
async def learn(req: LearnRequest):
    """Store a memory. Returns the memory ID and any contradiction warnings."""
    try:
        mesh = get_mesh(req.namespace, req.agent_id)
        result = mesh.learn(
            content=req.content,
            memory_type=req.memory_type,
            confidence=req.confidence,
            tags=req.tags,
            local_only=req.local_only,
            ttl_days=req.ttl_days
        )
        log_learn(
            content=req.content,
            namespace=req.namespace or os.environ.get("MESH_NAMESPACE", "shared"),
            agent_id=req.agent_id or os.environ.get("MESH_AGENT_ID", "http-client"),
            result=result
        )
        return {"status": "ok", "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/recall")
async def recall(req: RecallRequest):
    """Retrieve memories relevant to a query. Returns ranked list with similarity scores."""
    try:
        mesh = get_mesh(req.namespace, req.agent_id)
        results = mesh.recall(
            query=req.query,
            n=req.count,
            min_confidence=req.min_confidence
        )
        log_recall(
            query=req.query,
            namespace=req.namespace or os.environ.get("MESH_NAMESPACE", "shared"),
            agent_id=req.agent_id or os.environ.get("MESH_AGENT_ID", "http-client"),
            results=results
        )
        return {"status": "ok", "results": results, "count": len(results)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/forget")
async def forget(req: ForgetRequest):
    """Delete a memory by ID."""
    try:
        mesh = get_mesh(req.namespace)
        mesh.forget(req.memory_id)
        return {"status": "ok", "deleted": req.memory_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/inspect")
async def inspect(namespace: Optional[str] = None, limit: int = 50):
    """List all memories in the namespace."""
    try:
        mesh = get_mesh(namespace)
        result = mesh.inspect(limit=limit)
        return {"status": "ok", "memories": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/patterns")
async def patterns(namespace: Optional[str] = None):
    """Return pattern analysis: top queries, memory gaps, unused memories."""
    try:
        mesh = get_mesh(namespace)
        result = mesh.patterns()
        return {"status": "ok", "patterns": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/context")
async def context(req: ContextRequest):
    """
    Return memories formatted as a system prompt block.
    Used by mesh-context CLI command and direct integrations.
    """
    try:
        mesh = get_mesh(req.namespace)
        memories = mesh.recall(
            query=req.query or "general project context",
            n=req.count
        )
        formatted = _format_context(memories, req.format)
        return {"status": "ok", "context": formatted, "memory_count": len(memories)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/audit")
async def audit(namespace: Optional[str] = None, limit: int = 50):
    """Return the audit trail: recent memory reads and writes."""
    from .audit import get_audit_log
    ns = namespace or os.environ.get("MESH_NAMESPACE", "shared")
    entries = get_audit_log(namespace=ns, limit=limit)
    return {"status": "ok", "entries": entries}


def _format_context(memories: list, format: str) -> str:
    if not memories:
        return ""
    
    if format == "json":
        return json.dumps(memories, indent=2)
    
    if format == "plain":
        lines = ["[MESH CONTEXT — Shared Agent Memory]", ""]
        for i, mem in enumerate(memories, 1):
            lines.append(f"{i}. {mem['content']}")
            if mem.get("tags"):
                lines.append(f"   tags: {', '.join(mem['tags'])}")
        return "\n".join(lines)
    
    # Default: markdown
    lines = [
        "## Mesh Context",
        "",
        "The following facts have been stored by agents working on this project.",
        "Use this context to avoid repeating work and to stay consistent with past decisions.",
        "",
    ]
    
    by_type = {}
    for mem in memories:
        t = mem.get("memory_type", "fact")
        by_type.setdefault(t, []).append(mem)
    
    for mem_type, mems in by_type.items():
        lines.append(f"### {mem_type.title()}")
        for mem in mems:
            conf = mem.get("effective_confidence", mem.get("confidence", 1.0))
            stale = " ⚠️ (possibly stale)" if mem.get("is_stale") else ""
            lines.append(f"- {mem['content']}{stale}")
        lines.append("")
    
    return "\n".join(lines)


def start():
    port = int(os.environ.get("MESH_HTTP_PORT", 7701))
    uvicorn.run("mesh.http_server:app", host="127.0.0.1", port=port, reload=False)


if __name__ == "__main__":
    start()

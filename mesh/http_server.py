r"""
mesh/http_server.py — High-speed HTTP REST API & Background Daemon Server for Mesh.
Runs on 127.0.0.1:7701 by default. Separate from the dashboard (port 7433).

Security hardening:
  - Strict localhost CORS with properly escaped IPv6 `\[::1\]` origin regex
  - Host & Origin header validation blocking DNS rebinding and cross-site attacks
  - Per-user 256-bit Bearer / X-Mesh-Token authentication (`~/.mesh/auth_token`) with no `testserver` bypass
  - Interactive `y/n` user permission prompts before starting background downloads or warmup workers
  - Non-blocking threadpool execution for synchronous SQLite & ONNX operations
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
import threading
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .audit import get_audit_log, validate_namespace
from .context_builder import build_context_bundle
from .daemon import get_or_create_auth_token, request_user_permission
from .memory import (
    Mesh,
    _has_local_onnx_pair,
    ensure_model_downloaded,
    get_engine_status,
    set_idle_unload_seconds,
    unload_model,
    warmup_model,
)
from .version import VERSION

_LOCAL_ORIGIN_REGEX = r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$"
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", "testserver"}
_ACTIVE_BIND_HOST = "127.0.0.1"


def _tokens_match(provided: str, expected: str) -> bool:
    if not provided or not expected:
        return False
    try:
        return secrets.compare_digest(
            provided.encode("utf-8", errors="surrogatepass"),
            expected.encode("utf-8", errors="surrogatepass"),
        )
    except Exception:
        return False


def _extract_hostname(host_header: str) -> str:
    h = (host_header or "").strip().lower()
    if not h:
        return ""
    if h.startswith("["):
        end = h.find("]")
        return h[: end + 1] if end != -1 else h
    return h.split(":")[0]


def verify_request_auth(request: Request) -> None:
    """
    Verify the local Mesh authentication token on protected endpoints.
    Can be overridden in unit tests via `app.dependency_overrides[verify_request_auth] = lambda: None`.
    """
    expected_token = get_or_create_auth_token()
    auth_header = request.headers.get("authorization", "")
    provided_token = ""
    if auth_header.lower().startswith("bearer "):
        provided_token = auth_header[7:].strip()
    if not provided_token:
        provided_token = request.headers.get("x-mesh-token", "").strip()

    if not provided_token or not _tokens_match(provided_token, expected_token):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: missing or invalid Mesh authentication token.",
        )


app = FastAPI(
    title="Mesh HTTP API",
    description="Local HTTP interface for the Mesh shared agent memory layer.",
    version=VERSION,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:7433",
        "http://localhost:7433",
        "http://127.0.0.1:7701",
        "http://localhost:7701",
    ],
    allow_origin_regex=_LOCAL_ORIGIN_REGEX,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Mesh-Token"],
)


@app.middleware("http")
async def _security_middleware(request: Request, call_next: Any) -> Any:
    """
    1. Validate `Host` header to block DNS rebinding attacks.
    2. Validate `Origin` header (if present) to block cross-site requests from arbitrary websites.
    3. Verify `~/.mesh/auth_token` on all endpoints except `/health` and `OPTIONS` preflights
       (unless overridden via `app.dependency_overrides[verify_request_auth]`).
    """
    configured_host = os.environ.get("MESH_HTTP_HOST", _ACTIVE_BIND_HOST).strip().lower()
    allowed_hosts = _ALLOWED_HOSTS | {configured_host, _ACTIVE_BIND_HOST}
    # If a unit test explicitly registered a dependency override on `verify_request_auth`,
    # allow FastAPI TestClient's default `testserver` Host header for the DNS-rebinding check.
    has_auth_override = verify_request_auth in app.dependency_overrides
    if has_auth_override:
        allowed_hosts = allowed_hosts | {"testserver"}

    req_host = _extract_hostname(request.headers.get("host", ""))
    if req_host and req_host not in allowed_hosts:
        return JSONResponse(
            status_code=403,
            content={"detail": f"Forbidden Host header '{req_host}' (DNS rebinding protection)."},
        )

    origin = request.headers.get("origin")
    if origin:
        parsed_origin = urlparse(origin)
        origin_host = (parsed_origin.hostname or "").lower()
        if origin_host not in allowed_hosts or not re.match(_LOCAL_ORIGIN_REGEX, origin):
            return JSONResponse(
                status_code=403,
                content={"detail": "Forbidden cross-origin request."},
            )

    if request.method == "OPTIONS" or request.url.path == "/health" or has_auth_override:
        return await call_next(request)

    try:
        verify_request_auth(request)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    return await call_next(request)


_MESH_LOCK = threading.RLock()
_MESH_REGISTRY: dict[tuple[str, str], Mesh] = {}


def evict_http_mesh_cache(namespace: Optional[str] = None) -> None:
    """Evict cached `Mesh` instances for `namespace` (or all namespaces if None)."""
    with _MESH_LOCK:
        if namespace is None:
            _MESH_REGISTRY.clear()
            return
        stale_keys = [k for k in _MESH_REGISTRY if k[0] == namespace]
        for k in stale_keys:
            _MESH_REGISTRY.pop(k, None)


def get_mesh(namespace: Optional[str] = None, agent_id: Optional[str] = None) -> Mesh:
    """Return a cached Mesh instance for the given namespace and agent_id."""
    raw_ns = (namespace or os.environ.get("MESH_NAMESPACE", "shared")).strip() or "shared"
    ns = validate_namespace(raw_ns)
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
    include_private: Optional[bool] = False
    include_other_possible: Optional[bool] = True
    max_other_possible: Optional[int] = 5
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
    clear_ttl: Optional[bool] = False
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
    include_private: Optional[bool] = False


class McpProxyRequest(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    default_namespace: Optional[str] = None
    agent_id: Optional[str] = None


# --- Endpoints (Synchronous `def` so FastAPI runs them in a worker threadpool) ---

@app.get("/health")
def health() -> dict[str, Any]:
    """Health check. Returns ok if the server is running."""
    return {
        "status": "ok",
        "version": VERSION,
        "pid": os.getpid(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "engine": get_engine_status(),
    }


@app.get("/engine", dependencies=[Depends(verify_request_auth)])
def engine_status() -> dict[str, Any]:
    """Return current embedding engine and idle-unload status."""
    return {"status": "ok", "engine": get_engine_status()}


@app.post("/unload", dependencies=[Depends(verify_request_auth)])
def unload_engine() -> dict[str, Any]:
    """Explicitly unload the neural embedding model from RAM to free memory immediately."""
    unloaded = unload_model()
    return {"status": "ok", "unloaded": unloaded, "engine": get_engine_status()}


@app.post("/mcp", dependencies=[Depends(verify_request_auth)])
def mcp_proxy(req: McpProxyRequest) -> dict[str, Any]:
    """Execute an MCP tool request in the shared daemon process with structured error handling."""
    from .mcp_server import _execute_tool_sync

    try:
        payload = _execute_tool_sync(
            name=req.name,
            arguments=req.arguments,
            default_namespace=req.default_namespace,
            agent_id=req.agent_id,
            allow_daemon_proxy=False,
        )
        return {"status": "ok", "payload": payload}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/learn", dependencies=[Depends(verify_request_auth)])
def learn(req: LearnRequest) -> dict[str, Any]:
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
        return {"status": "ok", "result": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/recall", dependencies=[Depends(verify_request_auth)])
def recall(req: RecallRequest) -> dict[str, Any]:
    """Retrieve memories relevant to a query using 5-stage hybrid search + peripheral snippets."""
    try:
        mesh = get_mesh(req.namespace, req.agent_id)
        limit_n = req.n if req.n is not None else (req.count or 5)
        max_other = (req.max_other_possible or 5) if req.include_other_possible is not False else 0
        inc_priv = False if req.include_private is None else bool(req.include_private)
        bundle = mesh.recall_with_context(
            query=req.query,
            n=limit_n,
            min_similarity=req.min_similarity or 0.0,
            min_confidence=req.min_confidence or 0.0,
            include_stale=True if req.include_stale is None else req.include_stale,
            memory_type=req.memory_type,
            tags=req.tags,
            source_agent=req.source_agent,
            max_other_possible=max_other,
            include_private=inc_priv,
        )
        return {
            "status": "ok",
            "results": bundle["results"],
            "count": bundle["count"],
            "total_in_namespace": bundle["total_in_namespace"],
            "other_possible_memories": bundle["other_possible_memories"],
            "filter_hint": bundle["filter_hint"],
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/update", dependencies=[Depends(verify_request_auth)])
def update(req: UpdateRequest) -> dict[str, Any]:
    """Update an existing memory in-place by ID (supports clearing TTL via `clear_ttl=True`)."""
    try:
        mesh = get_mesh(req.namespace, req.agent_id)
        result = mesh.update(
            memory_id=req.memory_id,
            content=req.content,
            memory_type=req.memory_type,
            confidence=req.confidence,
            tags=req.tags,
            ttl_days=req.ttl_days,
            clear_ttl=bool(req.clear_ttl),
            local_only=req.local_only,
        )
        return {"status": "ok", "result": result}
    except ValueError as e:
        msg = str(e)
        status_code = 404 if "not found" in msg.lower() else 400
        raise HTTPException(status_code=status_code, detail=msg)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/forget", dependencies=[Depends(verify_request_auth)])
def forget(req: ForgetRequest) -> dict[str, Any]:
    """Delete a memory by ID."""
    try:
        mesh = get_mesh(req.namespace, req.agent_id)
        deleted = mesh.forget(req.memory_id)
        return {
            "status": "ok" if deleted else "not_found",
            "deleted": req.memory_id if deleted else None,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/inspect", dependencies=[Depends(verify_request_auth)])
def inspect(
    namespace: Optional[str] = None,
    limit: int = 50,
    memory_type: Optional[str] = None,
    tag: Optional[str] = None,
    include_private: bool = False,
) -> dict[str, Any]:
    """List memories in the namespace (excludes `local_only` private memories by default)."""
    try:
        mesh = get_mesh(namespace)
        result = mesh.inspect(
            limit=limit,
            memory_type=memory_type,
            tag=tag,
            include_private=include_private,
        )
        return {"status": "ok", "memories": result, "count": len(result)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/patterns", dependencies=[Depends(verify_request_auth)])
def patterns(namespace: Optional[str] = None) -> dict[str, Any]:
    """Return pattern analysis: top queries, memory gaps, unused and stale memories."""
    try:
        mesh = get_mesh(namespace)
        result = mesh.patterns()
        return {"status": "ok", "patterns": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/contradictions", dependencies=[Depends(verify_request_auth)])
def contradictions(
    namespace: Optional[str] = None,
    resolved: bool = False,
) -> dict[str, Any]:
    """Return unresolved (or resolved) contradictions in the namespace."""
    try:
        mesh = get_mesh(namespace)
        items = mesh.contradictions(resolved=resolved)
        return {"status": "ok", "contradictions": items, "count": len(items)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/contradictions/resolve", dependencies=[Depends(verify_request_auth)])
def resolve_contradiction(req: ResolveContradictionRequest) -> dict[str, Any]:
    """Mark a contradiction as resolved."""
    try:
        mesh = get_mesh(req.namespace)
        mesh.resolve_contradiction(req.contradiction_id)
        return {"status": "ok", "resolved": req.contradiction_id}
    except ValueError as e:
        msg = str(e)
        status_code = 404 if "not found" in msg.lower() else 400
        raise HTTPException(status_code=status_code, detail=msg)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/context", dependencies=[Depends(verify_request_auth)])
def context(req: ContextRequest) -> dict[str, Any]:
    """
    Return memories formatted as a system prompt block using the unified `build_context_bundle()`.
    Excludes `local_only=True` private memories by default unless `include_private=True`.
    """
    try:
        mesh = get_mesh(req.namespace, "http-context")
        bundle = build_context_bundle(
            query=req.query,
            namespace=mesh.namespace,
            count=req.count or 10,
            max_memories=req.max_memories,
            n=req.n,
            format=req.format or "markdown",
            min_confidence=req.min_confidence if req.min_confidence is not None else 0.3,
            memory_type=req.memory_type,
            tags=req.tags,
            include_private=bool(req.include_private),
            mesh_instance=mesh,
        )
        return {
            "status": "ok",
            "context": bundle["context"],
            "memory_count": bundle["memory_count"],
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/audit", dependencies=[Depends(verify_request_auth)])
def audit(
    namespace: Optional[str] = None,
    limit: int = 50,
    action: str = "all",
) -> dict[str, Any]:
    """Return the audit trail: recent memory reads and writes."""
    try:
        ns = validate_namespace((namespace or os.environ.get("MESH_NAMESPACE", "shared")).strip() or "shared")
        entries = get_audit_log(namespace=ns, limit=limit, action_filter=action)
        return {"status": "ok", "entries": entries}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def start() -> None:
    """Entry point for: mesh-http"""
    global _ACTIVE_BIND_HOST
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
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip interactive y/n permission prompts",
    )
    args, _ = parser.parse_known_args()

    _ACTIVE_BIND_HOST = args.host.strip().lower()
    os.environ["MESH_HTTP_HOST"] = args.host.strip()
    if _ACTIVE_BIND_HOST in ("0.0.0.0", "::", "[::]"):
        sys.stderr.write(
            f"[WARNING] Binding Mesh HTTP server to '{args.host}' exposes the API to the local network.\n"
        )

    get_or_create_auth_token()
    set_idle_unload_seconds(args.idle_timeout)

    allow_auto_download = bool(args.yes or os.environ.get("MESH_ALLOW_DAEMON_DOWNLOAD") == "1")

    if not args.no_warmup:
        if _has_local_onnx_pair() is None:
            from .memory import _get_onnx_model_dir

            if allow_auto_download or request_user_permission(
                f"download the ~90MB ONNX embedding model into {_get_onnx_model_dir()} and warm the HTTP server",
                assume_yes=allow_auto_download,
            ):
                threading.Thread(
                    target=warmup_model,
                    kwargs={"assume_yes": True},
                    daemon=True,
                ).start()
        else:
            threading.Thread(
                target=warmup_model,
                kwargs={"assume_yes": False},
                daemon=True,
            ).start()

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    start()
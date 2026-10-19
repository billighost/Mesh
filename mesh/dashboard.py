"""
Mesh Dashboard — Minimal, high-speed web UI for managing shared agent memory.
Start: mesh-dashboard
       mesh-dashboard --namespace my-project --port 7433

Security hardening:
  - Host & Origin header verification blocking DNS rebinding and cross-site attacks
    (`testserver` is rejected by default and accepted only when a test auth override is active;
    non-loopback binds require `MESH_ALLOWED_HOSTS` to be explicitly set)
  - Launch-token & HttpOnly SameSite=Strict session cookie protection on `GET /` and `/api/*`
    (never hands out the auth token to unauthenticated requests)
  - Read-only endpoints open namespaces with `create=False` so browsing never creates empty `.db` files
  - Event-delegated `data-action` / `data-id` attributes eliminating inline `onclick` XSS vectors
  - Full support for clearing TTLs (`clear_ttl=True`) when editing memories
"""
from __future__ import annotations

import argparse
import hmac
import logging
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import webbrowser
from collections import OrderedDict
from typing import Any, List, Optional
from urllib.parse import urlparse

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from .audit import ensure_secure_mesh_dir, get_audit_log, validate_namespace
from .daemon import get_or_create_auth_token, request_user_permission
from .digest import generate_digest
from .io import export_namespace
from .memory import (
    Mesh,
    get_engine_status,
    suggest_did_you_mean_namespaces,
    warmup_model,
)
from .namespaces import _evict_namespace_caches, delete_namespace, list_namespaces, namespace_stats
from .store import NamespaceLimitExceededError, NamespaceNotFoundError
from .version import VERSION

logger = logging.getLogger(__name__)

_LOCAL_ORIGIN_REGEX = r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$"
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
_ACTIVE_BIND_HOST = "127.0.0.1"
_SESSION_COOKIE_NAME = "mesh_dash_session"

_MESH_LOCK = threading.RLock()
_MESH_CACHE_MAX = 64
_MESH_CACHE: OrderedDict[str, Mesh] = OrderedDict()
_default_namespace: str = "shared"
_mesh: Optional[Mesh] = None

_LAUNCH_NONCES_LOCK = threading.Lock()
_LAUNCH_NONCES_MAX = 64
_LAUNCH_NONCE_TTL_SECONDS = 60.0
_LAUNCH_NONCES: dict[str, float] = {}


def _resolve_default_namespace() -> str:
    """
    Resolve the default namespace with deterministic precedence:
      1. Explicitly configured `_default_namespace` (when set to a non-'shared' value by `run()` or caller)
      2. `MESH_NAMESPACE` environment variable
      3. `_default_namespace` fallback ('shared')
    Never derives the default from the mutable cached `_mesh` instance (Issue H2).
    """
    if _default_namespace and _default_namespace != "shared":
        return validate_namespace(_default_namespace)
    env_ns = (os.environ.get("MESH_NAMESPACE") or "").strip()
    if env_ns:
        return validate_namespace(env_ns)
    raw_default = (_default_namespace or "shared").strip() or "shared"
    return validate_namespace(raw_default)


def _resolve_namespace(namespace: Optional[str] = None) -> str:
    raw = (namespace or "").strip()
    if raw:
        return validate_namespace(raw)
    return _resolve_default_namespace()


def _register_launch_nonce(nonce: str, now: Optional[float] = None) -> None:
    ts = time.time() if now is None else float(now)
    with _LAUNCH_NONCES_LOCK:
        expired = [
            k for k, created_at in _LAUNCH_NONCES.items()
            if (ts - created_at) >= _LAUNCH_NONCE_TTL_SECONDS
        ]
        for k in expired:
            _LAUNCH_NONCES.pop(k, None)
        _LAUNCH_NONCES[nonce] = ts
        while len(_LAUNCH_NONCES) > _LAUNCH_NONCES_MAX:
            oldest_key = next(iter(_LAUNCH_NONCES))
            _LAUNCH_NONCES.pop(oldest_key, None)


def _consume_launch_nonce(code: str, now: Optional[float] = None) -> bool:
    if not code:
        return False
    ts = time.time() if now is None else float(now)
    with _LAUNCH_NONCES_LOCK:
        expired = [
            k for k, created_at in _LAUNCH_NONCES.items()
            if (ts - created_at) >= _LAUNCH_NONCE_TTL_SECONDS
        ]
        for k in expired:
            _LAUNCH_NONCES.pop(k, None)
        created_at = _LAUNCH_NONCES.pop(code, None)
        if created_at is None:
            return False
        return (ts - created_at) < _LAUNCH_NONCE_TTL_SECONDS


def evict_dashboard_mesh_cache(namespace: Optional[str] = None) -> None:
    """Evict cached `Mesh` instances for `namespace` (or all namespaces if None)."""
    global _mesh
    with _MESH_LOCK:
        if namespace is None:
            _MESH_CACHE.clear()
            _mesh = None
            return
        _MESH_CACHE.pop(namespace, None)
        if _mesh is not None and _mesh.namespace == namespace:
            _mesh = None


def _raise_dashboard_http_error(exc: BaseException, endpoint: str) -> None:
    """
    Centralized exception-to-HTTPException mapper for dashboard routes (Issue H5):
      - HTTPException -> re-raised unchanged
      - NamespaceLimitExceededError -> 429
      - NamespaceNotFoundError / FileNotFoundError / missing object ValueError -> 404
      - Conflict / lock timeout / busy database / already exists -> 409
      - Other ValueError (validation / malformed input) -> 400
      - Unexpected Exception -> logged server-side and returned as generic 500
    """
    if isinstance(exc, HTTPException):
        raise exc
    if isinstance(exc, NamespaceLimitExceededError):
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    if isinstance(exc, (NamespaceNotFoundError, FileNotFoundError)):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, TimeoutError):
        raise HTTPException(
            status_code=409,
            detail="Conflict: namespace is currently locked by another operation.",
        ) from exc
    if isinstance(exc, ValueError):
        msg = str(exc)
        msg_low = msg.lower()
        if "not found" in msg_low or "does not exist" in msg_low:
            raise HTTPException(status_code=404, detail=msg) from exc
        if "already exists" in msg_low:
            raise HTTPException(status_code=409, detail=msg) from exc
        raise HTTPException(status_code=400, detail=msg) from exc
    if isinstance(exc, (RuntimeError, sqlite3.OperationalError, OSError)):
        msg_low = str(exc).lower()
        if any(
            marker in msg_low
            for marker in ("locked", "busy", "checkpoint", "another process", "hold", "open")
        ):
            raise HTTPException(
                status_code=409,
                detail="Conflict: another process holds the namespace database open.",
            ) from exc
    logger.exception("Unhandled error in dashboard endpoint '%s': %s", endpoint, exc)
    raise HTTPException(status_code=500, detail="Internal server error.") from exc


def _tokens_match(provided: str, expected: str) -> bool:
    if not provided or not expected:
        return False
    try:
        return hmac.compare_digest(
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


def _extract_request_token(request: Request) -> str:
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        tok = auth_header[7:].strip()
        if tok:
            return tok
    header_tok = request.headers.get("x-mesh-token", "").strip()
    if header_tok:
        return header_tok
    cookie_tok = (request.cookies.get(_SESSION_COOKIE_NAME) or "").strip()
    if cookie_tok:
        return cookie_tok
    query_tok = (request.query_params.get("token") or "").strip()
    return query_tok


def verify_dashboard_auth(request: Request) -> None:
    """
    Verify the local Mesh token via `Authorization: Bearer`, `X-Mesh-Token`,
    `mesh_dash_session` HttpOnly cookie, or `?token=` launch query parameter.
    Can be overridden in unit tests via `api.dependency_overrides[verify_dashboard_auth] = lambda: None`.
    """
    expected_token = get_or_create_auth_token()
    provided_token = _extract_request_token(request)
    if not provided_token or not _tokens_match(provided_token, expected_token):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: missing or invalid Mesh dashboard token.",
        )


api = FastAPI(title="Mesh Dashboard", version=VERSION)


@api.middleware("http")
async def _dashboard_security_middleware(request: Request, call_next: Any) -> Any:
    """
    1. Validate `Host` header to prevent DNS rebinding attacks.
    2. Validate `Origin` header (if present) to prevent cross-origin CSRF requests.
    3. Require valid token or session cookie on all `/api/*` endpoints.
    """
    has_auth_override = verify_dashboard_auth in api.dependency_overrides
    allowed_hosts = set(_ALLOWED_HOSTS)
    if _ACTIVE_BIND_HOST not in ("0.0.0.0", "::", "[::]"):
        allowed_hosts.add(_ACTIVE_BIND_HOST)

    custom_hosts: set[str] = set()
    env_allowed = os.environ.get("MESH_ALLOWED_HOSTS", "").strip()
    if env_allowed:
        for item in env_allowed.split(","):
            ci = _extract_hostname(item.strip().lower())
            if ci:
                allowed_hosts.add(ci)
                custom_hosts.add(ci)

    if has_auth_override:
        allowed_hosts.add("testserver")

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
        is_valid_local = bool(re.match(_LOCAL_ORIGIN_REGEX, origin))
        is_valid_custom = origin_host in custom_hosts and parsed_origin.scheme in ("http", "https")
        if origin_host not in allowed_hosts or not (is_valid_local or is_valid_custom):
            return JSONResponse(
                status_code=403,
                content={"detail": "Forbidden cross-origin request."},
            )

    if request.url.path.startswith("/api/") and not has_auth_override:
        try:
            verify_dashboard_auth(request)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    return await call_next(request)


def _is_cached_mesh_valid(inst: Mesh, expected_ns: str) -> bool:
    try:
        return (
            inst.namespace == expected_ns
            and inst.store.namespace == expected_ns
            and inst.store.db_path.exists()
            and inst.store.db_path.stat().st_size > 0
        )
    except OSError:
        return False


def get_mesh(namespace: Optional[str] = None, create: bool = True) -> Mesh:
    """
    Return a cached `Mesh` instance for the requested (or configured default) namespace.
    - Never falls back to `_mesh.namespace` when `namespace` is omitted (Issue H2).
    - Revalidates `db_path.exists()` and namespace metadata on lookup so out-of-band
      deletions are evicted and recreated according to `create` (Issue H7).
    """
    global _mesh
    ns = _resolve_namespace(namespace)
    evicted_to_clean: list[tuple[str, Any]] = []
    with _MESH_LOCK:
        inst = _MESH_CACHE.get(ns)
        if inst is not None:
            if not _is_cached_mesh_valid(inst, ns):
                _MESH_CACHE.pop(ns, None)
                if _mesh is inst:
                    _mesh = None
                evicted_to_clean.append((ns, inst.store.db_path))
                inst = None
            else:
                if create and not inst.store.create:
                    inst.store.create = True
                _MESH_CACHE.move_to_end(ns)
                return inst

    for ev_ns, ev_path in evicted_to_clean:
        _evict_namespace_caches(ev_ns, ev_path)

    if not create:
        db_file = ensure_secure_mesh_dir().resolve() / f"mesh_{ns}.db"
        if not db_file.exists():
            raise NamespaceNotFoundError(f"Namespace '{ns}' does not exist.")

    evicted_lru: list[tuple[str, Any]] = []
    with _MESH_LOCK:
        inst = _MESH_CACHE.get(ns)
        if inst is not None and _is_cached_mesh_valid(inst, ns):
            if create and not inst.store.create:
                inst.store.create = True
            _MESH_CACHE.move_to_end(ns)
            return inst

        inst = Mesh(namespace=ns, agent_id="dashboard", create=create, trusted_local=True)
        if inst.store.db_path.exists() or create:
            while len(_MESH_CACHE) >= _MESH_CACHE_MAX:
                victim_ns: Optional[str] = None
                for cand_ns in _MESH_CACHE:
                    if cand_ns != ns:
                        victim_ns = cand_ns
                        break
                if victim_ns is None:
                    break
                victim_inst = _MESH_CACHE.pop(victim_ns, None)
                if victim_inst is not None:
                    evicted_lru.append((victim_ns, victim_inst.store.db_path))
            _MESH_CACHE[ns] = inst
            _MESH_CACHE.move_to_end(ns)
            if _mesh is None and ns == _resolve_default_namespace():
                _mesh = inst

    for ev_ns, ev_path in evicted_lru:
        _evict_namespace_caches(ev_ns, ev_path)
    return inst


# --- Request models ---


class CreateMemoryRequest(BaseModel):
    content: str = Field(..., max_length=65536)
    memory_type: str = Field(default="fact", max_length=64)
    confidence: float = 1.0
    tags: List[str] = Field(default_factory=list, max_length=64)
    local_only: bool = False
    ttl_days: Optional[float] = None
    namespace: Optional[str] = Field(default=None, max_length=64)

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, v: List[str]) -> List[str]:
        for item in v:
            if len(str(item)) > 128:
                raise ValueError("Individual tag length cannot exceed 128 characters.")
        return v


class UpdateMemoryRequest(BaseModel):
    content: Optional[str] = Field(default=None, max_length=65536)
    memory_type: Optional[str] = Field(default=None, max_length=64)
    confidence: Optional[float] = None
    tags: Optional[List[str]] = Field(default=None, max_length=64)
    local_only: Optional[bool] = None
    ttl_days: Optional[float] = None
    clear_ttl: Optional[bool] = None
    namespace: Optional[str] = Field(default=None, max_length=64)

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is not None:
            for item in v:
                if len(str(item)) > 128:
                    raise ValueError("Individual tag length cannot exceed 128 characters.")
        return v


# --- REST API endpoints (synchronous `def` so FastAPI runs them in threadpool) ---


@api.get("/api/stats", dependencies=[Depends(verify_dashboard_auth)])
def get_stats(namespace: Optional[str] = None) -> dict[str, Any]:
    try:
        ns = _resolve_namespace(namespace)
        try:
            mesh = get_mesh(ns, create=False)
        except NamespaceNotFoundError:
            return {
                "namespace": ns,
                "namespace_exists": False,
                "did_you_mean": suggest_did_you_mean_namespaces(ns),
                "total_memories": 0,
                "open_contradictions": 0,
                "memory_gaps": 0,
                "stale_memories": 0,
                "unused_memories": 0,
                "engine": get_engine_status(),
            }
        patterns = mesh.patterns(include_private=False)
        contradictions = mesh.contradictions()
        return {
            "namespace": mesh.namespace,
            "namespace_exists": True,
            "total_memories": mesh.count(),
            "open_contradictions": len(contradictions),
            "memory_gaps": len(patterns["memory_gaps"]),
            "stale_memories": len(patterns["stale_memories"]),
            "unused_memories": len(patterns["unused_memories"]),
            "engine": get_engine_status(),
        }
    except Exception as e:
        _raise_dashboard_http_error(e, "/api/stats")


@api.get("/api/memories", dependencies=[Depends(verify_dashboard_auth)])
def get_memories(limit: int = 200, namespace: Optional[str] = None) -> list[dict[str, Any]]:
    try:
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000.")
        ns = _resolve_namespace(namespace)
        try:
            mesh = get_mesh(ns, create=False)
        except NamespaceNotFoundError:
            return []
        return mesh.inspect(limit=limit)
    except Exception as e:
        _raise_dashboard_http_error(e, "/api/memories")


@api.post("/api/memories", dependencies=[Depends(verify_dashboard_auth)])
def create_memory(req: CreateMemoryRequest) -> dict[str, Any]:
    """Add a new memory from the dashboard (runs `guard_ingest` before storing)."""
    try:
        from .guards import guard_ingest

        ns = _resolve_namespace(req.namespace)
        guard = guard_ingest(req.content, local_only=bool(req.local_only))
        if guard.rejected:
            raise HTTPException(status_code=400, detail=guard.reason or "Memory rejected by ingestion guard.")

        try:
            mesh = get_mesh(ns, create=True)
            result = mesh.learn(
                content=guard.content,
                memory_type=req.memory_type,
                confidence=req.confidence,
                tags=req.tags,
                local_only=guard.local_only,
                ttl_days=req.ttl_days if req.ttl_days and req.ttl_days > 0 else None,
            )
        except (NamespaceNotFoundError, sqlite3.OperationalError):
            evict_dashboard_mesh_cache(ns)
            _evict_namespace_caches(ns)
            mesh = get_mesh(ns, create=True)
            result = mesh.learn(
                content=guard.content,
                memory_type=req.memory_type,
                confidence=req.confidence,
                tags=req.tags,
                local_only=guard.local_only,
                ttl_days=req.ttl_days if req.ttl_days and req.ttl_days > 0 else None,
            )
        if guard.warnings:
            result["warnings"] = guard.warnings
        return {"status": "ok", "result": result}
    except Exception as e:
        _raise_dashboard_http_error(e, "POST /api/memories")


@api.put("/api/memories/{memory_id}", dependencies=[Depends(verify_dashboard_auth)])
def update_memory(memory_id: str, req: UpdateMemoryRequest) -> dict[str, Any]:
    """Update a memory in-place, supporting clearing TTL when `clear_ttl=True` or `ttl_days` is omitted/0."""
    try:
        ns = _resolve_namespace(req.namespace)
        content_to_set = req.content
        local_only_to_set = req.local_only
        if content_to_set is not None:
            from .guards import guard_ingest

            guard = guard_ingest(content_to_set, local_only=bool(req.local_only))
            if guard.rejected:
                raise HTTPException(status_code=400, detail=guard.reason or "Memory rejected by ingestion guard.")
            content_to_set = guard.content
            if guard.local_only:
                local_only_to_set = True

        mesh = get_mesh(ns, create=False)
        should_clear_ttl = bool(req.clear_ttl) or (req.ttl_days is not None and req.ttl_days <= 0)
        ttl_val = None if should_clear_ttl else req.ttl_days
        result = mesh.update(
            memory_id=memory_id,
            content=content_to_set,
            memory_type=req.memory_type,
            confidence=req.confidence,
            tags=req.tags,
            local_only=local_only_to_set,
            ttl_days=ttl_val,
            reset_ttl=ttl_val is not None,
            clear_ttl=should_clear_ttl,
        )
        return {"status": "ok", "result": result}
    except Exception as e:
        _raise_dashboard_http_error(e, "PUT /api/memories/{memory_id}")


@api.delete("/api/memories/{memory_id}", dependencies=[Depends(verify_dashboard_auth)])
def delete_memory(memory_id: str, namespace: Optional[str] = None) -> dict[str, str]:
    try:
        ns = _resolve_namespace(namespace)
        mesh = get_mesh(ns, create=False)
        deleted = mesh.forget(memory_id)
        if not deleted:
            raise HTTPException(
                status_code=404,
                detail=f"Memory ID '{memory_id}' not found in namespace '{ns}'.",
            )
        return {"status": "deleted", "memory_id": memory_id}
    except Exception as e:
        _raise_dashboard_http_error(e, "DELETE /api/memories/{memory_id}")


@api.get("/api/patterns", dependencies=[Depends(verify_dashboard_auth)])
def get_patterns(
    namespace: Optional[str] = None,
    include_private: bool = False,
) -> dict[str, Any]:
    try:
        ns = _resolve_namespace(namespace)
        try:
            mesh = get_mesh(ns, create=False)
        except NamespaceNotFoundError:
            return {
                "top_topics": [],
                "memory_gaps": [],
                "unused_memories": [],
                "stale_memories": [],
            }
        return mesh.patterns(include_private=include_private)
    except Exception as e:
        _raise_dashboard_http_error(e, "/api/patterns")


@api.get("/api/contradictions", dependencies=[Depends(verify_dashboard_auth)])
def get_contradictions(namespace: Optional[str] = None) -> list[dict[str, Any]]:
    try:
        ns = _resolve_namespace(namespace)
        try:
            mesh = get_mesh(ns, create=False)
        except NamespaceNotFoundError:
            return []
        return mesh.contradictions()
    except Exception as e:
        _raise_dashboard_http_error(e, "/api/contradictions")


@api.post("/api/contradictions/{contradiction_id}/resolve", dependencies=[Depends(verify_dashboard_auth)])
def resolve_contradiction(contradiction_id: str, namespace: Optional[str] = None) -> dict[str, str]:
    try:
        ns = _resolve_namespace(namespace)
        get_mesh(ns, create=False).resolve_contradiction(contradiction_id)
        return {"status": "resolved"}
    except Exception as e:
        _raise_dashboard_http_error(e, "/api/contradictions/resolve")


@api.get("/api/search", dependencies=[Depends(verify_dashboard_auth)])
def search(
    q: str,
    n: int = 25,
    namespace: Optional[str] = None,
    memory_type: Optional[str] = None,
    include_other: bool = False,
) -> Any:
    if not q or not q.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")
    if len(q) > 16384:
        raise HTTPException(status_code=400, detail="Query exceeds maximum allowed length (16384 chars).")
    if n < 1 or n > 200:
        raise HTTPException(status_code=400, detail="n must be between 1 and 200.")
    try:
        ns = _resolve_namespace(namespace)
        try:
            mesh = get_mesh(ns, create=False)
        except NamespaceNotFoundError:
            if include_other:
                return {
                    "query": q.strip(),
                    "namespace": ns,
                    "namespace_exists": False,
                    "did_you_mean": suggest_did_you_mean_namespaces(ns),
                    "count": 0,
                    "total_in_namespace": 0,
                    "stale_count": 0,
                    "results": [],
                    "other_possible_memories": [],
                    "filter_hint": None,
                }
            return []
        clean_type = (
            memory_type.strip().lower()
            if memory_type and memory_type.strip().lower() != "all"
            else None
        )
        if include_other and hasattr(mesh, "recall_with_context"):
            return mesh.recall_with_context(
                query=q,
                n=n,
                memory_type=clean_type,
                max_other_possible=6,
            )
        return mesh.recall(q, n=n, memory_type=clean_type)
    except Exception as e:
        _raise_dashboard_http_error(e, "/api/search")


@api.get("/api/namespaces", dependencies=[Depends(verify_dashboard_auth)])
def get_namespaces() -> list[dict[str, Any]]:
    try:
        return list_namespaces()
    except Exception as e:
        _raise_dashboard_http_error(e, "/api/namespaces")


@api.get("/api/namespaces/{namespace}/stats", dependencies=[Depends(verify_dashboard_auth)])
def get_namespace_stats(namespace: str) -> dict[str, Any]:
    try:
        clean_ns = validate_namespace(namespace)
        return namespace_stats(clean_ns)
    except Exception as e:
        _raise_dashboard_http_error(e, "/api/namespaces/{namespace}/stats")


@api.delete("/api/namespaces/{namespace}", dependencies=[Depends(verify_dashboard_auth)])
def api_delete_namespace(namespace: str) -> dict[str, Any]:
    try:
        clean_ns = validate_namespace(namespace)
        evict_dashboard_mesh_cache(clean_ns)
        count = delete_namespace(clean_ns)
        return {"deleted": True, "memories_removed": count}
    except Exception as e:
        _raise_dashboard_http_error(e, "DELETE /api/namespaces/{namespace}")


@api.get("/api/export/{namespace}", dependencies=[Depends(verify_dashboard_auth)])
def api_export_namespace(namespace: str, include_embeddings: bool = False) -> dict[str, Any]:
    try:
        return export_namespace(namespace=validate_namespace(namespace), include_embeddings=include_embeddings)
    except Exception as e:
        _raise_dashboard_http_error(e, "/api/export/{namespace}")


@api.get("/api/audit", dependencies=[Depends(verify_dashboard_auth)])
def get_audit(
    namespace: Optional[str] = None,
    limit: int = 100,
    action: str = "all",
) -> list[dict[str, Any]]:
    try:
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000.")
        ns = _resolve_namespace(namespace)
        return get_audit_log(namespace=ns, limit=limit, action_filter=action)
    except Exception as e:
        _raise_dashboard_http_error(e, "/api/audit")


@api.get("/api/digest", dependencies=[Depends(verify_dashboard_auth)])
def get_digest(namespace: Optional[str] = None, hours: int = 24) -> dict[str, Any]:
    try:
        if hours < 1 or hours > 8760:
            raise ValueError("hours must be between 1 and 8760.")
        ns = _resolve_namespace(namespace)
        return generate_digest(namespace=ns, hours=hours)
    except Exception as e:
        _raise_dashboard_http_error(e, "/api/digest")


# --- Minimal, Responsive Dashboard HTML (RAW STRING to preserve JS regex backslashes) ---

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Mesh</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg:       #090a0f;
  --surface:  #10121a;
  --surface2: #161924;
  --border:   rgba(255, 255, 255, 0.07);
  --border2:  rgba(255, 255, 255, 0.13);
  --text:     #eceff4;
  --muted:    #8b95a7;
  --subtle:   #525b6d;
  --accent:   #10b981;
  --accent-bg:rgba(16, 185, 129, 0.12);
  --blue:     #3b82f6;
  --purple:   #8b5cf6;
  --amber:    #f59e0b;
  --red:      #ef4444;
  --font:     -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", Roboto, sans-serif;
  --mono:     "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
html, body {
  height: 100%;
  background: var(--bg);
  color: var(--text);
  font-family: var(--font);
  font-size: 13px;
  line-height: 1.45;
  -webkit-font-smoothing: antialiased;
}
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--surface2); border-radius: 3px; }

.app {
  max-width: 1120px;
  margin: 0 auto;
  padding: 0 16px 40px;
}

header {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 50px;
  border-bottom: 1px solid var(--border);
  position: sticky;
  top: 0;
  background: rgba(9, 10, 15, 0.88);
  backdrop-filter: blur(10px);
  z-index: 50;
  flex-wrap: wrap;
}
.brand {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  font-weight: 600;
  font-size: 14px;
  letter-spacing: -0.02em;
  color: var(--text);
}
.brand-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent);
}
.ns-select {
  background: var(--surface);
  color: var(--muted);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 3px 8px;
  font-size: 12px;
  font-family: var(--mono);
  outline: none;
  cursor: pointer;
}
.ns-select:hover, .ns-select:focus { border-color: var(--border2); color: var(--text); }

.search-wrap {
  flex: 1;
  min-width: 180px;
  max-width: 360px;
  position: relative;
}
.search-wrap input {
  width: 100%;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 5px 26px 5px 10px;
  color: var(--text);
  font-size: 12.5px;
  outline: none;
  transition: border-color 0.12s;
}
.search-wrap input:focus { border-color: var(--accent); }
.search-wrap input::placeholder { color: var(--subtle); }
.search-kbd {
  position: absolute;
  right: 7px;
  top: 50%;
  transform: translateY(-50%);
  font-size: 10px;
  font-family: var(--mono);
  color: var(--subtle);
  border: 1px solid var(--border);
  border-radius: 3px;
  padding: 0 4px;
  pointer-events: none;
}

.metrics-inline {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-left: auto;
  font-size: 12px;
  color: var(--muted);
}
.metric-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.metric-val {
  font-family: var(--mono);
  font-weight: 600;
  color: var(--text);
}
.metric-val.warn { color: var(--amber); }
.metric-val.danger { color: var(--red); }

.btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 5px 10px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 500;
  font-family: var(--font);
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text);
  cursor: pointer;
  transition: all 0.12s;
  white-space: nowrap;
}
.btn:hover { background: var(--surface2); border-color: var(--border2); }
.btn-primary {
  background: var(--accent);
  color: #04130d;
  border-color: transparent;
  font-weight: 600;
}
.btn-primary:hover { background: #0ea572; border-color: transparent; }
.btn-danger { color: var(--red); border-color: rgba(239, 68, 68, 0.25); }
.btn-danger:hover { background: rgba(239, 68, 68, 0.1); }

.subnav {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 0;
  border-bottom: 1px solid var(--border);
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.tabs {
  display: flex;
  gap: 2px;
  overflow-x: auto;
}
.tab {
  padding: 4px 10px;
  border-radius: 5px;
  font-size: 12px;
  font-weight: 500;
  color: var(--muted);
  background: transparent;
  border: none;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  transition: all 0.12s;
}
.tab:hover { color: var(--text); background: var(--surface); }
.tab.active { color: var(--text); background: var(--surface2); }
.tab-count {
  font-family: var(--mono);
  font-size: 10.5px;
  color: var(--subtle);
}
.tab.active .tab-count { color: var(--accent); }

.toolbar {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}
.chip {
  padding: 3px 8px;
  border-radius: 4px;
  font-size: 11.5px;
  color: var(--muted);
  background: transparent;
  border: 1px solid transparent;
  cursor: pointer;
}
.chip:hover { color: var(--text); background: var(--surface); }
.chip.active {
  color: var(--accent);
  background: var(--accent-bg);
  border-color: rgba(16, 185, 129, 0.25);
}
.sort-select {
  margin-left: auto;
  background: var(--surface);
  color: var(--muted);
  border: 1px solid var(--border);
  border-radius: 5px;
  padding: 3px 7px;
  font-size: 11.5px;
  outline: none;
}

.panel { display: none; }
.panel.active { display: block; }

.list {
  display: flex;
  flex-direction: column;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface);
  overflow: hidden;
}
.row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
  transition: background 0.1s;
}
.row:last-child { border-bottom: none; }
.row:hover { background: rgba(255, 255, 255, 0.018); }

.row-main { flex: 1; min-width: 0; }
.row-top {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 4px;
}
.row-content {
  font-family: var(--mono);
  font-size: 12.5px;
  color: var(--text);
  word-break: break-word;
  line-height: 1.5;
}
.row-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 11px;
  color: var(--subtle);
}

.type-pill {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  background: var(--surface2);
  color: var(--muted);
  flex-shrink: 0;
}
.type-pill.fact        { color: #60a5fa; background: rgba(59,130,246,0.12); }
.type-pill.preference  { color: #a78bfa; background: rgba(139,92,246,0.12); }
.type-pill.context     { color: #34d399; background: rgba(16,185,129,0.12); }
.type-pill.result      { color: #4ade80; background: rgba(74,222,128,0.12); }
.type-pill.instruction { color: #fbbf24; background: rgba(245,158,11,0.12); }
.type-pill.process     { color: #38bdf8; background: rgba(56,189,248,0.12); }
.type-pill.decision    { color: #f472b6; background: rgba(244,114,182,0.12); }
.type-pill.error       { color: #f87171; background: rgba(248,113,113,0.12); }
.type-pill.general     { color: #94a3b8; background: rgba(148,163,184,0.12); }

.tag {
  font-family: var(--mono);
  font-size: 10.5px;
  color: var(--muted);
  background: var(--bg);
  border: 1px solid var(--border);
  padding: 0 5px;
  border-radius: 3px;
}
.flag {
  font-size: 10px;
  padding: 0 5px;
  border-radius: 3px;
  font-weight: 500;
}
.flag.stale { color: var(--amber); background: rgba(245,158,11,0.12); }
.flag.private { color: var(--red); background: rgba(239,68,68,0.12); }
.flag.sim { color: var(--accent); background: var(--accent-bg); font-family: var(--mono); }
.flag.reason { color: var(--blue); background: rgba(59, 130, 246, 0.12); font-family: var(--mono); }
.flag.synonym { color: var(--purple); background: rgba(139, 92, 246, 0.14); font-family: var(--mono); }
.flag.rescue { color: var(--amber); background: rgba(245, 158, 11, 0.14); font-family: var(--mono); }

.filter-rescue-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 9px 12px;
  margin-bottom: 10px;
  border-radius: 7px;
  border: 1px solid rgba(245, 158, 11, 0.35);
  background: rgba(245, 158, 11, 0.08);
  color: var(--text);
  font-size: 12px;
}
.other-snippets-wrap { margin-top: 16px; }
.other-snippets-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 8px;
}
.snippet-card {
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  gap: 6px;
  padding: 9px 12px;
  border-radius: 7px;
  border: 1px dashed var(--border2);
  background: rgba(16, 18, 26, 0.65);
  transition: border-color 0.12s, background 0.12s;
}
.snippet-card:hover {
  border-color: var(--accent);
  background: var(--surface);
}
.snippet-top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
}
.snippet-text {
  font-family: var(--mono);
  font-size: 11.5px;
  color: var(--muted);
  line-height: 1.45;
  word-break: break-word;
}

.row-actions {
  display: flex;
  gap: 4px;
  flex-shrink: 0;
  opacity: 0.35;
  transition: opacity 0.12s;
}
.row:hover .row-actions { opacity: 1; }
.icon-btn {
  background: transparent;
  border: 1px solid transparent;
  color: var(--muted);
  width: 24px;
  height: 24px;
  border-radius: 4px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
}
.icon-btn:hover { background: var(--surface2); border-color: var(--border2); color: var(--text); }
.icon-btn.del:hover { color: var(--red); border-color: rgba(239,68,68,0.3); }

.section-label {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--muted);
  margin: 14px 0 6px;
  display: flex;
  align-items: center;
  gap: 6px;
}

.conflict-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  margin-top: 6px;
}
.conflict-box {
  padding: 8px 10px;
  border-radius: 6px;
  font-family: var(--mono);
  font-size: 11.5px;
  border: 1px solid var(--border);
  background: var(--bg);
}
.conflict-box.old { border-left: 2px solid var(--red); }
.conflict-box.new { border-left: 2px solid var(--accent); }

.empty {
  padding: 36px 16px;
  text-align: center;
  color: var(--subtle);
  font-size: 12.5px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface);
}

.backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.65);
  display: none;
  align-items: center;
  justify-content: center;
  z-index: 100;
  padding: 16px;
}
.backdrop.open { display: flex; }
.modal {
  background: var(--surface);
  border: 1px solid var(--border2);
  border-radius: 10px;
  width: 100%;
  max-width: 480px;
  padding: 16px;
  box-shadow: 0 16px 40px rgba(0, 0, 0, 0.5);
}
.modal-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
  font-weight: 600;
  font-size: 13.5px;
}
.field { margin-bottom: 10px; display: flex; flex-direction: column; gap: 4px; }
.field label { font-size: 11px; color: var(--muted); font-weight: 500; }
.field textarea, .field input[type="text"], .field input[type="number"], .field select {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 7px 9px;
  color: var(--text);
  font-family: var(--font);
  font-size: 12.5px;
  outline: none;
}
.field textarea {
  font-family: var(--mono);
  min-height: 76px;
  resize: vertical;
}
.field textarea:focus, .field input:focus, .field select:focus { border-color: var(--accent); }
.field-row { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.modal-foot { display: flex; justify-content: flex-end; gap: 6px; margin-top: 12px; }

#toast {
  position: fixed;
  bottom: 16px;
  right: 16px;
  background: var(--surface2);
  border: 1px solid var(--border2);
  color: var(--text);
  padding: 7px 12px;
  border-radius: 6px;
  font-size: 12px;
  display: none;
  z-index: 200;
}

@media (max-width: 720px) {
  header { height: auto; padding: 8px 0; }
  .search-wrap { order: 3; max-width: 100%; flex-basis: 100%; }
  .metrics-inline { display: none; }
  .conflict-grid, .field-row { grid-template-columns: 1fr; }
  .row-actions { opacity: 1; }
}
</style>
</head>
<body>
<div class="app">
  <header>
    <div class="brand">
      <span class="brand-dot"></span>
      <span>Mesh</span>
    </div>
    <select id="ns-select" class="ns-select" onchange="switchNamespace(this.value)" title="Switch Namespace"></select>
    <div class="search-wrap">
      <input type="text" id="search-input" placeholder="Search memories (semantic + keyword)..." oninput="onSearch(this.value)">
      <span class="search-kbd">/</span>
    </div>
    <div class="metrics-inline">
      <span class="metric-pill">Memories <span class="metric-val" id="m-total">0</span></span>
      <span class="metric-pill">Stale <span class="metric-val warn" id="m-stale">0</span></span>
      <span class="metric-pill">Conflicts <span class="metric-val danger" id="m-conflicts">0</span></span>
      <span class="metric-pill">Gaps <span class="metric-val" id="m-gaps">0</span></span>
    </div>
    <button class="btn" onclick="refreshAll()" title="Refresh">&#x21bb;</button>
    <button class="btn btn-primary" onclick="openModal()">+ New</button>
  </header>

  <div class="subnav">
    <div class="tabs">
      <button class="tab active" onclick="showTab('memories', this)">Memories <span class="tab-count" id="tc-memories">0</span></button>
      <button class="tab" onclick="showTab('conflicts', this)">Conflicts <span class="tab-count" id="tc-conflicts">0</span></button>
      <button class="tab" onclick="showTab('patterns', this)">Patterns</button>
      <button class="tab" onclick="showTab('audit', this)">Audit</button>
      <button class="tab" onclick="showTab('namespaces', this)">Namespaces</button>
      <button class="tab" onclick="showTab('digest', this)">Digest</button>
    </div>
    <span id="shown-count" style="font-size:11.5px;color:var(--subtle);font-family:var(--mono)"></span>
  </div>

  <!-- Memories Panel -->
  <div class="panel active" id="panel-memories">
    <div class="toolbar">
      <button class="chip active" onclick="setType('all', this)">All</button>
      <button class="chip" onclick="setType('fact', this)">Fact</button>
      <button class="chip" onclick="setType('decision', this)">Decision</button>
      <button class="chip" onclick="setType('process', this)">Process</button>
      <button class="chip" onclick="setType('instruction', this)">Instruction</button>
      <button class="chip" onclick="setType('preference', this)">Preference</button>
      <button class="chip" onclick="setType('context', this)">Context</button>
      <button class="chip" onclick="setType('result', this)">Result</button>
      <button class="chip" onclick="setType('error', this)">Error</button>
      <button class="chip" onclick="setType('general', this)">General</button>
      <select class="sort-select" onchange="setSort(this.value)">
        <option value="accessed">Sort: Recent access</option>
        <option value="created">Sort: Created</option>
        <option value="confidence">Sort: Confidence</option>
      </select>
    </div>
    <div id="memory-list"></div>
  </div>

  <!-- Conflicts Panel -->
  <div class="panel" id="panel-conflicts">
    <div id="conflicts-list"></div>
  </div>

  <!-- Patterns Panel -->
  <div class="panel" id="panel-patterns">
    <div class="section-label">Top Queries</div>
    <div id="patterns-topics"></div>
    <div class="section-label">Memory Gaps (Low Similarity)</div>
    <div id="patterns-gaps"></div>
    <div class="section-label">Unused Memories (7+ Days)</div>
    <div id="patterns-unused"></div>
  </div>

  <!-- Audit Panel -->
  <div class="panel" id="panel-audit">
    <div class="toolbar">
      <button class="chip active" onclick="loadAudit('all', this)">All</button>
      <button class="chip" onclick="loadAudit('read', this)">Reads</button>
      <button class="chip" onclick="loadAudit('write', this)">Writes</button>
      <button class="chip" onclick="loadAudit('update', this)">Updates</button>
      <button class="chip" onclick="loadAudit('forget', this)">Deletes</button>
    </div>
    <div id="audit-list"></div>
  </div>

  <!-- Namespaces Panel -->
  <div class="panel" id="panel-namespaces">
    <div id="namespaces-list"></div>
  </div>

  <!-- Digest Panel -->
  <div class="panel" id="panel-digest">
    <div id="digest-list"></div>
  </div>
</div>

<!-- Add / Edit Modal -->
<div class="backdrop" id="backdrop" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <div class="modal-head">
      <span id="modal-title">New Memory</span>
      <button class="icon-btn" onclick="closeModal()" title="Close">&#x2715;</button>
    </div>
    <div class="field">
      <label>Content</label>
      <textarea id="f-content" placeholder="Specific fact, decision, or rule..."></textarea>
    </div>
    <div class="field-row">
      <div class="field">
        <label>Type</label>
        <select id="f-type">
          <option value="fact">fact</option>
          <option value="decision">decision</option>
          <option value="process">process</option>
          <option value="instruction">instruction</option>
          <option value="preference">preference</option>
          <option value="context">context</option>
          <option value="result">result</option>
          <option value="error">error</option>
          <option value="general">general</option>
        </select>
      </div>
      <div class="field">
        <label>Confidence (0.0 - 1.0)</label>
        <input type="number" id="f-conf" min="0" max="1" step="0.05" value="1.0">
      </div>
    </div>
    <div class="field-row">
      <div class="field">
        <label>Tags (comma-separated)</label>
        <input type="text" id="f-tags" placeholder="api, db, auth">
      </div>
      <div class="field">
        <label>TTL Days (optional — clear to make permanent)</label>
        <input type="number" id="f-ttl" min="1" step="1" placeholder="Permanent">
      </div>
    </div>
    <label style="display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);cursor:pointer;margin-top:2px">
      <input type="checkbox" id="f-private"> Private (local_only - exclude from exports &amp; prompts)
    </label>
    <div class="modal-foot">
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" id="f-submit" onclick="saveMemory()">Save</button>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
const _urlParams = new URLSearchParams(location.search);
let MESH_TOKEN = _urlParams.get('token') || '';
if (_urlParams.has('token')) {
  _urlParams.delete('token');
  const cleanSearch = _urlParams.toString();
  history.replaceState(null, '', location.pathname + (cleanSearch ? '?' + cleanSearch : ''));
}

let ns = _urlParams.get('ns') || '';
let memories = [];
let searchResults = null;
let otherPossibleResults = [];
let searchFilterHint = null;
let activeType = 'all';
let activeTab = 'memories';
let activeAuditFilter = 'all';
let sortBy = 'accessed';
let editId = null;
let editOrigTtl = null;
let debounceTimer = null;
// Monotonic token so an out-of-order search response can never overwrite the
// results of a newer query (or of a namespace that has since been switched).
let searchSeq = 0;

function authHeaders(extra = {}) {
  const h = { ...extra };
  if (MESH_TOKEN) {
    h['X-Mesh-Token'] = MESH_TOKEN;
  }
  return h;
}

// Delegated click handler for all dynamic row actions (prevents inline onclick XSS)
document.addEventListener('click', e => {
  const el = e.target.closest('[data-action]');
  if (!el) return;
  const action = el.getAttribute('data-action');
  const targetId = el.getAttribute('data-id') || '';
  if (action === 'edit') openEdit(targetId);
  else if (action === 'delete') delMemory(targetId);
  else if (action === 'jump-type') jumpToType(targetId);
  else if (action === 'resolve-conflict') resolveConflict(targetId);
  else if (action === 'switch-ns') switchNamespace(targetId);
  else if (action === 'export-ns') exportNs(targetId);
  else if (action === 'delete-ns') deleteNs(targetId);
});

window.addEventListener('DOMContentLoaded', async () => {
  await refreshAll();
});

let authFailureShown = false;
async function apiFetch(path, opts = {}) {
  try {
    const mergedOpts = {
      credentials: 'same-origin',
      ...opts,
      headers: authHeaders(opts.headers || {})
    };
    const res = await fetch(path, mergedOpts);
    if (res.status === 401 || res.status === 403) {
      if (!authFailureShown) {
        authFailureShown = true;
        notify('Authentication failed — reopen the dashboard with its access token');
      }
      return null;
    }
    return res.ok ? await res.json() : null;
  } catch { return null; }
}

async function refreshAll() {
  const stats = await apiFetch(`/api/stats${ns ? '?namespace=' + encodeURIComponent(ns) : ''}`);
  if (stats) {
    ns = stats.namespace;
    document.getElementById('m-total').textContent = stats.total_memories;
    document.getElementById('m-stale').textContent = stats.stale_memories;
    document.getElementById('m-conflicts').textContent = stats.open_contradictions;
    document.getElementById('m-gaps').textContent = stats.memory_gaps;
    document.getElementById('tc-memories').textContent = stats.total_memories;
    document.getElementById('tc-conflicts').textContent = stats.open_contradictions;
  }
  await populateNamespaceDropdown();
  await loadMemories();
  if (activeTab === 'conflicts') await loadConflicts();
  else if (activeTab === 'patterns') await loadPatterns();
  else if (activeTab === 'audit') await loadAudit(activeAuditFilter);
  else if (activeTab === 'namespaces') await loadNamespaces();
  else if (activeTab === 'digest') await loadDigest();
}

async function populateNamespaceDropdown() {
  const list = await apiFetch('/api/namespaces') || [];
  const names = new Set(list.map(x => x.namespace));
  if (ns) names.add(ns);
  const sel = document.getElementById('ns-select');
  sel.innerHTML = Array.from(names).sort().map(n =>
    `<option value="${esc(n)}" ${n === ns ? 'selected' : ''}>${esc(n)}</option>`
  ).join('');
}

function switchNamespace(newNs) {
  ns = newNs;
  // Cancel any in-flight/debounced search for the previous namespace; its
  // response must not populate the new namespace's view.
  clearTimeout(debounceTimer);
  searchSeq++;
  searchResults = null;
  otherPossibleResults = [];
  searchFilterHint = null;
  document.getElementById('search-input').value = '';
  refreshAll();
}

async function loadMemories() {
  const data = await apiFetch(`/api/memories?limit=200&namespace=${encodeURIComponent(ns)}`);
  memories = Array.isArray(data) ? data : [];
  renderMemories();
}

function parseTags(m) {
  if (Array.isArray(m.tags_list)) return m.tags_list;
  if (Array.isArray(m.tags)) return m.tags;
  if (typeof m.tags === 'string' && m.tags.trim()) {
    try { const p = JSON.parse(m.tags); return Array.isArray(p) ? p : []; } catch { return []; }
  }
  return [];
}

function renderMemories() {
  let list = searchResults !== null ? [...searchResults] : [...memories];
  if (activeType !== 'all') {
    list = list.filter(m => (m.memory_type || 'fact') === activeType);
  }
  if (searchResults === null) {
    if (sortBy === 'accessed') {
      list.sort((a, b) => (b.accessed_at || b.created_at || '').localeCompare(a.accessed_at || a.created_at || ''));
    } else if (sortBy === 'created') {
      list.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
    } else if (sortBy === 'confidence') {
      list.sort((a, b) => (b.effective_confidence ?? b.confidence ?? 1) - (a.effective_confidence ?? a.confidence ?? 1));
    }
  }

  document.getElementById('shown-count').textContent =
    searchResults !== null ? `${list.length} matches` : `${list.length} items`;

  const root = document.getElementById('memory-list');
  let html = '';

  const rescuedCount = otherPossibleResults.filter(x => String(x.reason || '').startsWith('matched_in_other_type_or_tag')).length;
  if (searchResults !== null && activeType !== 'all' && rescuedCount > 0) {
    html += `<div class="filter-rescue-banner">
      <span><strong>${rescuedCount} strong match${rescuedCount > 1 ? 'es' : ''}</strong> found in other memory types (hidden by active <code>${esc(activeType)}</code> filter).</span>
      <button class="btn" onclick="resetTypeFilter()">Show All Types</button>
    </div>`;
  } else if (searchFilterHint) {
    html += `<div class="filter-rescue-banner">
      <span>${esc(searchFilterHint)}</span>
      <button class="btn" onclick="resetTypeFilter()">Show All Types</button>
    </div>`;
  }

  if (!list.length) {
    html += `<div class="empty">No memories match the current view.</div>`;
  } else {
    html += `<div class="list">` + list.map(m => {
      const t = m.memory_type || 'fact';
      const tags = parseTags(m);
      const conf = (m.effective_confidence ?? m.confidence ?? 1.0).toFixed(2);
      const sim = m.similarity !== undefined ? `<span class="flag sim">${Math.round(m.similarity * 100)}% match</span>` : '';
      const reasonRaw = m.match_reason || '';
      const reasonClass = reasonRaw.includes('synonym') ? 'synonym' : 'reason';
      const reasonBadge = (m.similarity !== undefined && reasonRaw) ? `<span class="flag ${reasonClass}">${esc(reasonRaw)}</span>` : '';
      const stale = m.is_stale ? `<span class="flag stale">stale</span>` : '';
      const priv = m.local_only ? `<span class="flag private">private</span>` : '';
      return `<div class="row">
        <div class="row-main">
          <div class="row-top">
            <span class="type-pill ${esc(t)}">${esc(t)}</span>
            <span class="row-content">${esc(m.content)}</span>
          </div>
          <div class="row-meta">
            ${sim}${reasonBadge}${stale}${priv}
            <span>conf ${conf}</span>
            <span>by ${esc(m.source_agent || 'agent')}</span>
            ${m.access_count ? `<span>${m.access_count}x read</span>` : ''}
            ${m.created_at ? `<span>${relTime(m.created_at)}</span>` : ''}
            ${tags.map(tag => `<span class="tag">#${esc(tag)}</span>`).join('')}
          </div>
        </div>
        <div class="row-actions">
          <button class="icon-btn" title="Edit" data-action="edit" data-id="${escAttr(m.id)}">&#x270e;</button>
          <button class="icon-btn del" title="Delete" data-action="delete" data-id="${escAttr(m.id)}">&#x2715;</button>
        </div>
      </div>`;
    }).join('') + `</div>`;
  }

  const shownIds = new Set(list.map(m => m.id));
  const snippets = (searchResults !== null ? otherPossibleResults : []).filter(s => s.id && !shownIds.has(s.id));
  if (snippets.length > 0) {
    html += `<div class="other-snippets-wrap">
      <div class="section-label">Other Possible Memories in '${esc(ns)}' (Peripheral Snippets &middot; ${snippets.length})</div>
      <div class="other-snippets-grid">` +
      snippets.map(s => {
        const st = s.memory_type || 'fact';
        const stags = parseTags(s);
        const sSim = s.similarity && s.similarity > 0 ? `<span class="flag sim">${Math.round(s.similarity * 100)}%</span>` : '';
        const isRescue = String(s.reason || '').startsWith('matched_in_other_type_or_tag');
        const sReason = `<span class="flag ${isRescue ? 'rescue' : 'reason'}">${esc(s.reason || 'context')}</span>`;
        const preview = s.snippet || s.content || '';
        return `<div class="snippet-card">
          <div class="snippet-top">
            <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
              <span class="type-pill ${esc(st)}" style="cursor:pointer" title="Filter by ${esc(st)}" data-action="jump-type" data-id="${escAttr(st)}">${esc(st)}</span>
              ${sSim}${sReason}
            </div>
            <div class="row-actions" style="opacity:0.85">
              <button class="icon-btn" title="Edit Memory" data-action="edit" data-id="${escAttr(s.id)}">&#x270e;</button>
            </div>
          </div>
          <div class="snippet-text">${esc(preview)}</div>
          <div class="row-meta">
            <span>id ${esc(String(s.id).slice(0, 8))}..</span>
            ${stags.map(tag => `<span class="tag">#${esc(tag)}</span>`).join('')}
          </div>
        </div>`;
      }).join('') +
      `</div></div>`;
  }

  root.innerHTML = html;
}

function resetTypeFilter() {
  const allBtn = document.querySelector('#panel-memories .chip');
  if (allBtn) setType('all', allBtn);
}

function jumpToType(type) {
  const chips = Array.from(document.querySelectorAll('#panel-memories .chip'));
  const target = chips.find(c => c.textContent.trim().toLowerCase() === type.toLowerCase());
  if (target) setType(type.toLowerCase(), target);
}

function setType(type, btn) {
  activeType = type;
  document.querySelectorAll('#panel-memories .chip').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  const q = document.getElementById('search-input').value.trim();
  if (q.length >= 2) {
    onSearch(q);
  } else {
    renderMemories();
  }
}

function setSort(val) {
  sortBy = val;
  renderMemories();
}

function onSearch(val) {
  clearTimeout(debounceTimer);
  const q = val.trim();
  if (!q) {
    searchSeq++;
    searchResults = null;
    otherPossibleResults = [];
    searchFilterHint = null;
    renderMemories();
    return;
  }

  const ql = q.toLowerCase();
  const matchedAll = memories.filter(m =>
    (m.content || '').toLowerCase().includes(ql) ||
    parseTags(m).some(t => t.toLowerCase().includes(ql))
  );
  searchResults = activeType === 'all'
    ? matchedAll
    : matchedAll.filter(m => (m.memory_type || 'fact') === activeType);

  const primaryIds = new Set(searchResults.map(m => m.id));
  otherPossibleResults = [
    ...matchedAll.filter(m => !primaryIds.has(m.id)).map(m => ({
      ...m,
      snippet: (m.content || '').slice(0, 120),
      reason: `matched_in_other_type_or_tag (type=${m.memory_type || 'fact'})`
    })),
    ...memories.filter(m => !primaryIds.has(m.id) && !matchedAll.includes(m)).slice(0, 4).map(m => ({
      ...m,
      snippet: (m.content || '').slice(0, 120),
      reason: 'namespace_context'
    }))
  ].slice(0, 6);

  renderMemories();

  if (q.length >= 2) {
    const seq = ++searchSeq;
    const requestedNs = ns;
    debounceTimer = setTimeout(async () => {
      const typeParam = activeType !== 'all' ? `&memory_type=${encodeURIComponent(activeType)}` : '';
      const res = await apiFetch(
        `/api/search?q=${encodeURIComponent(q)}&n=25&include_other=true&namespace=${encodeURIComponent(requestedNs)}${typeParam}`
      );
      if (seq !== searchSeq || requestedNs !== ns) return;
      if (res && Array.isArray(res.results)) {
        searchResults = res.results;
        otherPossibleResults = Array.isArray(res.other_possible_memories) ? res.other_possible_memories : [];
        searchFilterHint = res.filter_hint || null;
        renderMemories();
      } else if (Array.isArray(res)) {
        searchResults = res;
        renderMemories();
      }
    }, 180);
  }
}

function openModal() {
  editId = null;
  editOrigTtl = null;
  document.getElementById('modal-title').textContent = 'New Memory';
  document.getElementById('f-content').value = '';
  document.getElementById('f-type').value = 'fact';
  document.getElementById('f-conf').value = '1.0';
  document.getElementById('f-tags').value = '';
  document.getElementById('f-ttl').value = '';
  document.getElementById('f-private').checked = false;
  document.getElementById('backdrop').classList.add('open');
  setTimeout(() => document.getElementById('f-content').focus(), 40);
}

function openEdit(id) {
  const pool = [
    ...(searchResults || []),
    ...(otherPossibleResults || []),
    ...memories
  ];
  const m = pool.find(x => x.id === id);
  if (!m) return;
  editId = id;
  editOrigTtl = m.ttl_days ?? null;
  document.getElementById('modal-title').textContent = 'Edit Memory';
  document.getElementById('f-content').value = m.content || m.snippet || '';
  document.getElementById('f-type').value = m.memory_type || 'fact';
  document.getElementById('f-conf').value = m.confidence ?? 1.0;
  document.getElementById('f-tags').value = parseTags(m).join(', ');
  document.getElementById('f-ttl').value = m.ttl_days || '';
  document.getElementById('f-private').checked = Boolean(m.local_only);
  document.getElementById('backdrop').classList.add('open');
}

function closeModal() {
  document.getElementById('backdrop').classList.remove('open');
  editId = null;
  editOrigTtl = null;
}

async function saveMemory() {
  const content = document.getElementById('f-content').value.trim();
  if (!content) return notify('Content is required');
  const tags = document.getElementById('f-tags').value.split(',').map(s => s.trim()).filter(Boolean);
  const ttlRaw = document.getElementById('f-ttl').value.trim();
  const parsedTtl = ttlRaw ? parseFloat(ttlRaw) : null;
  const ttlChanged = parsedTtl !== editOrigTtl;
  const confRaw = parseFloat(document.getElementById('f-conf').value);

  const payload = {
    content,
    memory_type: document.getElementById('f-type').value,
    // `|| 1.0` would silently turn a legitimate 0 confidence into 1.0.
    confidence: Number.isFinite(confRaw) ? confRaw : 1.0,
    tags,
    local_only: document.getElementById('f-private').checked,
    namespace: ns
  };

  if (editId) {
    if (ttlChanged) {
      if (parsedTtl && parsedTtl > 0) {
        payload.ttl_days = parsedTtl;
      } else if (editOrigTtl !== null) {
        payload.clear_ttl = true;
      }
    }
  } else {
    if (parsedTtl && parsedTtl > 0) {
      payload.ttl_days = parsedTtl;
    }
  }

  const url = editId ? `/api/memories/${encodeURIComponent(editId)}` : '/api/memories';
  const method = editId ? 'PUT' : 'POST';

  const res = await fetch(url, {
    method,
    credentials: 'same-origin',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(payload)
  });

  if (res.ok) {
    const wasEdit = Boolean(editId);
    closeModal();
    searchResults = null;
    otherPossibleResults = [];
    searchFilterHint = null;
    document.getElementById('search-input').value = '';
    notify(wasEdit ? 'Memory updated' : 'Memory saved');
    await refreshAll();
  } else {
    let detail = '';
    try {
      const body = await res.json();
      const reason = body && (body.detail || body.error);
      if (reason) detail = `: ${typeof reason === 'string' ? reason : JSON.stringify(reason)}`;
    } catch { /* non-JSON error body */ }
    notify(`Failed to save memory${detail}`);
  }
}

async function delMemory(id) {
  if (!confirm('Delete this memory?')) return;
  const res = await fetch(`/api/memories/${encodeURIComponent(id)}?namespace=${encodeURIComponent(ns)}`, {
    method: 'DELETE',
    credentials: 'same-origin',
    headers: authHeaders()
  });
  if (res.ok) {
    memories = memories.filter(m => m.id !== id);
    if (searchResults) searchResults = searchResults.filter(m => m.id !== id);
    if (otherPossibleResults) otherPossibleResults = otherPossibleResults.filter(m => m.id !== id);
    renderMemories();
    notify('Memory deleted');
    refreshAll();
  }
}

async function loadConflicts() {
  const list = await apiFetch(`/api/contradictions?namespace=${encodeURIComponent(ns)}`) || [];
  const el = document.getElementById('conflicts-list');
  if (!list.length) {
    el.innerHTML = `<div class="empty">No unresolved conflicts in '${esc(ns)}'.</div>`;
    return;
  }
  el.innerHTML = `<div class="list">` + list.map(c => `<div class="row" style="flex-direction:column">
    <div style="display:flex;justify-content:space-between;width:100%;align-items:center">
      <span class="row-meta"><span class="flag stale">conflict</span> similarity ${(c.similarity || 0).toFixed(2)} &middot; ${relTime(c.detected_at)}</span>
      <button class="btn" data-action="resolve-conflict" data-id="${escAttr(c.id)}">Mark Resolved</button>
    </div>
    <div class="conflict-grid" style="width:100%">
      <div class="conflict-box old"><div style="font-size:10px;color:var(--subtle);margin-bottom:3px">EXISTING</div>${esc(c.existing_content)}</div>
      <div class="conflict-box new"><div style="font-size:10px;color:var(--subtle);margin-bottom:3px">NEW</div>${esc(c.new_content)}</div>
    </div>
  </div>`).join('') + `</div>`;
}

async function resolveConflict(id) {
  const res = await fetch(`/api/contradictions/${encodeURIComponent(id)}/resolve?namespace=${encodeURIComponent(ns)}`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: authHeaders()
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => '');
    notify(`Could not resolve conflict (HTTP ${res.status})${detail ? ': ' + detail : ''}`);
    return;
  }
  notify('Conflict resolved');
  loadConflicts();
  refreshAll();
}

async function loadPatterns() {
  const data = await apiFetch(`/api/patterns?namespace=${encodeURIComponent(ns)}`);
  if (!data) return;

  const tEl = document.getElementById('patterns-topics');
  tEl.innerHTML = (data.top_topics || []).length
    ? `<div class="list">` + data.top_topics.map(t => `<div class="row">
        <span class="row-content">${esc(t.query_text)}</span>
        <span class="row-meta">${t.query_count} queries &middot; avg sim ${(t.avg_similarity || 0).toFixed(2)}</span>
      </div>`).join('') + `</div>`
    : `<div class="empty">No queries logged yet.</div>`;

  const gEl = document.getElementById('patterns-gaps');
  gEl.innerHTML = (data.memory_gaps || []).length
    ? `<div class="list">` + data.memory_gaps.map(g => `<div class="row">
        <span class="row-content">${esc(g.query_text)}</span>
        <span class="row-meta"><span class="flag stale">gap</span> asked ${g.query_count}x &middot; avg sim ${(g.avg_similarity || 0).toFixed(2)}</span>
      </div>`).join('') + `</div>`
    : `<div class="empty">No memory gaps detected.</div>`;

  const uEl = document.getElementById('patterns-unused');
  uEl.innerHTML = (data.unused_memories || []).length
    ? `<div class="list">` + data.unused_memories.map(m => `<div class="row">
        <span class="row-content">${esc(m.content)}</span>
        <button class="icon-btn del" title="Delete" data-action="delete" data-id="${escAttr(m.id)}">&#x2715;</button>
      </div>`).join('') + `</div>`
    : `<div class="empty">No unused memories.</div>`;
}

async function loadAudit(filter = 'all', btn = null) {
  activeAuditFilter = filter;
  if (btn) {
    document.querySelectorAll('#panel-audit .chip').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
  }
  const list = await apiFetch(`/api/audit?namespace=${encodeURIComponent(ns)}&limit=100&action=${encodeURIComponent(filter)}`) || [];
  const el = document.getElementById('audit-list');
  if (!list.length) {
    el.innerHTML = `<div class="empty">No audit activity recorded yet.</div>`;
    return;
  }
  el.innerHTML = `<div class="list">` + list.map(e => `<div class="row">
    <div class="row-main">
      <div class="row-top">
        <span class="type-pill">${esc(e.action)}</span>
        <span class="row-content">${esc(e.action === 'recall' ? e.query : e.content)}</span>
      </div>
      <div class="row-meta">
        <span>by ${esc(e.agent_id || 'agent')}</span>
        <span>${relTime(e.timestamp)}</span>
        ${e.top_result ? `<span>&rarr; "${esc(e.top_result.slice(0, 70))}"</span>` : ''}
      </div>
    </div>
  </div>`).join('') + `</div>`;
}

async function loadNamespaces() {
  const list = await apiFetch('/api/namespaces') || [];
  const el = document.getElementById('namespaces-list');
  if (!list.length) {
    el.innerHTML = `<div class="empty">No namespaces found.</div>`;
    return;
  }
  el.innerHTML = `<div class="list">` + list.map(n => `<div class="row" style="align-items:center">
    <div class="row-main">
      <div class="row-top">
        <span class="row-content" style="font-weight:600;cursor:pointer" data-action="switch-ns" data-id="${escAttr(n.namespace)}">${esc(n.namespace)}</span>
      </div>
      <div class="row-meta">
        <span>${n.memory_count} memories</span>
        <span>${n.local_only_count} private</span>
        <span>updated ${relTime(n.last_updated)}</span>
      </div>
    </div>
    <div style="display:flex;gap:6px">
      <button class="btn" data-action="export-ns" data-id="${escAttr(n.namespace)}">Export</button>
      <button class="btn btn-danger" data-action="delete-ns" data-id="${escAttr(n.namespace)}">Delete</button>
    </div>
  </div>`).join('') + `</div>`;
}

async function exportNs(targetNs) {
  const data = await apiFetch(`/api/export/${encodeURIComponent(targetNs)}?include_embeddings=false`);
  if (!data) return;
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `mesh_${targetNs}.json`;
  a.click();
  notify(`Exported ${data.memory_count} memories`);
}

async function deleteNs(targetNs) {
  if (!confirm(`Delete namespace '${targetNs}' and all its memories?`)) return;
  const res = await fetch(`/api/namespaces/${encodeURIComponent(targetNs)}`, {
    method: 'DELETE',
    credentials: 'same-origin',
    headers: authHeaders()
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => '');
    notify(`Could not delete ${targetNs} (HTTP ${res.status})${detail ? ': ' + detail : ''}`);
    return;
  }
  if (targetNs === ns) {
    ns = '';
    searchResults = null;
    otherPossibleResults = [];
    searchFilterHint = null;
  }
  notify(`Deleted ${targetNs}`);
  await loadNamespaces();
  await refreshAll();
}

async function loadDigest() {
  const d = await apiFetch(`/api/digest?namespace=${encodeURIComponent(ns)}&hours=24`);
  const el = document.getElementById('digest-list');
  if (!d) return;
  if (!d.new_count && !d.contradiction_count && !d.stale_count) {
    el.innerHTML = `<div class="empty">All clear &mdash; no new activity or stale memories in the last 24h.</div>`;
    return;
  }
  const items = (d.new_memories || []).map(m => `<div class="row">
    <div class="row-main">
      <div class="row-top"><span class="type-pill ${esc(m.memory_type)}">${esc(m.memory_type)}</span><span class="row-content">${esc(m.content)}</span></div>
      <div class="row-meta"><span>by ${esc(m.source_agent)}</span><span>${relTime(m.created_at)}</span></div>
    </div>
  </div>`).join('');
  el.innerHTML = `<div class="section-label">Last 24h: ${d.new_count} new &middot; ${d.total_recalls} recalls &middot; ${d.contradiction_count} conflicts</div>` +
    (items ? `<div class="list">${items}</div>` : '');
}

function showTab(name, btn) {
  activeTab = name;
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('panel-' + name).classList.add('active');
  btn.classList.add('active');
  if (name === 'conflicts') loadConflicts();
  if (name === 'patterns') loadPatterns();
  if (name === 'audit') loadAudit(activeAuditFilter);
  if (name === 'namespaces') loadNamespaces();
  if (name === 'digest') loadDigest();
}

function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function escAttr(s) {
  return esc(s).replace(/\\/g, '\\\\');
}

function relTime(s) {
  if (!s || s === 'never') return 'never';
  const d = new Date(s);
  if (isNaN(d)) return String(s).slice(0, 10);
  const sec = (Date.now() - d.getTime()) / 1000;
  if (sec < 60) return 'just now';
  if (sec < 3600) return Math.floor(sec / 60) + 'm ago';
  if (sec < 86400) return Math.floor(sec / 3600) + 'h ago';
  if (sec < 604800) return Math.floor(sec / 86400) + 'd ago';
  return d.toLocaleDateString();
}

function notify(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.display = 'block';
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.style.display = 'none'; }, 2400);
}

document.addEventListener('keydown', e => {
  const activeTag = document.activeElement ? document.activeElement.tagName : '';
  if (e.key === '/' && activeTag !== 'INPUT' && activeTag !== 'TEXTAREA' && activeTag !== 'SELECT') {
    e.preventDefault();
    document.getElementById('search-input').focus();
  } else if (e.key === 'n' && !e.metaKey && !e.ctrlKey && activeTag !== 'INPUT' && activeTag !== 'TEXTAREA' && activeTag !== 'SELECT') {
    e.preventDefault();
    openModal();
  } else if (e.key === 'Escape') {
    closeModal();
  }
});
</script>
</body>
</html>"""


@api.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    """
    Serve the Dashboard UI.
    Never embeds `~/.mesh/auth_token` into unauthenticated HTML responses.
    If the caller presents a valid `?token=` launch parameter, `?code=` launch nonce,
    header, or cookie, sets an `HttpOnly; SameSite=Strict` session cookie for subsequent `/api/*` calls.
    """
    expected_token = get_or_create_auth_token()
    provided_token = _extract_request_token(request)
    code = (request.query_params.get("code") or "").strip()
    if code and _consume_launch_nonce(code):
        provided_token = expected_token

    has_auth_override = verify_dashboard_auth in api.dependency_overrides
    is_authenticated = has_auth_override or (
        bool(provided_token) and _tokens_match(provided_token, expected_token)
    )

    response = HTMLResponse(content=DASHBOARD_HTML, status_code=200)
    if is_authenticated:
        response.set_cookie(
            key=_SESSION_COOKIE_NAME,
            value=expected_token,
            httponly=True,
            samesite="strict",
            path="/",
        )
    return response


# --- CLI entry point (standard library argparse, no click runtime dependency) ---


def run(
    namespace: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    no_browser: Optional[bool] = None,
    yes: Optional[bool] = None,
) -> None:
    """Start the Mesh dashboard at http://localhost:<port>"""
    global _mesh, _default_namespace, _ACTIVE_BIND_HOST
    parser = argparse.ArgumentParser(description="Start the Mesh web dashboard.")
    parser.add_argument(
        "--namespace",
        default=os.environ.get("MESH_NAMESPACE", "shared"),
        help="Mesh namespace to inspect (default: shared)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface to bind (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=7433,
        help="Port to run on (default: 7433)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't auto-open the browser",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip interactive y/n permission prompts",
    )
    args, _ = parser.parse_known_args()

    resolved_ns = namespace if namespace is not None else args.namespace
    resolved_host = (host if host is not None else args.host).strip()
    resolved_port = int(port if port is not None else args.port)
    resolved_no_browser = bool(no_browser if no_browser is not None else args.no_browser)
    resolved_yes = bool(yes if yes is not None else args.yes)

    clean_ns = validate_namespace(resolved_ns)
    _default_namespace = clean_ns
    _ACTIVE_BIND_HOST = resolved_host.lower()

    env_allowed = os.environ.get("MESH_ALLOWED_HOSTS", "").strip()
    if _ACTIVE_BIND_HOST not in ("127.0.0.1", "localhost", "::1", "[::1]"):
        if not env_allowed:
            err_msg = (
                f"Refusing to start dashboard on non-loopback bind '{resolved_host}' without MESH_ALLOWED_HOSTS specified.\n"
                f"Set MESH_ALLOWED_HOSTS to comma-separated allowed hostnames/IPs to confirm.\n"
            )
            sys.stderr.write(err_msg)
            raise ValueError(err_msg)
        else:
            sys.stderr.write(
                f"[WARNING] Binding Mesh dashboard to '{resolved_host}' with MESH_ALLOWED_HOSTS={env_allowed}. "
                "Traffic is unencrypted plain HTTP protected only by bearer-token authentication.\n"
            )

    get_or_create_auth_token()
    db_file = ensure_secure_mesh_dir().resolve() / f"mesh_{clean_ns}.db"
    if db_file.exists():
        _mesh = Mesh(namespace=clean_ns, agent_id="dashboard", create=False, trusted_local=True)

    from .memory import _has_local_onnx_pair

    if _has_local_onnx_pair() is None:
        if request_user_permission(
            "download the ~90MB ONNX embedding model and start the background warmup worker for the Dashboard",
            assume_yes=resolved_yes,
        ):
            threading.Thread(target=warmup_model, args=(clean_ns, False, True), daemon=True).start()
    else:
        threading.Thread(target=warmup_model, args=(clean_ns, False, False), daemon=True).start()

    nonce = secrets.token_urlsafe(16)
    _register_launch_nonce(nonce)
    launch_url = f"http://localhost:{resolved_port}/?ns={clean_ns}&code={nonce}"

    from . import ui

    ui.header("Dashboard", f"namespace {clean_ns}")
    print()
    ui.kv([("Open", ui.style(launch_url, "info")), ("Browser", "opening automatically" if not resolved_no_browser else "not opened (--no-browser)")])
    print()
    ui.hint("Press Ctrl+C to stop", stream=sys.stdout)
    print()

    if not resolved_no_browser:
        def open_browser() -> None:
            time.sleep(1.0)
            webbrowser.open(launch_url)

        threading.Thread(target=open_browser, daemon=True).start()

    uvicorn.run(api, host=resolved_host, port=resolved_port, log_level="error")


if __name__ == "__main__":
    run()
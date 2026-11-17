r"""
mesh/http_server.py — High-speed HTTP REST API & Background Daemon Server for Mesh.

Runs on 127.0.0.1:7701 by default. Separate from the dashboard (port 7433).

Addresses Findings:
  - S1: Supports `secret_ref` locator metadata and `vault_secrets` on `/learn` and `/update`
  - S6: Enforces server-side `MESH_MCP_INCLUDE_PRIVATE=0` lock (cannot be overridden by request
        `include_private=true`), blocks unauthorized `local_only` downgrades, and enforces per-agent scopes
  - S8: Supports `trust_tier`, `quarantined`, `include_quarantined`, and `/approve` endpoint
  - S9: Supports live-reloaded auth tokens and optional Unix domain socket (`MESH_UNIX_SOCKET`)
  - S11: Enforces `guard_ingest` and `guard_tags` on `/learn` and `/update` (including `content=None`)
  - N4: Enforces request body size limits (`_MAX_REQUEST_BODY_BYTES`) across all endpoints including `/mcp`,
        and prevents `agent_id` rotation from evicting other namespaces in `_MESH_REGISTRY`
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import contextvars
import hashlib
import hmac
import json
import logging
import os
import re
from .sqlite_runtime import sqlite3
import sys
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, NoReturn, Optional
from urllib.parse import urlparse

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from .audit import (
    _get_audit_db_path,
    ensure_secure_mesh_dir,
    get_mesh_dir,
    get_audit_log,
    reject_symlink,
    validate_namespace,
)
from .context_builder import build_context_bundle
from .daemon import get_or_create_auth_token, request_user_permission
from .envutil import env_bool, env_float, env_int, parse_strict_bool
from .execution import CapacityError, run_sync
from .group_commit import GroupAdmissionRejected
from .memory import (
    Mesh,
    _has_local_onnx_pair,
    get_engine_status,
    set_idle_unload_seconds,
    suggest_did_you_mean_namespaces,
    unload_model,
    warmup_model,
)
from .namespaces import _evict_namespace_caches
from .store import MeshStore, NamespaceLimitExceededError, NamespaceNotFoundError
from .version import VERSION

logger = logging.getLogger(__name__)

_LOCAL_ORIGIN_REGEX = r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$"
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
_ACTIVE_BIND_HOST = "127.0.0.1"

_VALID_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MAX_IDENTITY_BODY_BYTES = 1024
# Finding N4: Cap maximum HTTP request body size (256 KB) to prevent memory exhaustion on `/mcp` and `/learn`
_MAX_REQUEST_BODY_BYTES = 262144
_HTTP_REQUEST_SCOPE: contextvars.ContextVar[Any] = contextvars.ContextVar("mesh_http_request_scope", default=None)

# Whitelisted context keys accepted by `/mcp` (Issue H4).
# Security-sensitive keys (`enforce_agent_ownership`, auth/token/permission/privacy/sync flags)
# are never trusted from the request body and are derived strictly from server configuration.
_ALLOWED_MCP_CONTEXT_KEYS = {"workspace_root"}


def _is_private_locked_off() -> bool:
    """
    Finding S6: Return True when `MESH_MCP_INCLUDE_PRIVATE` is explicitly set to a falsy
    value (`0`, `false`, `no`) or `MESH_BLOCK_PRIVATE_OVERRIDE=1` is set on the server.
    When True, callers cannot override `include_private` to `True` via request parameters.
    """
    if env_bool("MESH_BLOCK_PRIVATE_OVERRIDE", False):
        return True
    raw = os.environ.get("MESH_MCP_INCLUDE_PRIVATE")
    if raw is not None and not parse_strict_bool(raw, default=False):
        return True
    return False


def _resolve_http_include_private(requested: Optional[bool]) -> bool:
    """
    Resolve `include_private` for HTTP routes (Finding S6):
      - If the server has locked off private access (`MESH_MCP_INCLUDE_PRIVATE=0`), always returns `False`.
      - If `requested is None`, defaults to `env_bool("MESH_MCP_INCLUDE_PRIVATE", False)`.
      - Otherwise returns `bool(requested)`.
    """
    if _is_private_locked_off():
        return False
    if requested is None:
        return env_bool("MESH_MCP_INCLUDE_PRIVATE", False)
    return bool(requested)


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


def verify_request_auth(request: Request) -> None:
    """
    Verify the local Mesh authentication token on protected endpoints using `hmac.compare_digest`[cite: 2].
    Can be overridden in unit tests via `app.dependency_overrides[verify_request_auth] = lambda: None`[cite: 2].
    """
    _scope = getattr(request, "scope", None)
    if isinstance(_scope, dict) and _scope.get("mesh_auth_verified") is True:
        return  # already authenticated by _SecurityASGIMiddleware for this very request
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


def _raise_http_error(exc: BaseException, endpoint: str) -> NoReturn:
    """
    Centralized exception-to-HTTPException mapper for HTTP routes (Issue H5)[cite: 2]:
      - HTTPException -> re-raised unchanged
      - NamespaceLimitExceededError -> 429
      - NamespaceNotFoundError / FileNotFoundError / missing object ValueError -> 404
      - Authorization / scope / read-only ValueError -> 403
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
        if "already exists" in msg_low or any(marker in msg_low for marker in (
            "different request", "memory revision changed", "memory incarnation changed",
            "incarnation or store generation changed",
        )):
            raise HTTPException(status_code=409, detail=msg) from exc
        if "not authorized" in msg_low or "read-only" in msg_low or any(marker in msg_low for marker in (
            "private memory access is disabled", "private task access is disabled",
            "requested object is unavailable or mutation is not permitted",
            "memory mutation is not permitted for this agent",
        )):
            raise HTTPException(status_code=403, detail=msg) from exc
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
    logger.exception("Unhandled error in HTTP endpoint '%s': %s", endpoint, exc)
    raise HTTPException(status_code=500, detail="Internal server error.") from exc


def shutdown_http_runtime() -> None:
    """Settle admitted work and release resources owned by this server runtime."""
    from .execution import close_executor
    from .operations import close_operation_brokers, evict_mesh_cache
    from .audit import stop_audit_worker
    from .store import _close_all_pooled_connections, flush_all_telemetry

    close_operation_brokers()
    close_executor()
    flush_all_telemetry()
    stop_audit_worker()
    evict_mesh_cache()
    evict_http_mesh_cache()
    module = sys.modules.get("mesh.mcp_server")
    if module is not None:
        module.evict_mcp_mesh_cache()
    _close_all_pooled_connections()


@asynccontextmanager
async def _http_lifespan(application: FastAPI):
    try:
        yield
    finally:
        await asyncio.to_thread(shutdown_http_runtime)


app = FastAPI(
    title="Mesh HTTP API",
    description="Local HTTP interface for the Mesh shared agent memory layer.",
    version=VERSION,
    lifespan=_http_lifespan,
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


_SEC_POLICY_CACHE: dict[tuple, tuple[frozenset, frozenset]] = {}


def _security_policy(has_auth_override: bool) -> tuple[frozenset, frozenset]:
    """(allowed_hosts, custom_hosts) for the current environment; memoised per distinct env tuple."""
    key = (
        os.environ.get("MESH_HTTP_HOST", _ACTIVE_BIND_HOST),
        os.environ.get("MESH_ALLOWED_HOSTS", ""),
        _ACTIVE_BIND_HOST,
        tuple(sorted(_ALLOWED_HOSTS)),
        has_auth_override,
    )
    hit = _SEC_POLICY_CACHE.get(key)
    if hit is not None:
        return hit
    configured_host = os.environ.get("MESH_HTTP_HOST", _ACTIVE_BIND_HOST).strip().lower()
    allowed_hosts = set(_ALLOWED_HOSTS)
    if configured_host not in ("0.0.0.0", "::", "[::]"):  # nosec B104
        allowed_hosts.add(configured_host)
    if _ACTIVE_BIND_HOST not in ("0.0.0.0", "::", "[::]"):  # nosec B104
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
    if len(_SEC_POLICY_CACHE) > 64:
        _SEC_POLICY_CACHE.clear()
    res = (frozenset(allowed_hosts), frozenset(custom_hosts))
    _SEC_POLICY_CACHE[key] = res
    return res


class _SecurityASGIMiddleware:
    """
    Pure-ASGI port of the former `@app.middleware("http")` security gate (same checks, same order,
    same responses) without BaseHTTPMiddleware's per-request task group / streaming wrapper:
    1. Validate `Host` to block DNS rebinding.
    2. Validate `Origin` (if present) to block cross-site requests.
    3. Enforce `Content-Length` <= `_MAX_REQUEST_BODY_BYTES` before the body is read (Finding N4).
    4. Verify the bearer token on everything except `/health`, `/identity` and `OPTIONS` preflights.
    A request that passed step 4 is marked in its own ASGI scope so the route-level
    `verify_request_auth` dependency does not repeat the token comparison.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def _serve(self, scope: Any, receive: Any, send: Any) -> None:
        from .memory import _PRIVATE_ACCESS_OVERRIDE

        scope_token = _HTTP_REQUEST_SCOPE.set(scope)
        path = scope.get("path", "")
        direct_mutation = scope.get("method") in ("POST", "PATCH", "DELETE") and (
            path in ("/learn", "/update", "/approve", "/forget", "/contradictions/resolve", "/tasks")
            or path.startswith(("/tasks/", "/memories/"))
        )
        private = _PRIVATE_ACCESS_OVERRIDE.get()
        if private is not False and (_is_private_locked_off() or direct_mutation):
            private = _resolve_http_include_private(None)
        private_token = _PRIVATE_ACCESS_OVERRIDE.set(private)
        try:
            await self.app(scope, receive, send)
        finally:
            _PRIVATE_ACCESS_OVERRIDE.reset(private_token)
            _HTTP_REQUEST_SCOPE.reset(scope_token)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        has_auth_override = verify_request_auth in app.dependency_overrides
        allowed_hosts, custom_hosts = _security_policy(has_auth_override)

        host_hdr = origin = content_len_hdr = None
        for k, v in scope.get("headers") or ():
            if k == b"host":
                host_hdr = v.decode("latin-1")
            elif k == b"origin":
                origin = v.decode("latin-1")
            elif k == b"content-length":
                content_len_hdr = v.decode("latin-1")

        req_host = _extract_hostname(host_hdr or "")
        if req_host and req_host not in allowed_hosts:
            await JSONResponse(
                status_code=403,
                content={"detail": f"Forbidden Host header '{req_host}' (DNS rebinding protection)."},
            )(scope, receive, send)
            return

        if origin:
            parsed_origin = urlparse(origin)
            origin_host = (parsed_origin.hostname or "").lower()
            is_valid_local = bool(re.match(_LOCAL_ORIGIN_REGEX, origin))
            is_valid_custom = origin_host in custom_hosts and parsed_origin.scheme in ("http", "https")
            if origin_host not in allowed_hosts or not (is_valid_local or is_valid_custom):
                await JSONResponse(
                    status_code=403,
                    content={"detail": "Forbidden cross-origin request."},
                )(scope, receive, send)
                return

        path = scope.get("path", "")
        if content_len_hdr and path != "/identity":
            try:
                if int(content_len_hdr) < 0:
                    raise ValueError("Negative Content-Length")
                if int(content_len_hdr) > _MAX_REQUEST_BODY_BYTES:
                    await JSONResponse(
                        status_code=413,
                        content={"detail": f"Request body exceeds maximum allowed size ({_MAX_REQUEST_BODY_BYTES} bytes)."},
                    )(scope, receive, send)
                    return
            except ValueError:
                await JSONResponse(status_code=400, content={"detail": "Invalid Content-Length header."})(
                    scope, receive, send
                )
                return

        received_bytes = 0
        body_limit = _MAX_IDENTITY_BODY_BYTES if path == "/identity" else _MAX_REQUEST_BODY_BYTES

        async def bounded_receive() -> Any:
            nonlocal received_bytes
            message = await receive()
            if message.get("type") == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > body_limit:
                    raise HTTPException(status_code=413, detail="Request body exceeds the size limit.")
            return message

        if scope.get("method") == "OPTIONS" or path in ("/health", "/identity", "/ready") or has_auth_override:
            await self._serve(scope, bounded_receive, send)
            return

        try:
            verify_request_auth(Request(scope))
        except HTTPException as exc:
            await JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})(scope, receive, send)
            return
        scope["mesh_auth_verified"] = True
        await self._serve(scope, bounded_receive, send)


class _AdmissionASGIMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app
        self._slots = threading.BoundedSemaphore(32)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "lifespan":
            from anyio.to_thread import current_default_thread_limiter
            limiter = current_default_thread_limiter()
            previous = limiter.total_tokens
            limiter.total_tokens = 4
            try:
                await self.app(scope, receive, send)
            finally:
                limiter.total_tokens = previous
            return
        if scope.get("type") != "http" or scope.get("path") in ("/health", "/ready", "/identity"):
            await self.app(scope, receive, send)
            return
        if not self._slots.acquire(blocking=False):
            await JSONResponse(status_code=503, content={"detail": "Mesh request capacity is full.", "not_admitted": True}, headers={"Retry-After": "1"})(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            self._slots.release()


app.add_middleware(_AdmissionASGIMiddleware)
app.add_middleware(_SecurityASGIMiddleware)




_MESH_LOCK = threading.RLock()
_MESH_REGISTRY: OrderedDict[tuple[Path, str, str], Mesh] = OrderedDict()
_MESH_REGISTRY_MAX = 64


def _get_registry_max() -> int:
    if os.environ.get("MESH_MAX_OPEN_NAMESPACES"):
        return env_int("MESH_MAX_OPEN_NAMESPACES", _MESH_REGISTRY_MAX, minimum=2)
    return max(2, int(_MESH_REGISTRY_MAX))


def evict_http_mesh_cache(namespace: Optional[str] = None, *, mesh_home: Optional[Path] = None) -> None:
    """Drop registry references for one bound store, or every store when unscoped."""
    namespace = validate_namespace(namespace) if namespace is not None else None
    root = (mesh_home or get_mesh_dir()).resolve() if namespace is not None or mesh_home is not None else None
    retired = []
    with _MESH_LOCK:
        for key in list(_MESH_REGISTRY):
            if (namespace is None or key[1] == namespace) and (root is None or key[0] == root):
                retired.append(_MESH_REGISTRY.pop(key))
    retired.clear()


def _is_cached_mesh_valid(mesh: Mesh, expected_ns: str, expected_root: Optional[Path] = None) -> bool:
    import stat

    try:
        path = mesh.store.db_path
        reject_symlink(path)
        status = path.stat()
        return (
            mesh.namespace == expected_ns
            and mesh.store.namespace == expected_ns
            and (expected_root is None or path.parent == expected_root)
            and stat.S_ISREG(status.st_mode)
            and status.st_size > 0
            and (not isinstance(mesh.store, MeshStore) or mesh.store._is_current_instance())
        )
    except OSError:
        return False


def get_mesh(
    namespace: Optional[str] = None,
    agent_id: Optional[str] = None,
    create: bool = True,
) -> Mesh:
    """Reuse a validated instance without serializing filesystem or constructor work."""
    from .audit import mesh_dir_context

    raw_ns = (namespace or os.environ.get("MESH_NAMESPACE", "shared")).strip() or "shared"
    ns = validate_namespace(raw_ns)
    scope = _HTTP_REQUEST_SCOPE.get() or {"type": "http", "state": {}}
    aid = _resolve_mcp_agent_id(Request(scope), agent_id)
    root = get_mesh_dir().resolve()
    key = (root, ns, aid)
    max_reg = _get_registry_max()
    created = None
    while True:
        with _MESH_LOCK:
            mesh = _MESH_REGISTRY.get(key)
        if mesh is not None:
            valid = _is_cached_mesh_valid(mesh, ns, root) and mesh.agent_id == aid
            with _MESH_LOCK:
                if _MESH_REGISTRY.get(key) is not mesh:
                    continue
                if valid:
                    if create:
                        mesh.store.create = True
                    _MESH_REGISTRY.move_to_end(key)
                    return mesh
                _MESH_REGISTRY.pop(key)
            if (
                mesh.namespace == ns and mesh.store.namespace == ns
                and mesh.agent_id == aid and mesh.store.db_path.parent == root
            ):
                with mesh_dir_context(root):
                    _evict_namespace_caches(ns, mesh.store.db_path)
        if created is None:
            with mesh_dir_context(root):
                created = Mesh(namespace=ns, agent_id=aid, create=create, trusted_local=False)
            if not _is_cached_mesh_valid(created, ns, root):
                raise NamespaceNotFoundError(f"Namespace '{ns}' changed while loading; retry the operation.")
        retired = []
        with _MESH_LOCK:
            if key in _MESH_REGISTRY:
                continue
            same_store = [item for item in _MESH_REGISTRY if item[:2] == key[:2]]
            per_store_cap = max(1, max_reg // 2)
            while len(same_store) >= per_store_cap:
                retired.append(_MESH_REGISTRY.pop(same_store.pop(0)))
            while len(_MESH_REGISTRY) >= max_reg:
                victim_key = next((item for item in _MESH_REGISTRY if item[:2] == key[:2]), None)
                if victim_key is None:
                    victim_key = next(iter(_MESH_REGISTRY))
                retired.append(_MESH_REGISTRY.pop(victim_key))
            _MESH_REGISTRY[key] = created
        # Store pools own their bounded resources; retiring an agent must not flush
        # or close a shared namespace still in use by another request or transport.
        retired.clear()
        return created


# --- Request Models ---


class MutationRequest(BaseModel):
    operation_id: Optional[str] = Field(default=None, min_length=1, max_length=128,
                                         pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class LearnRequest(MutationRequest):
    content: str = Field(..., max_length=65536)
    memory_type: Optional[str] = Field(default="fact", max_length=64)
    confidence: Optional[float] = 1.0
    tags: Optional[list[str]] = Field(default_factory=list, max_length=64)
    local_only: Optional[bool] = False
    ttl_days: Optional[float] = None
    namespace: Optional[str] = Field(default=None, max_length=64)
    agent_id: Optional[str] = Field(default=None, max_length=128)
    secret_ref: Optional[dict[str, Any] | str] = None
    vault_secrets: Optional[bool] = False
    quarantined: Optional[bool] = False
    wait_for_indexed: bool = False
    index_timeout: float = Field(default=5.0, ge=0, le=60, allow_inf_nan=False)

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        if v is not None:
            for item in v:
                if len(str(item)) > 128:
                    raise ValueError("Individual tag length cannot exceed 128 characters.")
        return v


class RecallRequest(BaseModel):
    query: str = Field(..., max_length=16384)
    count: Optional[int] = Field(default=5, ge=1, le=200)
    n: Optional[int] = Field(default=None, ge=1, le=200)
    memory_type: Optional[str] = Field(default=None, max_length=64)
    tags: Optional[list[str]] = Field(default=None, max_length=64)
    source_agent: Optional[str] = Field(default=None, max_length=128)
    min_similarity: Optional[float] = 0.0
    min_confidence: Optional[float] = 0.0
    include_stale: Optional[bool] = True
    include_private: Optional[bool] = None
    include_quarantined: Optional[bool] = False
    include_other_possible: Optional[bool] = True
    max_other_possible: Optional[int] = Field(default=5, ge=0, le=50)
    namespace: Optional[str] = Field(default=None, max_length=64)
    agent_id: Optional[str] = Field(default=None, max_length=128)

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        if v is not None:
            for item in v:
                if len(str(item)) > 128:
                    raise ValueError("Individual tag length cannot exceed 128 characters.")
        return v


class MemoryIdentityRequest(MutationRequest):
    expected_revision: Optional[int] = Field(default=None, strict=True, ge=1)
    expected_incarnation: Optional[str] = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    expected_store_generation: Optional[str] = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class UpdateRequest(MemoryIdentityRequest):
    memory_id: str = Field(..., min_length=1, max_length=128)
    content: Optional[str] = Field(default=None, max_length=65536)
    memory_type: Optional[str] = Field(default=None, max_length=64)
    confidence: Optional[float] = None
    tags: Optional[list[str]] = Field(default=None, max_length=64)
    local_only: Optional[bool] = None
    ttl_days: Optional[float] = None
    clear_ttl: Optional[bool] = False
    namespace: Optional[str] = Field(default=None, max_length=64)
    agent_id: Optional[str] = Field(default=None, max_length=128)
    secret_ref: Optional[dict[str, Any] | str] = None
    quarantined: Optional[bool] = None

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        if v is not None:
            for item in v:
                if len(str(item)) > 128:
                    raise ValueError("Individual tag length cannot exceed 128 characters.")
        return v


class ApproveRequest(MemoryIdentityRequest):
    memory_id: str = Field(..., min_length=1, max_length=128)
    namespace: Optional[str] = Field(default=None, max_length=64)
    agent_id: Optional[str] = Field(default=None, max_length=128)


class ForgetRequest(MemoryIdentityRequest):
    memory_id: str = Field(..., min_length=1, max_length=128)
    namespace: Optional[str] = Field(default=None, max_length=64)
    agent_id: Optional[str] = Field(default=None, max_length=128)


class ResolveContradictionRequest(BaseModel):
    contradiction_id: str = Field(..., min_length=1, max_length=128)
    namespace: Optional[str] = Field(default=None, max_length=64)
    agent_id: Optional[str] = Field(default=None, max_length=128)


class ContextRequest(BaseModel):
    query: Optional[str] = Field(default=None, max_length=16384)
    count: Optional[int] = Field(default=10, ge=1, le=200)
    max_memories: Optional[int] = Field(default=None, ge=1, le=200)
    n: Optional[int] = Field(default=None, ge=1, le=200)
    namespace: Optional[str] = Field(default=None, max_length=64)
    format: Optional[str] = Field(default="markdown", max_length=32)
    min_confidence: Optional[float] = 0.3
    memory_type: Optional[str] = Field(default=None, max_length=64)
    tags: Optional[list[str]] = Field(default=None, max_length=64)
    include_private: Optional[bool] = None
    include_quarantined: Optional[bool] = False

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        if v is not None:
            for item in v:
                if len(str(item)) > 128:
                    raise ValueError("Individual tag length cannot exceed 128 characters.")
        return v


class WarmupRequest(BaseModel):
    namespace: Optional[str] = Field(default=None, max_length=64)
    namespaces: Optional[list[str]] = Field(default=None, max_length=64)
    blocking: bool = False


class McpProxyRequest(BaseModel):
    name: Optional[str] = Field(default=None, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)
    default_namespace: Optional[str] = Field(default=None, max_length=64)
    agent_id: Optional[str] = Field(default=None, max_length=128)
    context: dict[str, Any] = Field(default_factory=dict)
    jsonrpc: Optional[str] = Field(default=None, max_length=16)
    id: Optional[Any] = None
    method: Optional[str] = Field(default=None, max_length=128)
    params: Optional[dict[str, Any]] = None


class TaskCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=512)
    description: Optional[str] = Field(default="", max_length=16384)
    kind: Optional[str] = Field(default="task", max_length=16)
    parent_id: Optional[str] = Field(default=None, max_length=128)
    priority: Optional[str] = Field(default="medium", max_length=16)
    tags: Optional[list[str]] = Field(default=None, max_length=64)
    local_only: Optional[bool] = False
    namespace: Optional[str] = Field(default=None, max_length=64)
    agent_id: Optional[str] = Field(default=None, max_length=128)


class TaskUpdateRequest(BaseModel):
    title: Optional[str] = Field(default=None, max_length=512)
    description: Optional[str] = Field(default=None, max_length=16384)
    status: Optional[str] = Field(default=None, max_length=32)
    priority: Optional[str] = Field(default=None, max_length=16)
    note: Optional[str] = Field(default=None, max_length=4096)
    namespace: Optional[str] = Field(default=None, max_length=64)
    agent_id: Optional[str] = Field(default=None, max_length=128)


class TaskCompleteRequest(BaseModel):
    summary: str = Field(..., min_length=1, max_length=4096)
    evidence: Optional[list[str]] = Field(default=None, max_length=64)
    note: Optional[str] = Field(default=None, max_length=4096)
    namespace: Optional[str] = Field(default=None, max_length=64)
    agent_id: Optional[str] = Field(default=None, max_length=128)


# --- Endpoints (Synchronous `def` so FastAPI runs them in a worker threadpool) ---


_DAEMON_READY_EVENT = threading.Event()


@app.get("/health")
async def health() -> dict[str, Any]:
    """Unauthenticated health check returning only status and version (no PID or engine details)."""
    return {
        "status": "ok",
        "version": VERSION,
    }


@app.get("/ready")
async def ready() -> dict[str, Any]:
    """
    Readiness probe.
    Returns 200 when the daemon has preloaded models and is ready to answer queries,
    or 503 Service Unavailable while warming up.
    """
    if not _DAEMON_READY_EVENT.is_set():
        raise HTTPException(status_code=503, detail="Daemon is warming up embedding models")
    return {
        "status": "ready",
        "version": VERSION,
    }


@app.api_route("/identity", methods=["GET", "POST"])
async def identity(request: Request, nonce: Optional[str] = None) -> dict[str, Any]:
    """
    Challenge-response authentication verification for the local Mesh daemon[cite: 2].
    Allows clients to cryptographically verify daemon identity before transmitting auth tokens[cite: 2].
    """
    raw_nonce: Optional[str] = None
    if nonce is not None:
        raw_nonce = nonce.strip()
    elif "nonce" in request.query_params:
        raw_nonce = (request.query_params.get("nonce") or "").strip()

    if raw_nonce is None and request.method == "POST":
        content_len_hdr = request.headers.get("content-length")
        if content_len_hdr:
            try:
                if int(content_len_hdr) > _MAX_IDENTITY_BODY_BYTES:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Identity request body exceeds {_MAX_IDENTITY_BODY_BYTES} bytes.",
                    )
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid Content-Length header.")
        raw_bytes = await request.body()
        if len(raw_bytes) > _MAX_IDENTITY_BODY_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"Identity request body exceeds {_MAX_IDENTITY_BODY_BYTES} bytes.",
            )
        if raw_bytes:
            try:
                body = json.loads(raw_bytes.decode("utf-8"))
            except Exception:
                raise HTTPException(status_code=400, detail="Malformed JSON body on /identity.")
            if isinstance(body, dict) and "nonce" in body:
                val = body.get("nonce")
                if not isinstance(val, str):
                    raise HTTPException(status_code=400, detail="Challenge nonce must be a string.")
                raw_nonce = val.strip()

    if raw_nonce is None and "x-mesh-nonce" in request.headers:
        raw_nonce = request.headers.get("x-mesh-nonce", "").strip()

    if not raw_nonce:
        raise HTTPException(status_code=400, detail="Missing challenge nonce.")

    if len(raw_nonce) > 128 or not _VALID_NONCE_RE.fullmatch(raw_nonce):
        raise HTTPException(
            status_code=400,
            detail="Invalid challenge nonce: must be 1-128 characters matching ^[A-Za-z0-9_-]+$.",
        )

    try:
        nonce_bytes = bytes.fromhex(raw_nonce)
    except ValueError:
        nonce_bytes = raw_nonce.encode("utf-8")

    token = get_or_create_auth_token()
    proof = hmac.new(token.encode("utf-8"), nonce_bytes, hashlib.sha256).hexdigest()
    from .client import store_identity
    store_id = store_identity(get_mesh_dir())
    scope_hmac = hmac.new(token.encode("utf-8"), (raw_nonce + "|" + store_id).encode("utf-8"), hashlib.sha256).hexdigest()
    from .operation_contract import OPERATION_RECEIPT_VERSION, receipt_capability_proof

    return {
        "status": "ok",
        "proof": proof,
        "store_id": store_id,
        "scope_hmac": scope_hmac,
        "operation_receipt_version": OPERATION_RECEIPT_VERSION,
        "receipt_hmac": receipt_capability_proof(token, raw_nonce, store_id),
        "pid": os.getpid(),
        "version": VERSION,
    }


@app.get("/engine", dependencies=[Depends(verify_request_auth)])
def engine_status() -> dict[str, Any]:
    """Return current embedding engine and idle-unload status[cite: 2]."""
    return {"status": "ok", "engine": get_engine_status()}


@app.get("/stats", dependencies=[Depends(verify_request_auth)])
def runtime_stats() -> dict[str, Any]:
    """Report bounded request aggregation and durable mutation delivery progress."""
    from .operations import get_group_commit_stats
    from .mutation_delivery import get_mutation_delivery_stats

    return {"status": "ok", "version": VERSION, "group_commit": get_group_commit_stats(),
            "mutation_delivery": get_mutation_delivery_stats()}


@app.post("/warmup", dependencies=[Depends(verify_request_auth)])
def warmup_endpoint(req: WarmupRequest) -> Any:
    """
    Warm the embedding model and heal pending embeddings across requested namespaces[cite: 2].
    """
    target_namespaces: list[str] = []
    if req.namespaces:
        target_namespaces.extend(str(n).strip() for n in req.namespaces if str(n).strip())
    if req.namespace and req.namespace.strip():
        if req.namespace.strip() not in target_namespaces:
            target_namespaces.append(req.namespace.strip())
    if not target_namespaces:
        default_ns = (os.environ.get("MESH_NAMESPACE") or "shared").strip() or "shared"
        target_namespaces.append(default_ns)

    if len(target_namespaces) == 1 and not req.namespaces:
        try:
            validate_namespace(target_namespaces[0])
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    per_namespace: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    succeeded = 0
    failed = 0

    for raw_ns in target_namespaces:
        try:
            clean_ns = validate_namespace(raw_ns)
            res = warmup_model(namespace=clean_ns, blocking=bool(req.blocking), assume_yes=False)
            if isinstance(res, dict) and res.get("status") == "error":
                failed += 1
                err_msg = str(res.get("error") or f"Warmup failed for namespace '{clean_ns}'.")
                per_namespace.append({"namespace": clean_ns, "status": "error", "error": err_msg})
                errors.append({"namespace": clean_ns, "error": err_msg})
            else:
                succeeded += 1
                ns_exists = bool(res.get("namespace_exists")) if isinstance(res, dict) else False
                per_namespace.append(
                    {"namespace": clean_ns, "status": "ok", "namespace_exists": ns_exists}
                )
        except Exception as exc:
            failed += 1
            logger.exception("Warmup failed for namespace '%s': %s", raw_ns, exc)
            err_summary = str(exc) if isinstance(exc, ValueError) else f"Failed warming namespace '{raw_ns}'."
            per_namespace.append({"namespace": raw_ns, "status": "error", "error": err_summary})
            errors.append({"namespace": raw_ns, "error": err_summary})

    total = len(target_namespaces)
    if failed == 0:
        overall_status = "ok"
        http_code = 200
    elif succeeded > 0:
        overall_status = "partial"
        http_code = 207
    else:
        overall_status = "error"
        http_code = 500

    payload = {
        "status": overall_status,
        "total": total,
        "succeeded": succeeded,
        "failed": failed,
        "namespaces": per_namespace,
        "errors": errors,
        "engine": get_engine_status(),
    }
    return JSONResponse(status_code=http_code, content=payload)


@app.post("/unload", dependencies=[Depends(verify_request_auth)])
def unload_engine() -> dict[str, Any]:
    """Explicitly unload the neural embedding model from RAM to free memory immediately[cite: 2]."""
    unloaded = unload_model()
    return {"status": "ok", "unloaded": unloaded, "engine": get_engine_status()}


def _sanitize_mcp_context(raw_context: Any) -> dict[str, Any]:
    """
    Whitelist allowed `/mcp` context keys (Issue H4)[cite: 2].
    Drops all security-sensitive keys (`enforce_agent_ownership`, auth/token/permission/privacy/sync flags)
    so ownership enforcement and permissions are derived strictly from server configuration[cite: 2].
    """
    if not isinstance(raw_context, dict):
        return {}
    sanitized: dict[str, Any] = {}
    for k, v in raw_context.items():
        if k in _ALLOWED_MCP_CONTEXT_KEYS and isinstance(v, str) and len(v) <= 1024:
            sanitized[k] = v
    return sanitized


def _resolve_mcp_agent_id(request: Request, caller_agent_id: Optional[str]) -> str:
    """
    Bind `agent_id` to an authenticated identity if configured (`request.state.agent_id`
    or `MESH_AUTHENTICATED_AGENT_ID`); otherwise treat `caller_agent_id` as an unverified label (Issue H4)[cite: 2].
    """
    state_agent = getattr(request.state, "agent_id", None) or getattr(
        request.state, "authenticated_agent_id", None
    )
    if isinstance(state_agent, str) and state_agent.strip():
        return re.sub(r"[\x00-\x1f\x7f]+", "", state_agent).strip()[:64] or "mcp-agent"
    env_bound = (os.environ.get("MESH_AUTHENTICATED_AGENT_ID") or "").strip()
    if env_bound:
        return re.sub(r"[\x00-\x1f\x7f]+", "", env_bound).strip()[:64] or "mcp-agent"
    raw = (
        caller_agent_id
        if (caller_agent_id is not None and str(caller_agent_id).strip())
        else (os.environ.get("MESH_AGENT_ID") or "mcp-agent")
    )
    return re.sub(r"[\x00-\x1f\x7f]+", "", str(raw)).strip()[:64] or "mcp-agent"


@app.post("/mcp", dependencies=[Depends(verify_request_auth)])
async def mcp_proxy(request: Request) -> Any:
    """
    Execute an MCP tool request in the shared daemon process (Issues H4, H6, N4)[cite: 2]:
      - Enforces `_MAX_REQUEST_BODY_BYTES` before parsing JSON (Finding N4)
      - Protocol/parse errors or unknown tools -> JSON-RPC `error` object
      - Tool execution failures -> MCP result with `isError: true` and explanatory `content` block
      - Whitelists `context` keys and derives `enforce_agent_ownership` from server configuration
    """
    from .mcp_server import _MCP_TOOLS, _execute_tool_sync

    try:
        raw_body = await request.body()
        if not raw_body:
            return JSONResponse(
                status_code=400,
                content={
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Parse error: empty request body."},
                    "detail": "Parse error: empty request body.",
                },
            )
        if len(raw_body) > _MAX_REQUEST_BODY_BYTES:
            return JSONResponse(
                status_code=413,
                content={
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32600,
                        "message": f"Request body exceeds maximum allowed size ({_MAX_REQUEST_BODY_BYTES} bytes).",
                    },
                    "detail": f"Request body exceeds maximum allowed size ({_MAX_REQUEST_BODY_BYTES} bytes).",
                },
            )
        try:
            body_data = json.loads(raw_body.decode("utf-8"))
        except Exception:
            return JSONResponse(
                status_code=400,
                content={
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Parse error: invalid JSON."},
                    "detail": "Parse error: invalid JSON.",
                },
            )
        if not isinstance(body_data, dict):
            return JSONResponse(
                status_code=400,
                content={
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Invalid Request: expected JSON object."},
                    "detail": "Invalid Request: expected JSON object.",
                },
            )

        req_id = body_data.get("id")
        raw_params = body_data.get("params")
        params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
        tool_name = str(body_data.get("name") or params.get("name") or "").strip()
        arguments = body_data.get("arguments") if "arguments" in body_data else params.get("arguments", {})
        default_ns = body_data.get("default_namespace") or params.get("default_namespace")
        raw_agent_id = body_data.get("agent_id") or params.get("agent_id")
        raw_context = body_data.get("context") if "context" in body_data else params.get("context", {})

        if not tool_name:
            return JSONResponse(
                status_code=400,
                content={
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32600, "message": "Invalid Request: missing tool 'name'."},
                    "detail": "Invalid Request: missing tool 'name'.",
                },
            )
        if not isinstance(arguments, dict):
            return JSONResponse(
                status_code=400,
                content={
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32602, "message": "Invalid params: 'arguments' must be an object."},
                    "detail": "Invalid params: 'arguments' must be an object.",
                },
            )

        known_tools = {t.name for t in _MCP_TOOLS}
        if tool_name not in known_tools:
            err_payload = {"error": f"Unknown tool: {tool_name}", "status": "error"}
            text_block = {"type": "text", "text": json.dumps(err_payload, indent=2)}
            return JSONResponse(
                status_code=200,
                content={
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "status": "error",
                    "isError": True,
                    "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
                    "content": [text_block],
                    "result": {"isError": True, "content": [text_block]},
                    "payload": err_payload,
                },
            )

        if default_ns:
            validate_namespace(str(default_ns))
        if arguments.get("namespace"):
            validate_namespace(str(arguments["namespace"]))

        expected_store = body_data.get("expected_store_id")
        if expected_store is not None:
            from .client import store_identity

            if not isinstance(expected_store, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_store):
                raise ValueError("Invalid expected store identity.")
            if not hmac.compare_digest(expected_store, store_identity(get_mesh_dir())):
                raise HTTPException(status_code=403, detail="Not authorized: the configured store identity differs from this endpoint.")

        safe_context = _sanitize_mcp_context(raw_context)
        bound_agent_id = _resolve_mcp_agent_id(request, raw_agent_id)
        authenticated = getattr(request.state, "agent_id", None) or getattr(request.state, "authenticated_agent_id", None) or os.environ.get("MESH_AUTHENTICATED_AGENT_ID")
        safe_context["approved_producer_scope"] = "principal:" + bound_agent_id if authenticated else "local-user"
        expected_scope = body_data.get("expected_producer_scope")
        if expected_scope is not None:
            from .operation_contract import validate_producer_scope

            validate_producer_scope(expected_scope)
            if expected_scope != safe_context["approved_producer_scope"]:
                raise HTTPException(status_code=403, detail="Not authorized: the authenticated producer scope differs from the reviewed profile.")

        client_policy = body_data.get("client_policy", {})
        if not isinstance(client_policy, dict) or set(client_policy) - {"include_private", "read_only", "enforce_agent_ownership"} or any(type(value) is not bool for value in client_policy.values()):
            raise ValueError("Invalid client policy restrictions.")
        if client_policy.get("include_private") is False:
            safe_context["private_locked_off"] = True
            arguments = {**arguments, "include_private": False}
        for flag in ("read_only", "enforce_agent_ownership"):
            if client_policy.get(flag) is True:
                safe_context[flag] = True
        operation_id = body_data["operation_id"] if "operation_id" in body_data else params.get("operation_id")
        if operation_id is not None:
            from .operation_contract import validate_operation_id
            validate_operation_id(operation_id)
            if "operation_id" in arguments and arguments["operation_id"] != operation_id:
                raise ValueError("Conflicting operation IDs.")
            arguments = {**arguments, "operation_id": operation_id}

        from .operations import execute_tool_async
        from .operation_contract import MUTATING_TOOLS
        if tool_name in MUTATING_TOOLS and arguments.get("operation_id") is None:
            import uuid

            arguments = {**arguments, "operation_id": uuid.uuid4().hex}
        from .operation_contract import UnknownOutcome

        try:
            if env_bool("MESH_GROUP_COMMIT", False):
                payload = await execute_tool_async(tool_name, arguments, default_namespace=default_ns,
                                                  agent_id=bound_agent_id, context=safe_context)
            else:
                payload = await run_sync(_execute_tool_sync, name=tool_name, arguments=arguments,
                                         default_namespace=default_ns, agent_id=bound_agent_id,
                                         allow_daemon_proxy=False, context=safe_context)
        except ValueError as exc:
            # Tool rejections are MCP results; request/authentication failures
            # above remain transport errors and cannot authorize a fallback.
            payload = {"status": "error", "error": str(exc),
                       "operation_id": getattr(exc, "operation_id", arguments.get("operation_id")),
                       "namespace": arguments.get("namespace") or default_ns}
        except UnknownOutcome as exc:
            payload = {"status": "unknown", "error": str(exc), "operation_id": exc.operation_id,
                       "namespace": arguments.get("namespace") or default_ns}
        try:
            return _mcp_result(req_id, payload)
        except Exception:
            if tool_name in MUTATING_TOOLS:
                return _mcp_result(req_id, {"status": "unknown", "error": str(UnknownOutcome(arguments["operation_id"])),
                                            "operation_id": arguments["operation_id"],
                                            "namespace": arguments.get("namespace") or default_ns})
            raise
    except (CapacityError, GroupAdmissionRejected) as e:
        return JSONResponse(status_code=503, content={"detail": str(e), "not_admitted": True}, headers={"Retry-After": "1"})
    except ValueError as e:
        _raise_http_error(e, "/mcp")
    except Exception as e:
        _raise_http_error(e, "/mcp")


def _mcp_result(request_id: Any, payload: Any) -> dict[str, Any]:
    is_error = isinstance(payload, dict) and (
        payload.get("status") in {"rejected", "error", "unknown", "busy", "unavailable"}
        or payload.get("error") is not None and payload.get("status") != "synchronized"
    )
    text_block = {"type": "text", "text": json.dumps(payload, indent=2, allow_nan=False)}
    return {"jsonrpc": "2.0", "id": request_id, "status": "error" if is_error else "ok", "isError": is_error,
            "content": [text_block], "result": {"isError": is_error, "content": [text_block]}, "payload": payload}


def _execute_rest_memory(name: str, arguments: dict[str, Any], operation_id: str,
                         namespace: Optional[str], caller_agent_id: Optional[str], endpoint: str) -> Any:
    """Use the same durable receipt and server policy for explicitly identified REST writes."""
    from .operations import execute_tool
    from .operation_contract import UnknownOutcome, validate_mutation_acknowledgement, validate_operation_id

    validate_operation_id(operation_id)
    raw_ns = (namespace or os.environ.get("MESH_NAMESPACE", "shared")).strip() or "shared"
    ns = validate_namespace(raw_ns)
    scope = _HTTP_REQUEST_SCOPE.get() or {"type": "http", "state": {}}
    request = Request(scope)
    actor = _resolve_mcp_agent_id(request, caller_agent_id)
    authenticated = (getattr(request.state, "agent_id", None)
                     or getattr(request.state, "authenticated_agent_id", None)
                     or os.environ.get("MESH_AUTHENTICATED_AGENT_ID"))
    producer = "principal:" + actor if authenticated else "local-user"
    private = _resolve_http_include_private(None)
    context = {"approved_producer_scope": producer, "private_locked_off": not private,
               "default_include_private": private}
    args = {**arguments, "namespace": ns, "include_private": private}
    try:
        payload = execute_tool(name, args, default_namespace=ns, agent_id=actor, operation_id=operation_id,
                               context=context, mesh_factory=get_mesh)
        validate_mutation_acknowledgement(payload, operation_id, ns, operation=name, producer_scope=producer)
        if payload.get("status") in {"error", "rejected"}:
            _raise_http_error(ValueError(payload.get("error") or "The memory operation was rejected."), endpoint)
    except (CapacityError, GroupAdmissionRejected) as exc:
        return JSONResponse(status_code=503, content={"status": "busy", "detail": str(exc),
                            "operation_id": operation_id, "namespace": ns, "not_admitted": True},
                            headers={"Retry-After": "1"})
    except UnknownOutcome as exc:
        return JSONResponse(status_code=503, content={"status": "unknown", "detail": str(exc),
                            "operation_id": operation_id, "namespace": ns})
    except Exception as exc:
        try:
            _raise_http_error(exc, endpoint)
        except HTTPException as mapped:
            return JSONResponse(status_code=mapped.status_code, content={"status": "error", "detail": mapped.detail,
                                "operation_id": operation_id, "namespace": ns})
    response = {"status": "ok", "result": payload, "operation_id": operation_id, "namespace": ns,
                "replayed": payload.get("replayed"), "receipt": payload.get("receipt")}
    if name == "mesh_forget":
        response["status"] = "ok" if payload.get("status") == "deleted" else "not_found"
        response["deleted"] = payload.get("memory_id") if payload.get("status") == "deleted" else None
    status_code = 404 if endpoint == "/memories/{memory_id}" and response["status"] == "not_found" else 200
    try:
        return JSONResponse(status_code=status_code, content=response)
    except Exception:
        # The effect and receipt are already committed; encoding a response is
        # never a validation rejection or permission to start a second write.
        return JSONResponse(status_code=503, content={"status": "unknown", "detail": str(UnknownOutcome(operation_id)),
                            "operation_id": operation_id, "namespace": ns})


def _rest_memory_arguments(request: MutationRequest) -> dict[str, Any]:
    return request.model_dump(exclude={"operation_id", "namespace", "agent_id"})


@app.post("/learn", dependencies=[Depends(verify_request_auth)])
def learn(req: LearnRequest) -> Any:
    """Store a memory. Runs `guard_ingest` & `guard_tags` and returns the memory ID and any contradiction warnings."""
    try:
        if req.operation_id is not None:
            arguments = _rest_memory_arguments(req)
            arguments["memory_type"] = req.memory_type or "fact"
            arguments["confidence"] = req.confidence if req.confidence is not None else 1.0
            arguments["tags"] = req.tags or []
            for flag in ("local_only", "vault_secrets", "quarantined"):
                arguments[flag] = bool(arguments[flag])
            return _execute_rest_memory("mesh_learn", arguments, req.operation_id, req.namespace, req.agent_id, "/learn")
        from .guards import guard_ingest, guard_tags

        raw_ns = (req.namespace or os.environ.get("MESH_NAMESPACE", "shared")).strip() or "shared"
        ns = validate_namespace(raw_ns)
        guard = guard_ingest(req.content, local_only=bool(req.local_only))
        if guard.rejected:
            reason_str = guard.reason or "Memory content rejected by ingestion guard."
            return JSONResponse(
                status_code=400,
                content={
                    "status": "rejected",
                    "error": reason_str,
                    "detail": reason_str,
                },
            )
        try:
            clean_tags, tags_priv, tag_warnings = guard_tags(
                req.tags or [],
                local_only=bool(guard.local_only),
            )
        except ValueError as tag_err:
            reason_str = str(tag_err)
            return JSONResponse(
                status_code=400,
                content={
                    "status": "rejected",
                    "error": reason_str,
                    "detail": reason_str,
                },
            )

        final_local = bool(guard.local_only or tags_priv)
        mesh = get_mesh(ns, req.agent_id, create=True)
        result = mesh.learn(
            content=guard.content,
            memory_type=req.memory_type or "fact",
            confidence=req.confidence if req.confidence is not None else 1.0,
            tags=clean_tags,
            local_only=final_local,
            ttl_days=req.ttl_days,
            secret_ref=req.secret_ref,
            vault_secrets=bool(req.vault_secrets),
            quarantined=bool(req.quarantined),
            trust_tier="agent",
            wait_for_indexed=req.wait_for_indexed,
            index_timeout=req.index_timeout,
        )
        all_warnings = tuple([*guard.warnings, *tag_warnings])
        if all_warnings:
            result["warnings"] = all_warnings
        return {"status": "ok", "result": result}
    except Exception as e:
        _raise_http_error(e, "/learn")


@app.post("/recall", dependencies=[Depends(verify_request_auth)])
def recall(req: RecallRequest) -> dict[str, Any]:
    """Retrieve memories relevant to a query using 5-stage hybrid search + peripheral snippets."""
    try:
        raw_ns = (req.namespace or os.environ.get("MESH_NAMESPACE", "shared")).strip() or "shared"
        ns = validate_namespace(raw_ns)
        try:
            mesh = get_mesh(ns, req.agent_id, create=False)
        except NamespaceNotFoundError:
            return {
                "status": "ok",
                "namespace": ns,
                "namespace_exists": False,
                "did_you_mean": suggest_did_you_mean_namespaces(ns),
                "results": [],
                "count": 0,
                "total_in_namespace": 0,
                "other_possible_memories": [],
                "filter_hint": None,
            }
        limit_n = req.n if req.n is not None else (req.count or 5)
        max_other = (req.max_other_possible or 5) if req.include_other_possible is not False else 0
        inc_priv = _resolve_http_include_private(req.include_private)
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
            include_quarantined=bool(req.include_quarantined),
        )
        return {
            "status": "ok",
            "namespace": ns,
            "namespace_exists": True,
            "results": bundle["results"],
            "count": bundle["count"],
            "total_in_namespace": bundle["total_in_namespace"],
            "other_possible_memories": bundle["other_possible_memories"],
            "filter_hint": bundle["filter_hint"],
        }
    except Exception as e:
        _raise_http_error(e, "/recall")


@app.post("/update", dependencies=[Depends(verify_request_auth)])
def update(req: UpdateRequest) -> Any:
    """Update an existing memory in-place by ID (supports clearing TTL via `clear_ttl=True`)."""
    try:
        if req.operation_id is not None:
            arguments = _rest_memory_arguments(req)
            arguments["clear_ttl"] = bool(req.clear_ttl)
            return _execute_rest_memory("mesh_update", arguments, req.operation_id, req.namespace, req.agent_id, "/update")
        from .guards import guard_ingest, guard_tags

        content = req.content
        local_only = req.local_only
        warnings: list[str] = []
        if content is not None:
            guard = guard_ingest(content, local_only=bool(req.local_only))
            if guard.rejected:
                reason_str = guard.reason or "Memory content rejected by ingestion guard."
                return JSONResponse(
                    status_code=400,
                    content={"status": "rejected", "error": reason_str, "detail": reason_str},
                )
            content = guard.content
            if guard.local_only:
                local_only = True
            warnings.extend(guard.warnings)

        clean_tags = req.tags
        if req.tags is not None:
            try:
                clean_tags, tags_priv, tag_warns = guard_tags(req.tags, local_only=bool(local_only))
            except ValueError as tag_err:
                reason_str = str(tag_err)
                return JSONResponse(
                    status_code=400,
                    content={"status": "rejected", "error": reason_str, "detail": reason_str},
                )
            if tags_priv:
                local_only = True
            warnings.extend(tag_warns)

        mesh = get_mesh(req.namespace, req.agent_id, create=False)
        result = mesh.update(
            memory_id=req.memory_id,
            content=content,
            memory_type=req.memory_type,
            confidence=req.confidence,
            tags=clean_tags,
            ttl_days=req.ttl_days,
            clear_ttl=bool(req.clear_ttl),
            local_only=local_only,
            secret_ref=req.secret_ref,
            quarantined=req.quarantined,
            expected_revision=req.expected_revision,
            expected_incarnation=req.expected_incarnation,
            expected_store_generation=req.expected_store_generation,
        )
        payload: dict[str, Any] = {"status": "ok", "result": result}
        if warnings:
            payload["warnings"] = tuple(warnings)
        return payload
    except Exception as e:
        _raise_http_error(e, "/update")


@app.post("/approve", dependencies=[Depends(verify_request_auth)])
def approve_memory_endpoint(req: ApproveRequest) -> dict[str, Any]:
    """Approve a quarantined memory by ID (Finding S8)."""
    try:
        if req.operation_id is not None:
            return _execute_rest_memory("mesh_approve", _rest_memory_arguments(req), req.operation_id,
                                        req.namespace, req.agent_id, "/approve")
        mesh = get_mesh(req.namespace, req.agent_id, create=False)
        result = mesh.approve(req.memory_id, expected_revision=req.expected_revision, expected_incarnation=req.expected_incarnation, expected_store_generation=req.expected_store_generation)
        return {"status": "ok", "result": result}
    except Exception as e:
        _raise_http_error(e, "/approve")


@app.post("/forget", dependencies=[Depends(verify_request_auth)])
def forget(req: ForgetRequest) -> dict[str, Any]:
    """Delete a memory by ID[cite: 2]."""
    try:
        if req.operation_id is not None:
            return _execute_rest_memory("mesh_forget", _rest_memory_arguments(req), req.operation_id,
                                        req.namespace, req.agent_id, "/forget")
        try:
            mesh = get_mesh(req.namespace, req.agent_id, create=False)
        except NamespaceNotFoundError:
            return {"status": "not_found", "deleted": None}
        deleted = mesh.forget(req.memory_id, expected_revision=req.expected_revision, expected_incarnation=req.expected_incarnation, expected_store_generation=req.expected_store_generation)
        return {
            "status": "ok" if deleted else "not_found",
            "deleted": req.memory_id if deleted else None,
        }
    except Exception as e:
        _raise_http_error(e, "/forget")


@app.delete("/memories/{memory_id}", dependencies=[Depends(verify_request_auth)])
def delete_memory_rest(
    memory_id: str,
    namespace: Optional[str] = None,
    agent_id: Optional[str] = None,
    expected_revision: Optional[int] = None,
    expected_incarnation: Optional[str] = None,
    expected_store_generation: Optional[str] = None,
    operation_id: Optional[str] = None,
) -> dict[str, Any]:
    """RESTful DELETE endpoint returning 404 when the namespace or memory does not exist (Issue H5)[cite: 2]."""
    try:
        if operation_id is not None:
            result = _execute_rest_memory("mesh_forget", {"memory_id": memory_id, "expected_revision": expected_revision,
                                          "expected_incarnation": expected_incarnation,
                                          "expected_store_generation": expected_store_generation},
                                          operation_id, namespace, agent_id, "/memories/{memory_id}")
            return result
        raw_ns = (namespace or os.environ.get("MESH_NAMESPACE", "shared")).strip() or "shared"
        ns = validate_namespace(raw_ns)
        mesh = get_mesh(ns, agent_id, create=False)
        deleted = mesh.forget(memory_id, expected_revision=expected_revision, expected_incarnation=expected_incarnation, expected_store_generation=expected_store_generation)
        if not deleted:
            raise HTTPException(
                status_code=404,
                detail=f"Memory ID '{memory_id}' not found in namespace '{ns}'.",
            )
        return {"status": "deleted", "deleted": memory_id, "namespace": ns}
    except Exception as e:
        _raise_http_error(e, "DELETE /memories/{memory_id}")


@app.get("/inspect", dependencies=[Depends(verify_request_auth)])
def inspect(
    namespace: Optional[str] = None,
    limit: int = 50,
    memory_type: Optional[str] = None,
    tag: Optional[str] = None,
    include_private: Optional[bool] = None,
    include_quarantined: bool = True,
    quarantined_only: bool = False,
) -> dict[str, Any]:
    """List memories in the namespace (excludes `local_only` private memories by default)[cite: 2]."""
    try:
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000.")
        inc_priv = _resolve_http_include_private(include_private)
        raw_ns = (namespace or os.environ.get("MESH_NAMESPACE", "shared")).strip() or "shared"
        ns = validate_namespace(raw_ns)
        try:
            mesh = get_mesh(ns, create=False)
        except NamespaceNotFoundError:
            return {
                "status": "ok",
                "namespace": ns,
                "namespace_exists": False,
                "did_you_mean": suggest_did_you_mean_namespaces(ns),
                "memories": [],
                "count": 0,
            }
        result = mesh.inspect(
            limit=limit,
            memory_type=memory_type,
            tag=tag,
            include_private=inc_priv,
            include_quarantined=include_quarantined,
            quarantined_only=quarantined_only,
        )
        return {
            "status": "ok",
            "namespace": ns,
            "namespace_exists": True,
            "memories": result,
            "count": len(result),
        }
    except Exception as e:
        _raise_http_error(e, "/inspect")


@app.get("/patterns", dependencies=[Depends(verify_request_auth)])
def patterns(
    namespace: Optional[str] = None,
    include_private: Optional[bool] = None,
) -> dict[str, Any]:
    """Return pattern analysis: top queries, memory gaps, unused and stale memories[cite: 2]."""
    try:
        raw_ns = (namespace or os.environ.get("MESH_NAMESPACE", "shared")).strip() or "shared"
        ns = validate_namespace(raw_ns)
        try:
            mesh = get_mesh(ns, create=False)
        except NamespaceNotFoundError:
            return {
                "status": "ok",
                "namespace": ns,
                "namespace_exists": False,
                "did_you_mean": suggest_did_you_mean_namespaces(ns),
                "patterns": {
                    "top_topics": [],
                    "memory_gaps": [],
                    "unused_memories": [],
                    "stale_memories": [],
                },
            }
        inc_priv = _resolve_http_include_private(include_private)
        result = mesh.patterns(include_private=inc_priv)
        return {
            "status": "ok",
            "namespace": ns,
            "namespace_exists": True,
            "patterns": result,
        }
    except Exception as e:
        _raise_http_error(e, "/patterns")


@app.get("/contradictions", dependencies=[Depends(verify_request_auth)])
def contradictions(
    namespace: Optional[str] = None,
    resolved: bool = False,
) -> dict[str, Any]:
    """Return unresolved (or resolved) contradictions in the namespace[cite: 2]."""
    try:
        raw_ns = (namespace or os.environ.get("MESH_NAMESPACE", "shared")).strip() or "shared"
        ns = validate_namespace(raw_ns)
        try:
            mesh = get_mesh(ns, create=False)
        except NamespaceNotFoundError:
            return {
                "status": "ok",
                "namespace": ns,
                "namespace_exists": False,
                "did_you_mean": suggest_did_you_mean_namespaces(ns),
                "contradictions": [],
                "count": 0,
            }
        items = mesh.contradictions(resolved=resolved)
        return {
            "status": "ok",
            "namespace": ns,
            "namespace_exists": True,
            "contradictions": items,
            "count": len(items),
        }
    except Exception as e:
        _raise_http_error(e, "/contradictions")


@app.post("/contradictions/resolve", dependencies=[Depends(verify_request_auth)])
def resolve_contradiction(req: ResolveContradictionRequest) -> dict[str, Any]:
    """Mark a contradiction as resolved[cite: 2]."""
    try:
        mesh = get_mesh(req.namespace, req.agent_id, create=False)
        mesh.resolve_contradiction(req.contradiction_id)
        return {"status": "ok", "resolved": req.contradiction_id}
    except Exception as e:
        _raise_http_error(e, "/contradictions/resolve")


@app.post("/context", dependencies=[Depends(verify_request_auth)])
def context(req: ContextRequest) -> dict[str, Any]:
    """
    Return memories formatted as a system prompt block using the unified `build_context_bundle()`[cite: 2].
    Excludes `local_only=True` and `quarantined=True` memories by default (Findings S6, S8).
    """
    try:
        raw_ns = (req.namespace or os.environ.get("MESH_NAMESPACE", "shared")).strip() or "shared"
        ns = validate_namespace(raw_ns)
        try:
            mesh = get_mesh(ns, "http-context", create=False)
        except NamespaceNotFoundError:
            return {
                "status": "ok",
                "namespace": ns,
                "namespace_exists": False,
                "did_you_mean": suggest_did_you_mean_namespaces(ns),
                "context": "",
                "memory_count": 0,
            }
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
            include_private=_resolve_http_include_private(req.include_private),
            include_quarantined=bool(req.include_quarantined),
            mesh_instance=mesh,
        )
        return {
            "status": "ok",
            "namespace": ns,
            "namespace_exists": bundle.get("namespace_exists", True),
            "context": bundle["context"],
            "memory_count": bundle["memory_count"],
        }
    except Exception as e:
        _raise_http_error(e, "/context")


@app.get("/audit", dependencies=[Depends(verify_request_auth)])
def audit(
    namespace: Optional[str] = None,
    limit: int = 50,
    action: str = "all",
) -> dict[str, Any]:
    """Return the audit trail: recent memory reads and writes[cite: 2]."""
    try:
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000.")
        ns = validate_namespace((namespace or os.environ.get("MESH_NAMESPACE", "shared")).strip() or "shared")
        db_file = ensure_secure_mesh_dir().resolve() / f"mesh_{ns}.db"
        audit_file = _get_audit_db_path(ns)
        entries = get_audit_log(namespace=ns, limit=limit, action_filter=action)
        if not db_file.exists() and not audit_file.exists():
            return {
                "status": "ok",
                "namespace": ns,
                "namespace_exists": False,
                "did_you_mean": suggest_did_you_mean_namespaces(ns),
                "entries": [],
            }
        return {"status": "ok", "namespace": ns, "namespace_exists": True, "entries": entries}
    except Exception as e:
        _raise_http_error(e, "/audit")


@app.get("/brief", dependencies=[Depends(verify_request_auth)])
def brief(
    namespace: Optional[str] = None,
    max_tokens: int = 1500,
    max_facts: int = 20,
    fact_offset: int = 0,
    include_private: Optional[bool] = None,
    agent_id: Optional[str] = None,
) -> dict[str, Any]:
    from .brief import empty_project_brief, finalize_project_brief, validate_brief_options

    ns = (namespace or os.environ.get("MESH_NAMESPACE", "shared")).strip() or "shared"
    try:
        ns = validate_namespace(ns)
        validate_brief_options(max_tokens, max_facts, fact_offset)
        priv = _resolve_http_include_private(include_private)
        mesh = get_mesh(namespace=ns, agent_id=agent_id, create=False)
        res = mesh.project_brief(max_tokens=max_tokens, max_facts=max_facts, fact_offset=fact_offset, include_private=priv)
        return finalize_project_brief({"status": "ok", **res})
    except NamespaceNotFoundError:
        return finalize_project_brief({
            "status": "ok",
            **empty_project_brief(ns, max_tokens, max_facts, fact_offset),
            "did_you_mean": suggest_did_you_mean_namespaces(ns),
        })
    except Exception as e:
        _raise_http_error(e, "/brief")


@app.get("/tasks", dependencies=[Depends(verify_request_auth)])
def list_tasks(
    namespace: Optional[str] = None,
    status: Optional[str] = None,
    kind: Optional[str] = None,
    parent_id: Optional[str] = None,
    limit: int = 50,
    include_private: Optional[bool] = None,
    agent_id: Optional[str] = None,
) -> dict[str, Any]:
    ns = (namespace or os.environ.get("MESH_NAMESPACE", "shared")).strip() or "shared"
    try:
        ns = validate_namespace(ns)
        priv = _resolve_http_include_private(include_private)
        mesh = get_mesh(namespace=ns, agent_id=agent_id, create=False)
        items = mesh.list_work_items(status=status, kind=kind, parent_id=parent_id, limit=limit, include_private=priv)
        return {"status": "ok", "namespace": ns, "tasks": items, "count": len(items)}
    except NamespaceNotFoundError:
        return {"status": "ok", "namespace": ns, "tasks": [], "count": 0}
    except Exception as e:
        _raise_http_error(e, "/tasks")


@app.post("/tasks", dependencies=[Depends(verify_request_auth)])
def create_task(req: TaskCreateRequest) -> dict[str, Any]:
    try:
        ns = validate_namespace((req.namespace or os.environ.get("MESH_NAMESPACE", "shared")).strip() or "shared")
        mesh = get_mesh(namespace=ns, agent_id=req.agent_id, create=True)
        res = mesh.create_work_item(
            title=req.title,
            description=req.description or "",
            kind=req.kind or "task",
            parent_id=req.parent_id,
            priority=req.priority or "medium",
            tags=req.tags,
            local_only=bool(req.local_only),
            include_private=_resolve_http_include_private(None),
        )
        return {"status": "ok", "namespace": ns, "task": res}
    except Exception as e:
        _raise_http_error(e, "/tasks")


@app.get("/tasks/{item_id}", dependencies=[Depends(verify_request_auth)])
def get_task(
    item_id: str,
    namespace: Optional[str] = None,
    include_private: Optional[bool] = None,
    agent_id: Optional[str] = None,
) -> dict[str, Any]:
    try:
        ns = validate_namespace((namespace or os.environ.get("MESH_NAMESPACE", "shared")).strip() or "shared")
        priv = _resolve_http_include_private(include_private)
        mesh = get_mesh(namespace=ns, agent_id=agent_id, create=False)
        item = mesh.get_work_item(item_id=item_id, include_private=priv)
        if item is None:
            raise HTTPException(status_code=404, detail=f"Task '{item_id}' not found.")
        events = mesh.get_work_events(item_id=item["id"])
        return {"status": "ok", "namespace": ns, "task": item, "events": events}
    except HTTPException:
        raise
    except Exception as e:
        _raise_http_error(e, f"/tasks/{item_id}")


@app.patch("/tasks/{item_id}", dependencies=[Depends(verify_request_auth)])
def update_task(item_id: str, req: TaskUpdateRequest) -> dict[str, Any]:
    try:
        ns = validate_namespace((req.namespace or os.environ.get("MESH_NAMESPACE", "shared")).strip() or "shared")
        mesh = get_mesh(namespace=ns, agent_id=req.agent_id, create=False)
        res = mesh.update_work_item(
            item_id=item_id,
            title=req.title,
            description=req.description,
            status=req.status,
            priority=req.priority,
            note=req.note,
            include_private=_resolve_http_include_private(None),
        )
        return {"status": "ok", "namespace": ns, "task": res}
    except Exception as e:
        _raise_http_error(e, f"/tasks/{item_id}")


@app.post("/tasks/{item_id}/complete", dependencies=[Depends(verify_request_auth)])
def complete_task(item_id: str, req: TaskCompleteRequest) -> dict[str, Any]:
    try:
        ns = validate_namespace((req.namespace or os.environ.get("MESH_NAMESPACE", "shared")).strip() or "shared")
        mesh = get_mesh(namespace=ns, agent_id=req.agent_id, create=False)
        res = mesh.complete_work_item(
            item_id=item_id,
            summary=req.summary,
            evidence=req.evidence,
            note=req.note,
            include_private=_resolve_http_include_private(None),
        )
        return {"status": "ok", "namespace": ns, "task": res}
    except Exception as e:
        _raise_http_error(e, f"/tasks/{item_id}/complete")


@app.get("/log", dependencies=[Depends(verify_request_auth)])
def changelog(
    namespace: Optional[str] = None,
    since: str = "7d",
    format: str = "json",
    include_private: Optional[bool] = None,
    agent_id: Optional[str] = None,
) -> dict[str, Any]:
    ns = (namespace or os.environ.get("MESH_NAMESPACE", "shared")).strip() or "shared"
    try:
        ns = validate_namespace(ns)
        priv = _resolve_http_include_private(include_private)
        mesh = get_mesh(namespace=ns, agent_id=agent_id, create=False)
        res = mesh.changelog(since=since, format=format, include_private=priv)
        return {"status": "ok", **res}
    except NamespaceNotFoundError:
        return {
            "status": "ok",
            "namespace": ns,
            "since": since,
            "completed_count": 0,
            "decisions_count": 0,
            "completed_items": [],
            "decisions": [],
            "changelog_markdown": f"# Project Changelog: {ns}\n\n*No activity recorded.*",
        }
    except Exception as e:
        _raise_http_error(e, "/log")


@app.get("/audit/events", dependencies=[Depends(verify_request_auth)])
def audit_events(
    since: Optional[str] = None,
    limit: int = 200,
    namespace: Optional[str] = None,
) -> dict[str, Any]:
    try:
        from .audit import export_content_free_audit

        events = export_content_free_audit(since=since, limit=limit, namespace=namespace)
        return {"status": "ok", "events": events, "count": len(events)}
    except Exception as e:
        _raise_http_error(e, "/audit/events")


def start(
    host: Optional[str] = None,
    port: Optional[int] = None,
    unix_socket: Optional[str] = None,
) -> None:
    """Entry point for: mesh-http[cite: 2]"""
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
        default=env_int("MESH_HTTP_PORT", 7701, minimum=1, maximum=65535),
        help="Port to listen on (default: 7701)",
    )
    parser.add_argument(
        "--unix-socket",
        default=os.environ.get("MESH_UNIX_SOCKET", "").strip() or None,
        help="Optional POSIX Unix domain socket path (mode 0600)",
    )
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=env_float("MESH_IDLE_UNLOAD_SECONDS", 180.0, minimum=0.0),
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
    actual_host = (host or args.host).strip()
    actual_port = int(port or args.port)
    actual_uds = (unix_socket or args.unix_socket or "").strip() or None
    _ACTIVE_BIND_HOST = actual_host.lower()
    os.environ["MESH_HTTP_HOST"] = actual_host

    env_allowed = os.environ.get("MESH_ALLOWED_HOSTS", "").strip()
    if not actual_uds and _ACTIVE_BIND_HOST not in ("127.0.0.1", "localhost", "::1", "[::1]"):
        if not env_allowed:
            err_msg = (
                f"Refusing to start on non-loopback bind '{actual_host}' without MESH_ALLOWED_HOSTS specified.\n"
                f"Set MESH_ALLOWED_HOSTS to comma-separated allowed hostnames/IPs to confirm.\n"
            )
            sys.stderr.write(err_msg)
            raise ValueError(err_msg)
        else:
            sys.stderr.write(
                f"[WARNING] Binding Mesh HTTP server to '{actual_host}' with MESH_ALLOWED_HOSTS={env_allowed}. "
                "Traffic is unencrypted plain HTTP protected only by bearer-token authentication.\n"
            )

    get_or_create_auth_token()
    set_idle_unload_seconds(args.idle_timeout)

    allow_auto_download = bool(args.yes or os.environ.get("MESH_ALLOW_DAEMON_DOWNLOAD") == "1")
    if not args.no_warmup:
        def _warmup_worker(assume_yes: bool) -> None:
            try:
                warmup_model(assume_yes=assume_yes)
            except Exception as exc:
                logger.warning("Daemon model warmup warning: %s", exc)
            finally:
                _DAEMON_READY_EVENT.set()

        if _has_local_onnx_pair() is None:
            from .memory import _get_onnx_model_dir

            if allow_auto_download or request_user_permission(
                f"download the ~90MB ONNX embedding model into {_get_onnx_model_dir()} and warm the HTTP server",
                assume_yes=allow_auto_download,
            ):
                threading.Thread(
                    target=_warmup_worker,
                    kwargs={"assume_yes": True},
                    daemon=True,
                ).start()
            else:
                _DAEMON_READY_EVENT.set()
        else:
            threading.Thread(
                target=_warmup_worker,
                kwargs={"assume_yes": False},
                daemon=True,
            ).start()
    else:
        _DAEMON_READY_EVENT.set()

    from . import ui

    if actual_uds and os.name != "nt":
        uds_path = Path(actual_uds).expanduser()
        ensure_secure_mesh_dir(uds_path.parent)
        reject_symlink(uds_path)
        if uds_path.exists():
            uds_path.unlink(missing_ok=True)
        ui.info(f"Mesh HTTP API listening on unix://{uds_path}", "Press Ctrl+C to stop.", stream=sys.stderr)
        uvicorn.run(app, uds=str(uds_path), log_level="warning")
    else:
        ui.info(f"Mesh HTTP API listening on http://{actual_host}:{actual_port}", "Press Ctrl+C to stop.", stream=sys.stderr)
        uvicorn.run(app, host=actual_host, port=actual_port, log_level="warning")


if __name__ == "__main__":
    start()

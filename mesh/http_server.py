r"""
mesh/http_server.py — High-speed HTTP REST API & Background Daemon Server for Mesh.

Runs on 127.0.0.1:7701 by default. Separate from the dashboard (port 7433).

Security hardening:
  - Strict localhost CORS with properly escaped IPv6 `\[::1\]` origin regex
  - Host & Origin header validation blocking DNS rebinding and cross-site attacks
    (`testserver` is rejected by default and accepted only when a test auth override is active;
    non-loopback binds require `MESH_ALLOWED_HOSTS` to be explicitly configured)
  - Per-user 256-bit Bearer / X-Mesh-Token authentication (`~/.mesh/auth_token`) compared via `hmac.compare_digest`
  - Unauthenticated `/identity` HMAC-SHA256 challenge-response endpoint so clients verify daemon
    identity before ever sending their bearer token (with bounded nonce length and charset)
  - Read-only routes open namespaces with `create=False` so typo'd namespaces never create `.db` files
  - Interactive `y/n` user permission prompts before starting background downloads or warmup workers
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import sys
import threading
from collections import OrderedDict
from typing import Any, Optional
from urllib.parse import urlparse

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from .audit import _get_audit_db_path, ensure_secure_mesh_dir, get_audit_log, validate_namespace
from .context_builder import build_context_bundle
from .daemon import get_or_create_auth_token, request_user_permission
from .envutil import env_bool, env_float, env_int
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
from .store import NamespaceLimitExceededError, NamespaceNotFoundError
from .version import VERSION

logger = logging.getLogger(__name__)

_LOCAL_ORIGIN_REGEX = r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$"
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
_ACTIVE_BIND_HOST = "127.0.0.1"

_VALID_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MAX_IDENTITY_BODY_BYTES = 1024

# Whitelisted context keys accepted by `/mcp` (Issue H4).
# Security-sensitive keys (`enforce_agent_ownership`, auth/token/permission/privacy/sync flags)
# are never trusted from the request body and are derived strictly from server configuration.
_ALLOWED_MCP_CONTEXT_KEYS = {"workspace_root"}


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
    Verify the local Mesh authentication token on protected endpoints using `hmac.compare_digest`.
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


def _raise_http_error(exc: BaseException, endpoint: str) -> None:
    """
    Centralized exception-to-HTTPException mapper for HTTP routes (Issue H5):
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
    logger.exception("Unhandled error in HTTP endpoint '%s': %s", endpoint, exc)
    raise HTTPException(status_code=500, detail="Internal server error.") from exc


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
    3. Verify `~/.mesh/auth_token` on all endpoints except `/health`, `/identity`, and `OPTIONS` preflights
       (unless overridden via `app.dependency_overrides[verify_request_auth]`).
    """
    configured_host = os.environ.get("MESH_HTTP_HOST", _ACTIVE_BIND_HOST).strip().lower()
    allowed_hosts = set(_ALLOWED_HOSTS)
    if configured_host not in ("0.0.0.0", "::", "[::]"):
        allowed_hosts.add(configured_host)
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

    # Only allow FastAPI TestClient's `testserver` Host header when a test auth override is active.
    has_auth_override = verify_request_auth in app.dependency_overrides
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

    if request.method == "OPTIONS" or request.url.path in ("/health", "/identity") or has_auth_override:
        return await call_next(request)

    try:
        verify_request_auth(request)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return await call_next(request)


_MESH_LOCK = threading.RLock()
_MESH_REGISTRY: OrderedDict[tuple[str, str], Mesh] = OrderedDict()
# `agent_id` and `namespace` are caller-supplied; bound the registry with LRU eviction
# and evict underlying store connections/vector caches for evicted namespaces (Issue H3).
_MESH_REGISTRY_MAX = 64


def _get_registry_max() -> int:
    if os.environ.get("MESH_MAX_OPEN_NAMESPACES"):
        return env_int("MESH_MAX_OPEN_NAMESPACES", _MESH_REGISTRY_MAX, minimum=2)
    return max(2, int(_MESH_REGISTRY_MAX))


def evict_http_mesh_cache(namespace: Optional[str] = None) -> None:
    """Evict cached `Mesh` instances for `namespace` (or all namespaces if None)."""
    with _MESH_LOCK:
        if namespace is None:
            _MESH_REGISTRY.clear()
            return
        stale_keys = [k for k in _MESH_REGISTRY if k[0] == namespace]
        for k in stale_keys:
            _MESH_REGISTRY.pop(k, None)


def _is_cached_mesh_valid(mesh: Mesh, expected_ns: str) -> bool:
    try:
        return (
            mesh.namespace == expected_ns
            and mesh.store.namespace == expected_ns
            and mesh.store.db_path.exists()
            and mesh.store.db_path.stat().st_size > 0
        )
    except OSError:
        return False


def get_mesh(
    namespace: Optional[str] = None,
    agent_id: Optional[str] = None,
    create: bool = True,
) -> Mesh:
    """
    Return a cached `Mesh` instance for the given namespace and agent_id.
    - When `create=False` and the namespace database does not exist, raises `NamespaceNotFoundError`
      without creating any `.db` file on disk.
    - Revalidates `db_path.exists()` and namespace metadata on lookup so out-of-band deletions
      are evicted and recreated according to `create` (Issue H7).
    - Evicts the least-recently-used namespace *other than the one serving the request* across
      store/vector/audit/context/HTTP caches when inserting beyond `_MESH_REGISTRY_MAX` (Issue H3).
    """
    raw_ns = (namespace or os.environ.get("MESH_NAMESPACE", "shared")).strip() or "shared"
    ns = validate_namespace(raw_ns)
    aid = (agent_id or os.environ.get("MESH_AGENT_ID", "http-client")).strip() or "http-client"
    key = (ns, aid)

    stale_to_clean: list[tuple[str, Any]] = []
    with _MESH_LOCK:
        mesh = _MESH_REGISTRY.get(key)
        if mesh is not None:
            if not _is_cached_mesh_valid(mesh, ns):
                _MESH_REGISTRY.pop(key, None)
                stale_to_clean.append((ns, mesh.store.db_path))
                mesh = None
            else:
                if create and not mesh.store.create:
                    mesh.store.create = True
                _MESH_REGISTRY.move_to_end(key)
                return mesh

    for ev_ns, ev_path in stale_to_clean:
        _evict_namespace_caches(ev_ns, ev_path)

    if not create:
        db_file = ensure_secure_mesh_dir().resolve() / f"mesh_{ns}.db"
        if not db_file.exists():
            raise NamespaceNotFoundError(f"Namespace '{ns}' does not exist.")

    evicted_namespaces: list[tuple[str, Any]] = []
    with _MESH_LOCK:
        mesh = _MESH_REGISTRY.get(key)
        if mesh is not None and _is_cached_mesh_valid(mesh, ns):
            if create and not mesh.store.create:
                mesh.store.create = True
            _MESH_REGISTRY.move_to_end(key)
            return mesh

        mesh = Mesh(namespace=ns, agent_id=aid, create=create)
        if mesh.store.db_path.exists() or create:
            max_reg = _get_registry_max()
            while len(_MESH_REGISTRY) >= max_reg:
                victim_key: Optional[tuple[str, str]] = None
                for cand_key in _MESH_REGISTRY:
                    if cand_key[0] != ns:
                        victim_key = cand_key
                        break
                if victim_key is None:
                    for cand_key in _MESH_REGISTRY:
                        if cand_key != key:
                            victim_key = cand_key
                            break
                if victim_key is None:
                    break
                victim_mesh = _MESH_REGISTRY.pop(victim_key, None)
                victim_ns = victim_key[0]
                if (
                    victim_mesh is not None
                    and victim_ns != ns
                    and not any(k[0] == victim_ns for k in _MESH_REGISTRY)
                ):
                    evicted_namespaces.append((victim_ns, victim_mesh.store.db_path))
            _MESH_REGISTRY[key] = mesh
            _MESH_REGISTRY.move_to_end(key)

    for ev_ns, ev_path in evicted_namespaces:
        _evict_namespace_caches(ev_ns, ev_path)
    return mesh


# --- Request Models ---


class LearnRequest(BaseModel):
    content: str = Field(..., max_length=65536)
    memory_type: Optional[str] = Field(default="fact", max_length=64)
    confidence: Optional[float] = 1.0
    tags: Optional[list[str]] = Field(default_factory=list, max_length=64)
    local_only: Optional[bool] = False
    ttl_days: Optional[float] = None
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
    # None means "not specified": the MESH_MCP_INCLUDE_PRIVATE default applies.
    include_private: Optional[bool] = None
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


class UpdateRequest(BaseModel):
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

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        if v is not None:
            for item in v:
                if len(str(item)) > 128:
                    raise ValueError("Individual tag length cannot exceed 128 characters.")
        return v


class ForgetRequest(BaseModel):
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
    format: Optional[str] = Field(default="markdown", max_length=32)  # "markdown" | "plain" | "xml" | "json"
    min_confidence: Optional[float] = 0.3
    memory_type: Optional[str] = Field(default=None, max_length=64)
    tags: Optional[list[str]] = Field(default=None, max_length=64)
    # None means "not specified": the MESH_MCP_INCLUDE_PRIVATE default applies.
    include_private: Optional[bool] = None

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


# --- Endpoints (Synchronous `def` so FastAPI runs them in a worker threadpool) ---


@app.get("/health")
def health() -> dict[str, Any]:
    """Unauthenticated health check returning only status and version (no PID or engine details)."""
    return {
        "status": "ok",
        "version": VERSION,
    }


@app.api_route("/identity", methods=["GET", "POST"])
async def identity(request: Request, nonce: Optional[str] = None) -> dict[str, Any]:
    """
    Challenge-response authentication verification for the local Mesh daemon.
    Allows clients to cryptographically verify daemon identity before transmitting auth tokens.
    Enforces strict length (1..128) and charset (`[A-Za-z0-9_-]`) bounds on `nonce` before HMAC work (Issue H9).
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
    return {
        "status": "ok",
        "proof": proof,
        "pid": os.getpid(),
        "version": VERSION,
    }


@app.get("/engine", dependencies=[Depends(verify_request_auth)])
def engine_status() -> dict[str, Any]:
    """Return current embedding engine and idle-unload status."""
    return {"status": "ok", "engine": get_engine_status()}


@app.post("/warmup", dependencies=[Depends(verify_request_auth)])
def warmup_endpoint(req: WarmupRequest) -> Any:
    """
    Warm the embedding model and heal pending embeddings across requested namespaces.
    Aggregates per-namespace results and returns `ok` (200), `partial` (207), or `error` (500)
    with counts and error summaries (Issue H8).
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

    # If a single explicit namespace fails basic validation, return 400 directly when not a batch
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
    """Explicitly unload the neural embedding model from RAM to free memory immediately."""
    unloaded = unload_model()
    return {"status": "ok", "unloaded": unloaded, "engine": get_engine_status()}


def _sanitize_mcp_context(raw_context: Any) -> dict[str, Any]:
    """
    Whitelist allowed `/mcp` context keys (Issue H4).
    Drops all security-sensitive keys (`enforce_agent_ownership`, auth/token/permission/privacy/sync flags)
    so ownership enforcement and permissions are derived strictly from server configuration.
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
    or `MESH_AUTHENTICATED_AGENT_ID`); otherwise treat `caller_agent_id` as an unverified label (Issue H4).
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
    Execute an MCP tool request in the shared daemon process (Issues H4 & H6):
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
        params = body_data.get("params") if isinstance(body_data.get("params"), dict) else {}
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

        safe_context = _sanitize_mcp_context(raw_context)
        bound_agent_id = _resolve_mcp_agent_id(request, raw_agent_id)

        payload = _execute_tool_sync(
            name=tool_name,
            arguments=arguments,
            default_namespace=default_ns,
            agent_id=bound_agent_id,
            allow_daemon_proxy=False,
            context=safe_context,
        )

        is_tool_error = False
        if isinstance(payload, dict):
            if payload.get("status") in ("rejected", "error") or (
                payload.get("error") is not None and payload.get("status") != "synchronized"
            ):
                is_tool_error = True

        text_block = {"type": "text", "text": json.dumps(payload, indent=2)}
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "status": "error" if is_tool_error else "ok",
            "isError": is_tool_error,
            "content": [text_block],
            "result": {
                "isError": is_tool_error,
                "content": [text_block],
            },
            "payload": payload,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _raise_http_error(e, "/mcp")


@app.post("/learn", dependencies=[Depends(verify_request_auth)])
def learn(req: LearnRequest) -> Any:
    """Store a memory. Runs `guard_ingest` and returns the memory ID and any contradiction warnings."""
    try:
        from .guards import guard_ingest

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
            mesh = get_mesh(ns, req.agent_id, create=True)
            result = mesh.learn(
                content=guard.content,
                memory_type=req.memory_type or "fact",
                confidence=req.confidence if req.confidence is not None else 1.0,
                tags=req.tags or [],
                local_only=bool(guard.local_only),
                ttl_days=req.ttl_days,
            )
        except (NamespaceNotFoundError, sqlite3.OperationalError):
            evict_http_mesh_cache(ns)
            _evict_namespace_caches(ns)
            mesh = get_mesh(ns, req.agent_id, create=True)
            result = mesh.learn(
                content=guard.content,
                memory_type=req.memory_type or "fact",
                confidence=req.confidence if req.confidence is not None else 1.0,
                tags=req.tags or [],
                local_only=bool(guard.local_only),
                ttl_days=req.ttl_days,
            )
        if guard.warnings:
            result["warnings"] = guard.warnings
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
        inc_priv = (
            env_bool("MESH_MCP_INCLUDE_PRIVATE", False)
            if req.include_private is None
            else bool(req.include_private)
        )
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
        from .guards import guard_ingest

        content = req.content
        local_only = req.local_only
        warnings: tuple[str, ...] = ()
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
            warnings = guard.warnings

        mesh = get_mesh(req.namespace, req.agent_id, create=False)
        result = mesh.update(
            memory_id=req.memory_id,
            content=content,
            memory_type=req.memory_type,
            confidence=req.confidence,
            tags=req.tags,
            ttl_days=req.ttl_days,
            clear_ttl=bool(req.clear_ttl),
            local_only=local_only,
        )
        payload: dict[str, Any] = {"status": "ok", "result": result}
        if warnings:
            payload["warnings"] = warnings
        return payload
    except Exception as e:
        _raise_http_error(e, "/update")


@app.post("/forget", dependencies=[Depends(verify_request_auth)])
def forget(req: ForgetRequest) -> dict[str, Any]:
    """Delete a memory by ID."""
    try:
        try:
            mesh = get_mesh(req.namespace, req.agent_id, create=False)
        except NamespaceNotFoundError:
            return {"status": "not_found", "deleted": None}
        deleted = mesh.forget(req.memory_id)
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
) -> dict[str, Any]:
    """RESTful DELETE endpoint returning 404 when the namespace or memory does not exist (Issue H5)."""
    try:
        raw_ns = (namespace or os.environ.get("MESH_NAMESPACE", "shared")).strip() or "shared"
        ns = validate_namespace(raw_ns)
        mesh = get_mesh(ns, agent_id, create=False)
        deleted = mesh.forget(memory_id)
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
) -> dict[str, Any]:
    """List memories in the namespace (excludes `local_only` private memories by default)."""
    try:
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000.")
        inc_priv = (
            env_bool("MESH_MCP_INCLUDE_PRIVATE", False)
            if include_private is None
            else bool(include_private)
        )
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
    """Return pattern analysis: top queries, memory gaps, unused and stale memories."""
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
        inc_priv = (
            env_bool("MESH_MCP_INCLUDE_PRIVATE", False)
            if include_private is None
            else bool(include_private)
        )
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
    """Return unresolved (or resolved) contradictions in the namespace."""
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
    """Mark a contradiction as resolved."""
    try:
        mesh = get_mesh(req.namespace, req.agent_id, create=False)
        mesh.resolve_contradiction(req.contradiction_id)
        return {"status": "ok", "resolved": req.contradiction_id}
    except Exception as e:
        _raise_http_error(e, "/contradictions/resolve")


@app.post("/context", dependencies=[Depends(verify_request_auth)])
def context(req: ContextRequest) -> dict[str, Any]:
    """
    Return memories formatted as a system prompt block using the unified `build_context_bundle()`.
    Excludes `local_only=True` private memories by default unless `include_private=True`.
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
            include_private=(
                env_bool("MESH_MCP_INCLUDE_PRIVATE", False)
                if req.include_private is None
                else bool(req.include_private)
            ),
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
    """Return the audit trail: recent memory reads and writes."""
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


def start(host: Optional[str] = None, port: Optional[int] = None) -> None:
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
        default=env_int("MESH_HTTP_PORT", 7701, minimum=1, maximum=65535),
        help="Port to listen on (default: 7701)",
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
    _ACTIVE_BIND_HOST = actual_host.lower()
    os.environ["MESH_HTTP_HOST"] = actual_host

    env_allowed = os.environ.get("MESH_ALLOWED_HOSTS", "").strip()
    if _ACTIVE_BIND_HOST not in ("127.0.0.1", "localhost", "::1", "[::1]"):
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

    from . import ui

    ui.info(f"Mesh HTTP API listening on http://{actual_host}:{actual_port}", "Press Ctrl+C to stop.", stream=sys.stderr)
    uvicorn.run(app, host=actual_host, port=actual_port, log_level="warning")


if __name__ == "__main__":
    start()
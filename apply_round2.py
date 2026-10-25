#!/usr/bin/env python3
"""
apply_round2.py - Mesh latency patches, round 2 (extends apply_mesh_patches.py; do not re-apply round 1).

Usage (from the repo root that contains mesh/):
    python apply_round2.py            # apply
    python apply_round2.py --check    # dry run
    python apply_round2.py --revert   # restore *.pre_round2 backups

Each edit is an exact-text replacement. `new in text` is tested BEFORE `old`, so edits whose
new text contains the old text are detected as already applied. The script aborts without
writing anything if an anchor is missing or ambiguous. CRLF files stay CRLF.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

PATCHES: list[tuple[str, str, str]] = []
# (path, old) -> earlier `new` variants this script (or an older copy of it) may have left in a tree
PREV: dict[tuple[str, str], tuple[str, ...]] = {}


def P(path: str, old: str, new: str, prev: tuple[str, ...] = ()) -> None:
    PATCHES.append((path, old, new))
    if prev:
        PREV[(path, old)] = tuple(prev)


# ============================================================================
# mesh/store.py
# ============================================================================
# 1. _get_db_stamp: 4 stat calls -> 2 (exists()+stat() collapsed into one stat each).
P("mesh/store.py",
r'''def _get_db_stamp(db_path: Path) -> Optional[DbStamp]:
    if not db_path.exists():
        return None
    try:
        st = db_path.stat()
        wal_path = Path(f"{db_path}-wal")
        if wal_path.exists():
            try:
                wst = wal_path.stat()
                return (st.st_mtime_ns, st.st_size, wst.st_mtime_ns, wst.st_size)
            except OSError:
                pass
        return (st.st_mtime_ns, st.st_size, 0, 0)
    except OSError:
        return None
''',
r'''def _get_db_stamp(db_path: Path) -> Optional[DbStamp]:
    # One stat per file (the old exists()+stat() pair doubled the syscalls; on Windows each
    # one is a CreateFile round-trip that Defender may scan).
    try:
        st = os.stat(db_path)
    except OSError:
        return None
    try:
        wst = os.stat(f"{db_path}-wal")
        return (st.st_mtime_ns, st.st_size, wst.st_mtime_ns, wst.st_size)
    except OSError:
        return (st.st_mtime_ns, st.st_size, 0, 0)
''')

# 2. memoised resolve() of the db path (TTL = MESH_PATH_RECHECK_SECONDS, shared with audit.py).
P("mesh/store.py",
r'''from .envutil import env_int
from .guards import guard_ingest, guard_tags
''',
r'''from .audit import _path_recheck_ttl
from .envutil import env_int
from .guards import guard_ingest, guard_tags
''')

P("mesh/store.py",
r'''    def _maybe_lazy_flush_telemetry(self) -> None:
        resolved = self.db_path.resolve()
''',
r'''    def _resolved_db_path(self) -> Path:
        """`self.db_path.resolve()` memoised per store for the path-trust TTL."""
        ttl = _path_recheck_ttl()
        if ttl > 0.0:
            ent = self.__dict__.get("_rdb_cache")
            if ent is not None and (time.monotonic() - ent[0]) < ttl:
                return ent[1]
        resolved = self.db_path.resolve()
        if ttl > 0.0:
            self.__dict__["_rdb_cache"] = (time.monotonic(), resolved)
        return resolved

    def _maybe_lazy_flush_telemetry(self) -> None:
        resolved = self._resolved_db_path()
''')

P("mesh/store.py",
r'''        resolved = self.db_path.resolve()

        with _CACHE_LOCK:
            cached = _VECTOR_CACHE.get((self.db_path, self.namespace))
''',
r'''        resolved = self._resolved_db_path()

        with _CACHE_LOCK:
            cached = _VECTOR_CACHE.get((self.db_path, self.namespace))
''')

# ============================================================================
# mesh/crypto.py
# ============================================================================
# 3. is_encryption_enabled: was ensure_secure_mesh_dir().resolve() + exists() (~6 lstat + 2 stat per
#    call, three calls per learn). Now: one reject_symlink on the mesh dir (TTL-cached, one lstat)
#    + one stat of the marker under the memoised resolved dir. Same answer, same symlink refusal.
P("mesh/crypto.py",
r'''    try:
        return _encryption_marker_path().exists()
    except OSError:
        return False


def enable_encryption_at_rest(''',
r'''    global _ENC_MEMO
    try:
        from .audit import cached_resolved_mesh_dir, get_mesh_dir, reject_symlink

        _mdir = os.fspath(get_mesh_dir())
        _now = time.monotonic()
        _memo = _ENC_MEMO
        if _memo is not None and _memo[0] == _mdir and (_now - _memo[1]) < _enc_memo_seconds():
            return _memo[2]
        reject_symlink(get_mesh_dir())
        os.stat(os.path.join(os.fspath(cached_resolved_mesh_dir()), ".encryption_enabled"))
        _ENC_MEMO = (_mdir, _now, True)
        return True
    except OSError:
        _ENC_MEMO = (_mdir, _now, False)
        return False


_ENC_MEMO: Optional[tuple[str, float, bool]] = None


def _enc_memo_seconds() -> float:
    raw = os.environ.get("MESH_FS_MEMO_MS")
    try:
        return max(0.0, float(raw) / 1000.0) if raw is not None else 0.01
    except ValueError:
        return 0.01


def _invalidate_enc_memo() -> None:
    global _ENC_MEMO
    _ENC_MEMO = None


def enable_encryption_at_rest(''',
prev=(r'''    try:
        from .audit import cached_resolved_mesh_dir, get_mesh_dir, reject_symlink

        reject_symlink(get_mesh_dir())
        os.stat(os.path.join(os.fspath(cached_resolved_mesh_dir()), ".encryption_enabled"))
        return True
    except OSError:
        return False


def enable_encryption_at_rest(''',))

# ============================================================================
# mesh/memory.py
# ============================================================================
# 4. _has_local_onnx_pair: two stat() calls on every embed (6 per learn). Memoise for a short TTL.
P("mesh/memory.py",
r'''def _has_local_onnx_pair() -> Optional[tuple[Path, Path]]:
    global _LOCAL_PAIR_CACHE
    model_dir = _get_onnx_model_dir()
''',
r'''_PAIR_TTL_CACHE: Optional[tuple[Path, float, Optional[tuple[Path, Path]]]] = None


def _pair_recheck_seconds() -> float:
    raw = os.environ.get("MESH_MODEL_PAIR_RECHECK_SECONDS")
    if raw is None:
        return 1.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 1.0


def _has_local_onnx_pair() -> Optional[tuple[Path, Path]]:
    """Cheap front for _has_local_onnx_pair_impl: re-stat the model files at most once per TTL."""
    global _PAIR_TTL_CACHE
    ttl = _pair_recheck_seconds()
    if ttl <= 0.0:
        return _has_local_onnx_pair_impl()
    model_dir = _get_onnx_model_dir()
    ent = _PAIR_TTL_CACHE
    now = time.monotonic()
    if ent is not None and ent[0] == model_dir and (now - ent[1]) < ttl:
        return ent[2]
    res = _has_local_onnx_pair_impl()
    # Only a FOUND pair is memoised: a missing pair costs one failed stat and must be noticed the
    # moment the background download worker installs it.
    _PAIR_TTL_CACHE = (model_dir, now, res) if res is not None else None
    return res


def _has_local_onnx_pair_impl() -> Optional[tuple[Path, Path]]:
    global _LOCAL_PAIR_CACHE
    model_dir = _get_onnx_model_dir()
''')


# 5. Lexical scorer: two regex operations per (query token x candidate) and one pattern build per
#    candidate were re-done on every row. Both depend only on the query -> memoise (exact same output).
P("mesh/store.py",
r"""            tok_compact = re.sub(r"[^a-z0-9]", "", tok)
""",
r"""            tok_compact = _TOK_COMPACT_CACHE.get(tok)
            if tok_compact is None:
                tok_compact = re.sub(r"[^a-z0-9]", "", tok)
                if len(_TOK_COMPACT_CACHE) > 4096:
                    _TOK_COMPACT_CACHE.clear()
                _TOK_COMPACT_CACHE[tok] = tok_compact
""")

P("mesh/store.py",
r"""            if len(q_norm_phrase) >= 4 and re.search(r"\b" + re.escape(q_norm_phrase) + r"\b", doc_norm):
""",
r"""            if len(q_norm_phrase) >= 4 and _phrase_pattern(q_norm_phrase).search(doc_norm):
""")

P("mesh/store.py",
r"""def _get_db_stamp(db_path: Path) -> Optional[DbStamp]:
""",
r"""_TOK_COMPACT_CACHE: dict[str, str] = {}
_PHRASE_PATTERN_CACHE: dict[str, "re.Pattern[str]"] = {}


def _phrase_pattern(phrase: str) -> "re.Pattern[str]":
    pat = _PHRASE_PATTERN_CACHE.get(phrase)
    if pat is None:
        pat = re.compile(r"\b" + re.escape(phrase) + r"\b")
        if len(_PHRASE_PATTERN_CACHE) > 512:
            _PHRASE_PATTERN_CACHE.clear()
        _PHRASE_PATTERN_CACHE[phrase] = pat
    return pat


def _get_db_stamp(db_path: Path) -> Optional[DbStamp]:
""")

# ============================================================================
# mesh/cli.py
# ============================================================================
# 6. `mesh --version` / `python -m mesh.cli --version` answered before argparse/sqlite3/urllib/ssl/ui/audit
#    are imported (the printed text is identical to the one in _main()).
P("mesh/cli.py",
r"""from __future__ import annotations

import argparse
import difflib
""",
r"""from __future__ import annotations

import sys as _fast_sys

if __name__ == "__main__" and len(_fast_sys.argv) == 2 and _fast_sys.argv[1] in ("-v", "--version", "version"):
    from .version import VERSION as _FAST_VERSION

    print(f"mesh {_FAST_VERSION}")
    raise SystemExit(0)

import argparse
import difflib
""")


# 7. HTTP: BaseHTTPMiddleware -> pure ASGI security gate (identical checks), policy memoised,
#    and the per-route token check is skipped for requests the gate already authenticated.
P("mesh/http_server.py",
r'''@app.middleware("http")
async def _security_middleware(request: Request, call_next: Any) -> Any:
    """
    1. Validate `Host` header to block DNS rebinding attacks[cite: 2].
    2. Validate `Origin` header (if present) to block cross-site requests from arbitrary websites[cite: 2].
    3. Enforce `Content-Length` bound (`_MAX_REQUEST_BODY_BYTES`) before reading body (Finding N4).
    4. Verify `~/.mesh/auth_token` on all endpoints except `/health`, `/identity`, and `OPTIONS` preflights[cite: 2].
    """
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

    # Finding N4: Reject oversized Content-Length upfront across all routes
    content_len_hdr = request.headers.get("content-length")
    if content_len_hdr and request.url.path != "/identity":
        try:
            if int(content_len_hdr) > _MAX_REQUEST_BODY_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"detail": f"Request body exceeds maximum allowed size ({_MAX_REQUEST_BODY_BYTES} bytes)."},
                )
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length header."})

    if request.method == "OPTIONS" or request.url.path in ("/health", "/identity") or has_auth_override:
        return await call_next(request)

    try:
        verify_request_auth(request)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return await call_next(request)
''',
r'''_SEC_POLICY_CACHE: dict[tuple, tuple[frozenset, frozenset]] = {}


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

        if scope.get("method") == "OPTIONS" or path in ("/health", "/identity") or has_auth_override:
            await self.app(scope, receive, send)
            return

        try:
            verify_request_auth(Request(scope))
        except HTTPException as exc:
            await JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})(scope, receive, send)
            return
        scope["mesh_auth_verified"] = True
        await self.app(scope, receive, send)


app.add_middleware(_SecurityASGIMiddleware)


''')

P("mesh/http_server.py",
r'''    expected_token = get_or_create_auth_token()
    auth_header = request.headers.get("authorization", "")
    provided_token = ""
''',
r'''    _scope = getattr(request, "scope", None)
    if isinstance(_scope, dict) and _scope.get("mesh_auth_verified") is True:
        return  # already authenticated by _SecurityASGIMiddleware for this very request
    expected_token = get_or_create_auth_token()
    auth_header = request.headers.get("authorization", "")
    provided_token = ""
''')

# 8. cli.py unused imports; MESH_QUIET gate for io.py status lines; benchmark options.
P('mesh/cli.py',
r'''import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
''',
r'''import time
import urllib.parse
from pathlib import Path
''')

P('mesh/io.py',
r'''        if hasattr(sys.stderr, "isatty") and sys.stderr.isatty():
            from . import ui

            ui.success(''',
r'''        if hasattr(sys.stderr, "isatty") and sys.stderr.isatty() and not os.environ.get("MESH_QUIET"):
            from . import ui

            ui.success(''')

P('mesh/io.py',
r'''    if hasattr(sys.stderr, "isatty") and sys.stderr.isatty():
        from . import ui

        _summary =''',
r'''    if hasattr(sys.stderr, "isatty") and sys.stderr.isatty() and not os.environ.get("MESH_QUIET"):
        from . import ui

        _summary =''')

P('benchmark_all_surfaces.py',
r'''    os.environ["MESH_NO_UPDATE_CHECK"] = "1"

    from mesh import Mesh, VERSION''',
r'''    os.environ["MESH_NO_UPDATE_CHECK"] = "1"
    os.environ["MESH_QUIET"] = "1"  # no "Exported/Imported ..." status lines when stderr is a terminal

    from mesh import Mesh, VERSION''')

P('benchmark_all_surfaces.py',
r'''def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
''',
r'''def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


_PERSISTENT_ASGI = False  # --persistent-asgi
_SEPARATE_SERVER = False  # --separate-server


def _make_client(asgi_app, base_url):
    """ASGI test client. Starlette's TestClient starts a new anyio portal THREAD for every request unless it
    is used as a context manager; --persistent-asgi keeps one portal for the whole run so the numbers measure
    the server, not the harness."""
    from fastapi.testclient import TestClient

    client = TestClient(asgi_app, base_url=base_url)
    if _PERSISTENT_ASGI:
        client.__enter__()
    return client
''')

P('benchmark_all_surfaces.py',
r'''    http_client = TestClient(http_app, base_url="http://127.0.0.1:7701")''',
r'''    http_client = _make_client(http_app, "http://127.0.0.1:7701")''')

P('benchmark_all_surfaces.py',
r'''    dash_client = TestClient(dash_app, base_url="http://127.0.0.1:7433")''',
r'''    dash_client = _make_client(dash_app, "http://127.0.0.1:7433")''')

P('benchmark_all_surfaces.py',
r'''    config = uvicorn.Config(http_app, host="127.0.0.1", port=live_port, log_level="error")
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
''',
r'''    server = None
    server_thread = None
    server_proc = None
    if _SEPARATE_SERVER:
        # real daemon in its own process: no shared GIL / timer granularity with the measuring client
        _srv_env = dict(os.environ)
        _srv_env["PYTHONPATH"] = str(Path(__file__).resolve().parent) + (
            os.pathsep + _srv_env["PYTHONPATH"] if _srv_env.get("PYTHONPATH") else ""
        )
        server_proc = subprocess.Popen(
            [sys.executable, "-m", "mesh.http_server", "--port", str(live_port), "--no-warmup", "--yes"],
            env=_srv_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        config = uvicorn.Config(http_app, host="127.0.0.1", port=live_port, log_level="error")
        server = uvicorn.Server(config)
        server_thread = threading.Thread(target=server.run, daemon=True)
        server_thread.start()
''')

P('benchmark_all_surfaces.py',
r'''    server.should_exit = True
    server_thread.join(timeout=3.0)
''',
r'''    _native_exe = Path(__file__).resolve().parent / "c_cli" / ("mesh_fast.exe" if os.name == "nt" else "mesh_fast")
    if _native_exe.exists():
        _nat_env = dict(os.environ)
        _nat_env["MESH_NO_DAEMON_PROXY"] = "0"
        rec.measure(
            "cli_native",
            "mesh_fast --version (native, no Python)",
            lambda i: subprocess.run(
                [str(_native_exe), "--version"], env=_nat_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
            ),
            iterations=10,
            warmup=2,
        )
        rec.measure(
            "cli_native",
            "mesh_fast ask 'Redis port' --json (verified daemon forwarding)",
            lambda i: subprocess.run(
                [str(_native_exe), "ask", "Redis port", "--namespace", "bench_sdk", "--json"],
                env=_nat_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ),
            iterations=10,
            warmup=2,
        )
    if server is not None:
        server.should_exit = True
    if server_thread is not None:
        server_thread.join(timeout=3.0)
    if server_proc is not None:
        server_proc.terminate()
        try:
            server_proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            server_proc.kill()
''')

P('benchmark_all_surfaces.py',
r'''    args = parser.parse_args()

    report = run_all_benchmarks(iters=max(5, args.iterations))''',
r'''    parser.add_argument(
        "--persistent-asgi",
        action="store_true",
        help="Keep ONE anyio portal per ASGI test client (default: Starlette's one-thread-per-request behaviour)",
    )
    parser.add_argument(
        "--separate-server",
        action="store_true",
        help="Run the live-TCP daemon as a separate process instead of a thread of this process",
    )
    args = parser.parse_args()
    globals()["_PERSISTENT_ASGI"] = bool(args.persistent_asgi)
    globals()["_SEPARATE_SERVER"] = bool(args.separate_server)

    report = run_all_benchmarks(iters=max(5, args.iterations))''')

# 9. Stage-2 embedding: low-priority background worker upgrades hash-384 rows to the real ONNX vector.
P("mesh/memory.py",
r'''def _trigger_background_bootstrap() -> None:
    global _bootstrap_triggered
''',
r'''_FG_TS: float = 0.0  # monotonic time of the last FOREGROUND embed (learn / recall / update); the worker yields to it
_REEMBED_EVENT = threading.Event()
_REEMBED_IDLE = threading.Event()
_REEMBED_IDLE.set()
_REEMBED_WORKER_LOCK = threading.Lock()
_REEMBED_WORKER: Optional[threading.Thread] = None
_REEMBED_DRAINING = 0
_REEMBED_INFLIGHT = 0  # items popped from the queue but not finished yet (guarded by _PENDING_REEMBED_LOCK)
_REEMBED_STORES: "OrderedDict[str, MeshStore]" = OrderedDict()
_REEMBED_ATEXIT_REGISTERED = False


def _bg_onnx_enabled() -> bool:
    """Stage 2 only runs when a real ONNX pair is installed and the person has not opted out."""
    if os.environ.get("MESH_BG_ONNX", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    if os.environ.get("MESH_FORCE_HASH_EMBED", "").strip().lower() in ("1", "true", "yes"):
        return False
    if not _should_use_fast_stage1():
        return False
    return _has_local_onnx_pair() is not None


def _reembed_idle_gap() -> float:
    raw = os.environ.get("MESH_BG_ONNX_IDLE_MS")
    try:
        return max(0.0, float(raw) / 1000.0) if raw is not None else 0.15
    except ValueError:
        return 0.15


def _notify_reembed_worker() -> None:
    """Called whenever a hash-384 row is queued: wake (or lazily start) the stage-2 worker. Never blocks."""
    global _REEMBED_WORKER, _REEMBED_ATEXIT_REGISTERED
    if not _bg_onnx_enabled():
        return
    _REEMBED_IDLE.clear()
    _REEMBED_EVENT.set()
    worker = _REEMBED_WORKER
    if worker is not None and worker.is_alive():
        return
    with _REEMBED_WORKER_LOCK:
        if _REEMBED_WORKER is not None and _REEMBED_WORKER.is_alive():
            return
        _REEMBED_WORKER = threading.Thread(target=_reembed_worker_loop, name="mesh-onnx-stage2", daemon=True)
        _REEMBED_WORKER.start()
        if not _REEMBED_ATEXIT_REGISTERED:
            import atexit

            atexit.register(_reembed_atexit)
            _REEMBED_ATEXIT_REGISTERED = True


def _reembed_store(ns: str) -> "MeshStore":
    store = _REEMBED_STORES.get(ns)
    if store is None:
        store = MeshStore(namespace=ns, create=False)
        _REEMBED_STORES[ns] = store
        while len(_REEMBED_STORES) > 8:
            _REEMBED_STORES.popitem(last=False)
    else:
        _REEMBED_STORES.move_to_end(ns)
    return store


def _process_reembed_batch(items: list[tuple[str, str, str]]) -> None:
    """Swap the real ONNX vector in for rows that were stored with the fast hash vector.

    Safe against concurrent edits: a row is only rewritten if its content still equals the queued text
    (checked before AND after the forward pass); `preserve_accessed_at` keeps recall statistics intact.
    """
    by_ns: dict[str, list[tuple[str, str]]] = {}
    for ns, mid, text in items:
        by_ns.setdefault(ns, []).append((mid, text))
    for ns, group in by_ns.items():
        try:
            store = _reembed_store(ns)
        except Exception:
            _REEMBED_STORES.pop(ns, None)
            continue
        valid: list[tuple[str, str]] = []
        for mid, text in group:
            row = store.get(mid)
            if row is not None and row.get("content") == text and row.get("embedding_model") == "hash-384":
                valid.append((mid, text))
        if not valid:
            continue
        vecs, tag = _encode_texts_with_tag([t for _, t in valid])
        if tag != "onnx-minilm-l6-v2":
            continue
        for (mid, text), vec in zip(valid, vecs):
            latest = store.get(mid)
            if latest is not None and latest.get("content") == text and latest.get("embedding_model") == "hash-384":
                store.update(
                    memory_id=mid,
                    content=None,
                    embedding=vec.tolist(),
                    embedding_model="onnx-minilm-l6-v2",
                    preserve_accessed_at=True,
                )


def _reembed_worker_loop() -> None:
    global _REEMBED_INFLIGHT
    while True:
        _REEMBED_EVENT.wait(timeout=60.0)
        with _PENDING_REEMBED_LOCK:
            backlog = len(_PENDING_REEMBED_QUEUE)
        if backlog == 0:
            _REEMBED_EVENT.clear()
            with _PENDING_REEMBED_LOCK:
                if len(_PENDING_REEMBED_QUEUE) == 0 and _REEMBED_INFLIGHT == 0:
                    _REEMBED_IDLE.set()
                else:
                    _REEMBED_EVENT.set()
            if len(_PENDING_REEMBED_QUEUE) == 0 and not _REEMBED_IDLE.is_set():
                time.sleep(0.005)  # another worker is mid-batch: do not spin, do not declare idle
            continue
        if not _bg_onnx_enabled():
            _REEMBED_EVENT.clear()
            _REEMBED_IDLE.set()
            continue
        if not _REEMBED_DRAINING:
            gap = _reembed_idle_gap() - (time.monotonic() - _FG_TS)
            if gap > 0.0:
                time.sleep(min(gap, 0.25))  # a foreground call just ran: stay out of its way
                continue
        with _PENDING_REEMBED_LOCK:
            batch = _PENDING_REEMBED_QUEUE.pop_some(8)
            _REEMBED_INFLIGHT += len(batch)
        try:
            _process_reembed_batch(batch)
        except Exception:
            time.sleep(0.25)  # transient (locked db, model warming): rows stay hash-tagged and are healed later
        finally:
            with _PENDING_REEMBED_LOCK:
                _REEMBED_INFLIGHT -= len(batch)


def drain_reembed_worker(timeout: float = 10.0) -> bool:
    """Block until every queued row has been upgraded (or `timeout`). True when the queue is empty."""
    global _REEMBED_DRAINING
    if not _bg_onnx_enabled():
        return True
    with _PENDING_REEMBED_LOCK:
        if len(_PENDING_REEMBED_QUEUE) == 0 and _REEMBED_INFLIGHT == 0 and _REEMBED_IDLE.is_set():
            return True
    _REEMBED_DRAINING += 1
    try:
        _notify_reembed_worker()
        return _REEMBED_IDLE.wait(timeout=max(0.0, float(timeout)))
    finally:
        _REEMBED_DRAINING -= 1


def _drain_policy_allows() -> bool:
    """close()/exit drain: always when forced (MESH_BG_ONNX_DRAIN_ON_EXIT=1); by default only if the model is
    already resident, so a one-shot CLI call never pays a model load just to upgrade a vector on its way out."""
    mode = os.environ.get("MESH_BG_ONNX_DRAIN_ON_EXIT", "auto").strip().lower()
    if mode in ("0", "false", "no", "off"):
        return False
    if mode in ("1", "true", "yes", "on"):
        return True
    return _model is not None and _model_backend not in (None, "onnx-warming", "hash-fallback")


def _drain_reembed_for_close() -> None:
    try:
        if _drain_policy_allows() and len(_PENDING_REEMBED_QUEUE):
            drain_reembed_worker(timeout=env_float("MESH_BG_ONNX_DRAIN_TIMEOUT", 10.0, minimum=0.0))
    except Exception:
        pass


def _reembed_atexit() -> None:
    _drain_reembed_for_close()


def _trigger_background_bootstrap() -> None:
    global _bootstrap_triggered
''')

P("mesh/memory.py",
r'''        self._data[key] = str(content)
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)
''',
r'''        self._data[key] = str(content)
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)
        _notify_reembed_worker()
''')

P("mesh/memory.py",
r'''    def pop_all(self) -> list[tuple[str, str, str]]:
''',
r'''    def pop_some(self, count: int) -> list[tuple[str, str, str]]:
        items: list[tuple[str, str, str]] = []
        while self._data and len(items) < max(1, int(count)):
            (ns, mid), text = self._data.popitem(last=False)
            items.append((ns, mid, text))
        return items

    def pop_all(self) -> list[tuple[str, str, str]]:
''')

P("mesh/memory.py",
r'''def _embed_cached_with_tag(cleaned_text: str) -> tuple[np.ndarray, str]:
    global _last_model_use_ts
''',
r'''def _embed_cached_with_tag(cleaned_text: str) -> tuple[np.ndarray, str]:
    global _last_model_use_ts, _FG_TS
    _FG_TS = time.monotonic()
''')

P("mesh/memory.py",
r'''    def close(self) -> None:
        if hasattr(self, "store") and self.store is not None:
            self.store.flush_telemetry()
''',
r'''    def close(self) -> None:
        if hasattr(self, "store") and self.store is not None:
            self.store.flush_telemetry()
        _drain_reembed_for_close()
''')

P("mesh/memory.py",
r'''        _embed_cached_with_tag("warmup")
        db_file = ensure_secure_mesh_dir().resolve()''',
r'''        _embed_cached_with_tag("warmup")
        drain_reembed_worker(timeout=env_float("MESH_BG_ONNX_DRAIN_TIMEOUT", 10.0, minimum=0.0))
        db_file = ensure_secure_mesh_dir().resolve()''')

# 10. hybrid_query: per-row loops -> comprehensions (identical rows, order and arithmetic).
P("mesh/store.py",
r'''        if _no_row_filters:
            # Fast path: only the quarantine flag can exclude a row.
            for idx, mid in enumerate(ids_snap):
                cached_row = rows_snap.get(mid)
                if cached_row is None:
                    continue
                if not include_quarantined and cached_row.get("quarantined"):
                    continue
                masked_indices.append(idx)
                masked_mid_set.add(mid)
        else:
''',
r'''        if _no_row_filters:
            # Fast path: only the quarantine flag can exclude a row (comprehensions run ~2x faster than the
            # per-row append loop; same rows, same order).
            if include_quarantined:
                masked_indices = [idx for idx, mid in enumerate(ids_snap) if mid in rows_snap]
            else:
                masked_indices = [
                    idx
                    for idx, mid in enumerate(ids_snap)
                    if (_r := rows_snap.get(mid)) is not None and not _r.get("quarantined")
                ]
            masked_mid_set = {ids_snap[idx] for idx in masked_indices}
        else:
''')

P("mesh/store.py",
r'''        lexical_map: dict[str, float] = {}
        for mid, cnt in _exact_hits.items():
            if mid in masked_mid_set:
                lexical_map[mid] = float(cnt)
        for mid, cnt in _syn_hits.items():
            if mid in masked_mid_set:
                lexical_map[mid] = lexical_map.get(mid, 0.0) + 0.5 * cnt
        if lexical_map:
            max_hits = max(lexical_map.values())
            for mid, hits in list(lexical_map.items()):
                rel = hits / max_hits
                lexical_map[mid] = round(0.65 * rel + 0.35 * (hits / (hits + 1.0)), 4)
''',
r'''        lexical_map: dict[str, float] = {
            mid: float(cnt) for mid, cnt in _exact_hits.items() if mid in masked_mid_set
        }
        for mid, cnt in _syn_hits.items():
            if mid in masked_mid_set:
                lexical_map[mid] = lexical_map.get(mid, 0.0) + 0.5 * cnt
        if lexical_map:
            max_hits = max(lexical_map.values())
            lexical_map = {
                mid: round(0.65 * (hits / max_hits) + 0.35 * (hits / (hits + 1.0)), 4)
                for mid, hits in lexical_map.items()
            }
''')

# 11. SUPERSESSION: edits 10a/10b above rewrite code that round-1 edits #6/#7 also own. Round 1 checks
#     "is my `new` text present?" - so without this it would report an ANCHOR PROBLEM after round 2 ran.
#     Round 2 therefore also updates the expected text recorded inside apply_mesh_patches.py. Result: round 1
#     and round 2 can be run in any order, any number of times, on any tree.
P("apply_mesh_patches.py",
r'''        if _no_row_filters:
            # Fast path: only the quarantine flag can exclude a row.
            for idx, mid in enumerate(ids_snap):
                cached_row = rows_snap.get(mid)
                if cached_row is None:
                    continue
                if not include_quarantined and cached_row.get("quarantined"):
                    continue
                masked_indices.append(idx)
                masked_mid_set.add(mid)
        else:
''',
r'''        if _no_row_filters:
            # Fast path: only the quarantine flag can exclude a row (comprehensions run ~2x faster than the
            # per-row append loop; same rows, same order).
            if include_quarantined:
                masked_indices = [idx for idx, mid in enumerate(ids_snap) if mid in rows_snap]
            else:
                masked_indices = [
                    idx
                    for idx, mid in enumerate(ids_snap)
                    if (_r := rows_snap.get(mid)) is not None and not _r.get("quarantined")
                ]
            masked_mid_set = {ids_snap[idx] for idx in masked_indices}
        else:
''')

P("apply_mesh_patches.py",
r'''        lexical_map: dict[str, float] = {}
        for mid, cnt in _exact_hits.items():
            if mid in masked_mid_set:
                lexical_map[mid] = float(cnt)
''',
r'''        lexical_map: dict[str, float] = {
            mid: float(cnt) for mid, cnt in _exact_hits.items() if mid in masked_mid_set
        }
''')

# 12. native_cli.py (auto-compile helper): the rewritten client needs Winsock + BCrypt on Windows.
P("mesh/native_cli.py",
r'''            if os.name == "nt":
                cmd.append("-lws2_32")
''',
r'''            if os.name == "nt":
                cmd.extend(["-lws2_32", "-lbcrypt", "-lshell32"])
''')

P("mesh/native_cli.py",
r'''            cmd = [cl_path, "/O2", "/nologo", str(src), f"/Fe:{out}", "ws2_32.lib"]''',
r'''            cmd = [cl_path, "/O2", "/nologo", str(src), f"/Fe:{out}", "ws2_32.lib", "bcrypt.lib", "shell32.lib"]''')

# 13. _parse_iso_utc: the same few hundred timestamps were re-parsed on every digest / stale-check / recall.
#     Pure function of an immutable str returning an immutable datetime -> safe to memoise.
P("mesh/store.py",
r'''def _parse_iso_utc(ts: Optional[str]) -> Optional[datetime]:
    if not ts or not isinstance(ts, str):
        return None
    try:
        cleaned = ts.strip()
''',
r'''@functools.lru_cache(maxsize=16384)
def _parse_iso_utc_cached(ts: str) -> Optional[datetime]:
    return _parse_iso_utc_impl(ts)


def _parse_iso_utc(ts: Optional[str]) -> Optional[datetime]:
    if not ts or not isinstance(ts, str):
        return None
    return _parse_iso_utc_cached(ts)


def _parse_iso_utc_impl(ts: Optional[str]) -> Optional[datetime]:
    if not ts or not isinstance(ts, str):
        return None
    try:
        cleaned = ts.strip()
''')

P("mesh/store.py",
r'''import os
import re
''',
r'''import functools
import os
import re
''')

# 14. _ensure_initialized: several processes creating the same new namespace at once can get an immediate
#     "database is locked" from the journal_mode=WAL switch (the busy handler is not consulted for it).
#     This is a robustness measure only. NOTE: it is NOT the cause of the Windows failure of
#     tests/test_thread_safety_c001.py::test_multi_process_writers_share_one_namespace - that one is a missing
#     numpy in the child process (edit 15 below). It has not been shown to matter on Windows.
#     Retry only lock/busy errors, bounded, with back-off.
P("mesh/store.py",
r'''            self._init_db()
            new_stamp = _get_db_stamp(self.db_path)
            if new_stamp is not None:
                _INITIALIZED_DBS[self.db_path] = new_stamp
''',
r'''            try:
                _init_budget = max(0.0, float(os.environ.get("MESH_INIT_LOCK_RETRY_SECONDS", "10")))
            except ValueError:
                _init_budget = 10.0
            _init_deadline = time.monotonic() + _init_budget
            _init_delay = 0.01
            while True:
                try:
                    self._init_db()
                    break
                except sqlite3.OperationalError as _exc:
                    _msg = str(_exc).lower()
                    if ("locked" not in _msg and "busy" not in _msg) or time.monotonic() >= _init_deadline:
                        raise
                    time.sleep(_init_delay)
                    _init_delay = min(_init_delay * 2.0, 0.25)
            new_stamp = _get_db_stamp(self.db_path)
            if new_stamp is not None:
                _INITIALIZED_DBS[self.db_path] = new_stamp
''')

# 15. tests/test_thread_safety_c001.py: the `isolated_mesh_env` fixture points HOME/USERPROFILE/APPDATA at a temp
#     dir. Python finds per-user site-packages (`pip install --user`) through those variables, so the spawned
#     worker processes could not import numpy ("No module named 'numpy'") although the parent could. This is an
#     environment detail of the test harness, not a Mesh behaviour: the children now receive the parent's own
#     import path. No assertion is weakened.
P("tests/test_thread_safety_c001.py",
r'''def test_multi_process_writers_share_one_namespace(isolated_mesh_env):
    namespace = _ns("c001-mp")
''',
r'''def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    extra = [p for p in sys.path if p and os.path.isdir(p)]
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join(extra + ([existing] if existing else []))
    return env


def test_multi_process_writers_share_one_namespace(isolated_mesh_env):
    namespace = _ns("c001-mp")
''')

P("tests/test_thread_safety_c001.py",
r'''            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for w in range(procs)
''',
r'''            cwd=str(repo_root),
            env=_child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for w in range(procs)
''')

# 16-20. Round 3 (driven by the Windows profile: stat/lstat cost ~60 us each there; argparse colour probing).
P('mesh/audit.py',
r'''    """Return a composite (db_mtime_ns, db_size, wal_mtime_ns, wal_size) stamp for WAL-safe sync."""
    if not db_path.exists():
        return None
    try:
        st = db_path.stat()
        if st.st_size <= 0:
            return None
        wal_path = Path(f"{db_path}-wal")
        if wal_path.exists():
            wst = wal_path.stat()
            return (st.st_mtime_ns, st.st_size, wst.st_mtime_ns, wst.st_size)
        return (st.st_mtime_ns, st.st_size, 0, 0)
    except OSError:
        return None
''',
r'''    """Return a composite (db_mtime_ns, db_size, wal_mtime_ns, wal_size) stamp for WAL-safe sync."""
    # One stat per file: the old exists()+stat() pairs doubled the syscalls (~60 us each on Windows with AV).
    try:
        st = os.stat(db_path)
    except OSError:
        return None
    if st.st_size <= 0:
        return None
    try:
        wst = os.stat(f"{db_path}-wal")
        return (st.st_mtime_ns, st.st_size, wst.st_mtime_ns, wst.st_size)
    except OSError:
        return (st.st_mtime_ns, st.st_size, 0, 0)
''')

P('mesh/audit.py',
r'''        ts = _PATH_TRUST.get(key)
        if ts is not None and (time.monotonic() - ts) < ttl:
            link = _lstat_link_state(key)
            if link is False:
                return
            _PATH_TRUST.pop(key, None)
''',
r'''        ts = _PATH_TRUST.get(key)
        _now = time.monotonic()
        if ts is not None and (_now - ts) < ttl:
            _micro = _fs_memo_seconds()
            if _micro > 0.0:
                _seen = _PATH_MICRO.get(key)
                if _seen is not None and (_now - _seen) < _micro:
                    return  # lstat-verified milliseconds ago, inside the same operation: skip a repeat syscall
            link = _lstat_link_state(key)
            if link is False:
                _PATH_MICRO[key] = _now
                return
            _PATH_MICRO.pop(key, None)
            _PATH_TRUST.pop(key, None)
''')

P('apply_mesh_patches.py',
r'''        ts = _PATH_TRUST.get(key)
        if ts is not None and (time.monotonic() - ts) < ttl:
            link = _lstat_link_state(key)
            if link is False:
                return
            _PATH_TRUST.pop(key, None)
''',
r'''        ts = _PATH_TRUST.get(key)
        _now = time.monotonic()
        if ts is not None and (_now - ts) < ttl:
            _micro = _fs_memo_seconds()
            if _micro > 0.0:
                _seen = _PATH_MICRO.get(key)
                if _seen is not None and (_now - _seen) < _micro:
                    return  # lstat-verified milliseconds ago, inside the same operation: skip a repeat syscall
            link = _lstat_link_state(key)
            if link is False:
                _PATH_MICRO[key] = _now
                return
            _PATH_MICRO.pop(key, None)
            _PATH_TRUST.pop(key, None)
''')

P('mesh/audit.py',
r'''def clear_path_trust_cache() -> None:
    """Forget every verified path (tests / evict hooks)."""
    _PATH_TRUST.clear()
''',
r'''_PATH_MICRO: dict[str, float] = {}


def _fs_memo_seconds() -> float:
    """Window (default 10 ms) in which a path that was lstat-verified is not re-checked (MESH_FS_MEMO_MS, 0 = off)."""
    raw = os.environ.get("MESH_FS_MEMO_MS")
    try:
        return max(0.0, float(raw) / 1000.0) if raw is not None else 0.01
    except ValueError:
        return 0.01


def clear_path_trust_cache() -> None:
    """Forget every verified path (tests / evict hooks)."""
    _PATH_TRUST.clear()
    _PATH_MICRO.clear()
''')

P('apply_mesh_patches.py',
r'''def clear_path_trust_cache() -> None:
    """Forget every verified path (tests / evict hooks)."""
    _PATH_TRUST.clear()
''',
r'''_PATH_MICRO: dict[str, float] = {}


def _fs_memo_seconds() -> float:
    """Window (default 10 ms) in which a path that was lstat-verified is not re-checked (MESH_FS_MEMO_MS, 0 = off)."""
    raw = os.environ.get("MESH_FS_MEMO_MS")
    try:
        return max(0.0, float(raw) / 1000.0) if raw is not None else 0.01
    except ValueError:
        return 0.01


def clear_path_trust_cache() -> None:
    """Forget every verified path (tests / evict hooks)."""
    _PATH_TRUST.clear()
    _PATH_MICRO.clear()
''')

P('mesh/crypto.py',
r'''    _write_atomic_private_bytes(_encryption_marker_path(), b"1")
    get_active_master_key()
''',
r'''    _write_atomic_private_bytes(_encryption_marker_path(), b"1")
    _invalidate_enc_memo()
    get_active_master_key()
''')

P('mesh/crypto.py',
r'''    try:
        _encryption_marker_path().unlink(missing_ok=True)
    except OSError:
        pass
    with _KEY_LOCK:
        _CACHED_KEY_STATE = None
''',
r'''    try:
        _encryption_marker_path().unlink(missing_ok=True)
    except OSError:
        pass
    _invalidate_enc_memo()
    with _KEY_LOCK:
        _CACHED_KEY_STATE = None
''')

P('mesh/store.py',
r'''        if query_embedding_model is not None:
            valid_indices = [
                idx
                for idx, mid in enumerate(ids_snap)
                if mid in rows_snap
                and not _is_mixed_space(rows_snap[mid].get("embedding_model"), query_embedding_model)
            ]
            if not valid_indices:
''',
r'''        if query_embedding_model is not None:
            # the per-row model tag takes a handful of distinct values: decide each value once, not once per row
            _mix_memo: dict[Any, bool] = {}
            valid_indices = []
            for idx, mid in enumerate(ids_snap):
                _row = rows_snap.get(mid)
                if _row is None:
                    continue
                _em = _row.get("embedding_model")
                _mixed = _mix_memo.get(_em)
                if _mixed is None:
                    _mixed = _is_mixed_space(_em, query_embedding_model)
                    _mix_memo[_em] = _mixed
                if not _mixed:
                    valid_indices.append(idx)
            if not valid_indices:
''')

P('mesh/cli.py',
r'''def _parser(name: str, description: str, examples: Optional[list[tuple[str, str]]] = None) -> argparse.ArgumentParser:
    epilog = None
    if examples:
        width = max(len(c) for c, _ in examples) + 2
        epilog = "examples:\n" + "\n".join(f"  {c.ljust(width)}# {d}" for c, d in examples)
    return argparse.ArgumentParser(
''',
r'''_TERM_WIDTH_CACHE: Optional[tuple[float, int]] = None


def _cached_term_width() -> int:
    """HelpFormatter's default width, without a console query for every add_argument() (~120 us on Windows)."""
    global _TERM_WIDTH_CACHE
    now = time.monotonic()
    cached = _TERM_WIDTH_CACHE
    if cached is not None and (now - cached[0]) < 1.0:
        return cached[1]
    import shutil as _shutil

    width = _shutil.get_terminal_size().columns - 2
    _TERM_WIDTH_CACHE = (now, width)
    return width


def _install_colorize_memo() -> None:
    """Python 3.14's argparse calls _colorize.can_colorize() (env lookups + a console-mode syscall on Windows) for
    every parser and formatter - thousands of times per `mesh task list`. The answer cannot change within a
    process for the same stream, so remember it."""
    if sys.version_info < (3, 13):
        return
    try:
        import _colorize
    except ImportError:
        return
    orig = getattr(_colorize, "can_colorize", None)
    if orig is None or getattr(orig, "_mesh_memo", False):
        return
    memo: dict[Any, bool] = {}

    def can_colorize(*args: Any, **kwargs: Any) -> bool:
        key = (tuple(id(a) for a in args), tuple((k, id(v)) for k, v in sorted(kwargs.items())))
        if key not in memo:
            memo[key] = orig(*args, **kwargs)
        return memo[key]

    can_colorize._mesh_memo = True  # type: ignore[attr-defined]
    _colorize.can_colorize = can_colorize


_install_colorize_memo()


class _FastArgumentParser(argparse.ArgumentParser):
    def _get_formatter(self) -> Any:
        try:
            formatter = self.formatter_class(prog=self.prog, width=_cached_term_width())
        except TypeError:
            return super()._get_formatter()
        set_color = getattr(formatter, "_set_color", None)
        if set_color is not None:
            set_color(getattr(self, "color", False))
        return formatter


def _parser(name: str, description: str, examples: Optional[list[tuple[str, str]]] = None) -> argparse.ArgumentParser:
    epilog = None
    if examples:
        width = max(len(c) for c, _ in examples) + 2
        epilog = "examples:\n" + "\n".join(f"  {c.ljust(width)}# {d}" for c, d in examples)
    return _FastArgumentParser(
''')

# ============================================================================
# driver
# ============================================================================
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".", help="repo root containing mesh/")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--revert", action="store_true")
    args = ap.parse_args()
    root = Path(args.root).resolve()

    if args.revert:
        n = 0
        for bak in root.glob("**/*.pre_round2"):
            shutil.copy2(bak, bak.with_suffix(""))
            n += 1
        print(f"reverted {n} file(s)")
        return 0

    by_file: dict[str, list[tuple[str, str]]] = {}
    for f, old, new in PATCHES:
        by_file.setdefault(f, []).append((old, new))

    results: dict[str, str] = {}
    failed = False
    applied = skipped = 0
    for f, edits in by_file.items():
        p = root / f
        if not p.exists():
            print(f"MISSING FILE: {f}")
            failed = True
            continue
        raw = p.read_bytes().decode("utf-8")
        crlf = "\r\n" in raw
        text = raw.replace("\r\n", "\n")
        for i, (old, new) in enumerate(edits, 1):
            if new in text:  # check FIRST: for some edits `new` contains `old`
                skipped += 1
            elif any(text.count(v) == 1 for v in PREV.get((f, old), ())):
                variant = next(v for v in PREV[(f, old)] if text.count(v) == 1)
                text = text.replace(variant, new)  # upgrade a tree patched by an earlier version of this script
                applied += 1
            elif text.count(old) == 1:
                text = text.replace(old, new)
                applied += 1
            else:
                print(f"ANCHOR PROBLEM in {f} (edit #{i}, matches={text.count(old)}): {old[:90]!r}")
                failed = True
        results[f] = text.replace("\n", "\r\n") if crlf else text

    if failed:
        print("ABORTED: nothing was written.")
        return 2
    print(f"{applied} edit(s) to apply, {skipped} already applied.")
    if args.check:
        return 0
    for f, text in results.items():
        p = root / f
        bak = p.with_suffix(p.suffix + ".pre_round2")
        if not bak.exists():
            shutil.copy2(p, bak)
        p.write_bytes(text.encode("utf-8"))
    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

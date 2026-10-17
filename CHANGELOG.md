# Changelog

All notable changes to **Mesh** (`mesh-context-layer`) are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.6.1] - 2026-10-17

### Security & Privacy Hardening
- **Eliminated `Host: testserver` Auth Bypass (`mesh/http_server.py` & `mesh/dashboard.py`)**:
  - Removed `"testserver"` from `_ALLOWED_HOSTS` and removed unauthenticated bypass logic from production middleware; added `verify_request_auth` and `verify_dashboard_auth` FastAPI dependencies for clean test overrides.
  - Protected `GET /` on the Web Dashboard with a launch-scoped `?token=` exchange that sets an `HttpOnly; SameSite=Strict` session cookie (`mesh_dash_session`) and strips the token from the URL bar via `history.replaceState`, never exposing `~/.mesh/auth_token` to unauthenticated local requests.
  - Fixed the IPv6 localhost CORS regular expression in `http_server.py` (`[::1]`).
- **Fixed Dashboard Stored XSS & Added Memory ID Validation (`mesh/dashboard.py`, `mesh/io.py`, `mesh/store.py`)**:
  - Enforced strict regex validation (`validate_memory_id`) on all memory and contradiction IDs across `store.py` and `io.py`.
  - Replaced all inline `onclick="..."` string interpolations in `dashboard.py` with delegated `data-action` and `data-id` attributes and full HTML/attribute escaping (`&`, `<`, `>`, `"`, `'`).
- **Redacted Private Memories in Contradiction Payloads & Cold Cache (`mesh/memory.py` & `mesh/store.py`)**:
  - Redacted `existing_content` and `existing_memory` in the `conflict` dictionary returned by `learn()` whenever the existing memory has `local_only=True`.
  - Hardened `store.log_contradiction()` to query SQLite when the in-memory cache is cold so private existing memories are never written unredacted to `contradictions`.
  - Defaulted `include_private=False` on `mesh_recall`, `mesh_inspect`, `POST /recall`, and `GET /inspect` so private memories are not sent to external agent model providers unless explicitly requested.
- **Historical Audit & Contradiction Scrubbing on `update()` (`mesh/audit.py` & `mesh/store.py`)**:
  - Added `scrub_memory_update_from_audit()` and automatic `contradictions` scrubbing whenever a memory's content is modified or marked `local_only=True` via `update()`.
  - Added automatic retention pruning to bound `audit_log` (`MESH_MAX_AUDIT_ROWS=10000`) and `queries` (`MESH_MAX_QUERY_ROWS=10000`).
- **Atomic `0600` Token/DB Permissions & `0700` Directory Permissions (`mesh/audit.py`, `mesh/daemon.py`, `mesh/store.py`)**:
  - Created `~/.mesh` with mode `0700` and wrote `~/.mesh/auth_token`, `daemon.pid`, and `daemon.log` atomically with `os.open(..., 0o600)` so sensitive files are never briefly world-readable.
- **Explicit `y/n` User Consent Across Background & Cloud Operations**:
  - Added interactive `y/n` user permission prompts before downloading the ~90 MB ONNX model (`memory.py`), starting background warmup/sync threads (`http_server.py`, `mcp_server.py`, `dashboard.py`), uploading redacted session logs to cloud LLM providers (`passive.py`), modifying shell RC files (`cli.py`), or deleting namespaces (`cli.py`).
  - Made PyPI update checks strictly opt-in (`mesh status --check-updates` or `MESH_CHECK_UPDATES=1`).
- **Prompt Injection & Context Block Hardening (`mesh/context_builder.py`)**:
  - Collapsed newlines and control characters inside memories, stripped leading markdown heading/bullet markers, and neutralized case-insensitive `<mesh_context` sequences even without a closing `>`.
  - Added optional `MESH_LOCKED_NAMESPACE` enforcement in `validate_namespace()`.

### Fixed & Improved
- **MCP Daemon Proxy Stale Keep-Alive Recovery (`mesh/mcp_server.py`)**:
  - Detected closed idle keep-alive sockets via `select.select` prior to sending and enforced fresh connections for mutating tools (`_MUTATING_TOOLS`), fixing first-write-after-idle `RemoteDisconnected` failures.
- **Fixed Custom AI Provider Detection (`mesh/passive.py`)**:
  - Checked `MESH_AI_API_KEY` + `MESH_AI_BASE_URL` before provider-specific defaults in `detect_provider()` so custom OpenAI-compatible endpoints are never misrouted to Anthropic.
  - Discarded partial leading lines when seeking into large `.jsonl` and `.log` files.
- **Verified ONNX SHA-256 Digests & Atomic Model Tagging (`mesh/memory.py`)**:
  - Updated `_ALLOWED_MODEL_SHA256` and `_ALLOWED_TOKENIZER_SHA256` with verified 64-char SHA-256 digests and added a 200 MB stream/tarball extraction cap.
  - Stored `(vector, model_tag)` atomically in `_EMBED_CACHE` and `_embed_with_tag()` to prevent race conditions during background ONNX hot-swaps.
  - Improved deduplication and contradiction detection to recognize digits inside alphanumeric identifiers (`db1` vs `db2`, `v2` vs `v3`), added multi-window mean pooling for texts >256 tokens, and enforced `MAX_CONTENT_LENGTH = 16384`.
- **Unified Staleness, TTL Clearing & Context Ranking (`mesh/store.py`, `mesh/namespaces.py`, `mesh/digest.py`)**:
  - Unified staleness calculations across `namespace_stats()`, `generate_digest()`, and `get_patterns()` using `_compute_row_effective_confidence()`.
  - Added `clear_ttl=True` / `--clear-ttl` support across Python, CLI, MCP, HTTP, and Dashboard.
  - Disabled telemetry increments during `build_context()` (`record_telemetry=False`) to eliminate the `access_count` ranking feedback loop.
  - Raised `ValueError` in `resolve_contradiction()` when a contradiction ID does not exist.
- **CLI, Doctor, Completions & Packaging Polish (`mesh/cli.py`, `mesh/__init__.py`, `pyproject.toml`)**:
  - Implemented PEP 562 lazy `__getattr__` in `mesh/__init__.py` and moved `sentence-transformers` and `fastembed` to optional dependencies in `pyproject.toml`.
  - Upgraded `mesh doctor` to verify SQLite FTS5, `~/.mesh` permissions, ONNX SHA-256 checksums, daemon status, and MCP configs.
  - Added native `bash`, `zsh`, `fish`, and `powershell` completion scripts with idempotent `--install` markers.

---

## [0.6.0] - 2026-10-15

### Added
- **Direct Zero-Network ONNX Runtime Engine (`_DirectOnnxEmbedding`)**:
  - Replaced blocking Hugging Face Hub calls with direct `onnxruntime.InferenceSession` + `tokenizers.Tokenizer` execution (`~80 ms` cold start, `~1.5–3 ms` CPU inference, zero PyTorch overhead).
  - Routed `_encode_texts()` directly through the 4,096-entry `_EMBED_CACHE` for `<0.01 ms` LRU cache hits.
- **Non-Blocking Multi-Mirror Background ONNX Auto-Downloader**:
  - Downloads `model.onnx` and `tokenizer.json` into `~/.mesh/models/all-MiniLM-L6-v2-onnx/` prioritizing Google Cloud Storage and CDN mirrors.
- **In-Memory Metadata Cache & Capacity-Doubling Vector Buffer (`mesh/store.py`)**:
  - Cached full row metadata and parsed `tags_list` in RAM (`_VectorCacheEntry.rows_by_id`) alongside a capacity-doubling `float32` NumPy matrix buffer.
- **Persistent SQLite WAL Connection Pooling (`mesh/store.py` & `mesh/audit.py`)**:
  - Replaced per-call connection closing with persistent pooled SQLite WAL connections (`_PooledConnection` and `_ManagedAuditConnection`).

---

## [0.5.3] - 2026-10-15

### Added
- Added `GET /engine` and `POST /unload` HTTP REST endpoints and `mesh-daemon` CLI management commands.
- Added background inactivity watchdog (`MESH_IDLE_UNLOAD_SECONDS=180`) to unload neural model weights when idle.

---

## [0.5.0] - 2026-10-15

### Major Architecture Evolution
- Replaced dual ChromaDB + SQLite storage with a single unified SQLite WAL database (`mesh_<namespace>.db`) + in-memory NumPy SIMD matrix cache + FTS5 full-text search.
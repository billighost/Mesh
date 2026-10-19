# Mesh — Architecture & Contributor Guide (v0.8.0)

> How the code is organized, how the 5-stage hybrid storage & retrieval engine works, and how to extend Mesh safely.

---

## Table of Contents

1. [Folder Structure](#folder-structure)
2. [Module Map](#module-map)
3. [Data Flow Diagrams](#data-flow-diagrams)
   - [learn() flow](#learn-flow)
   - [recall() flow](#recall-flow)
   - [update() flow](#update-flow)
4. [Module Deep Dives](#module-deep-dives)
   - [__init__.py](#__init__py)
   - [store.py](#storepy)
   - [memory.py](#memorypy)
   - [crypto.py](#cryptopy)
   - [secrets.py](#secretspy)
   - [guards.py](#guardspy)
   - [audit.py](#auditpy)
   - [context_builder.py](#context_builderpy)
   - [passive.py](#passivepy)
   - [digest.py](#digestpy)
   - [io.py](#iopy)
   - [namespaces.py](#namespacespy)
   - [http_server.py](#http_serverpy)
   - [mcp_server.py](#mcp_serverpy)
   - [dashboard.py](#dashboardpy)
   - [daemon.py](#daemonpy)
   - [cli.py](#clipy)
   - [skill_installer.py](#skill_installerpy)
   - [ui.py](#uipy)
   - [version.py](#versionpy)
5. [Storage Schema](#storage-schema)
6. [Performance & Caching Architecture](#performance--caching-architecture)
7. [Security, Privacy & Consent Architecture](#security-privacy--consent-architecture)
8. [How to Add a New Feature](#how-to-add-a-new-feature)
9. [How to Add a New CLI Command](#how-to-add-a-new-cli-command)
10. [How to Add a New MCP Tool](#how-to-add-a-new-mcp-tool)
11. [How to Add a New HTTP Endpoint](#how-to-add-a-new-http-endpoint)
12. [How to Add a New Memory Type](#how-to-add-a-new-memory-type)
13. [Testing & Quality Assurance](#testing)
14. [Key Design Decisions](#key-design-decisions)

---

## Folder Structure

```text
mesh/
├── __init__.py           # PEP 562 lazy public API surface — zero-overhead module loading
├── memory.py             # Core Mesh class — learn(), recall(), update(), forget(), patterns(), etc.
├── store.py              # Unified SQLite WAL + in-memory SIMD NumPy matrix + rowid FTS5 BM25 + IDF engine
├── crypto.py             # Authenticated encryption at rest (ChaCha20-Poly1305 / AES-GCM) & key derivation
├── secrets.py            # Credential detection, secret auto-redaction & local keyring fallback vault
├── guards.py             # Prompt-injection defense, Unicode normalization & poison filter
├── envutil.py            # Strict boolean, integer & float environment parser with NaN/Inf defense
├── audit.py              # Fault-isolated SQLite audit trail with automatic redaction, scrubbing & retention
├── context_builder.py    # Unified prompt-injection-safe system prompt builder (markdown, plain, xml, json)
├── passive.py            # AI-powered & local regex extraction from agent session logs (with y/n cloud consent)
├── digest.py             # Timezone-safe UTC session summary generator
├── io.py                 # High-speed validated batch export/import to and from JSON files
├── namespaces.py         # Namespace discovery, deletion, renaming, cross-module cache eviction & statistics
├── http_server.py        # FastAPI REST API server (port 7701) with Bearer/X-Mesh-Token auth
├── mcp_server.py         # Production MCP server over stdio + stale-socket-aware daemon proxy
├── dashboard.py          # FastAPI + zero-build inline HTML/JS web UI (port 7433) with HttpOnly session cookie
├── daemon.py             # Background daemon, atomic 0600 token manager, y/n consent helper & OS auto-start
├── cli.py                # Unified `mesh` command router, REPL shell, doctor diagnostics & multi-shell completions
├── skill_installer.py    # Multi-agent skill discovery & automated installer (Cursor, Claude, Antigravity, etc.)
├── ui.py                 # Rich terminal rendering, spinners, progress formatting & color control
├── _model_download.py    # SHA-256 verified ONNX model weight downloader with non-blocking fallback
└── version.py            # Package version metadata, schema compatibility helpers & opt-in PyPI update checker

benchmarks/
├── mesh_benchmark.py     # End-to-end latency & hybrid retrieval throughput benchmark
├── scale_benchmark.py    # Scale evaluation (1k, 3k, 5k memories) measuring throughput, DB size, RAM & latency
└── concurrency_stress.py # High-concurrency stress suite (16 threads dedup, 320 multi-writers, multi-process lock)

tests/
├── test_memory.py              # Core unit tests: learn, hybrid recall, dedup, conflicts, decay, update
├── test_v03.py                 # Privacy flags (local_only), export/import, namespace management
├── test_v04.py                 # Audit trail, context builder, digest, HTTP API, MCP tools, passive capture
├── test_v06.py                 # Security fixes, token boundary, prompt sanitization, atomic token concurrency
├── test_security_audit_v08.py  # Security audit v0.8: encrypted databases, FTS suppression, cloud redaction
├── test_retrieval_quality.py   # Retrieval accuracy regression, token prefix boundaries, Unicode bypasses
├── test_dependency_matrix.py   # Runtime boundaries, PEP 562 lazy-loading isolation, keyring fallback
└── bench/                      # Retrieval quality benchmark fixtures and test_retrieval_benchmark.py

~/.mesh/                  # Local data directory (created with 0700 permissions on POSIX)
├── auth_token            # Per-user 256-bit authentication token (created atomically with 0600 permissions)
├── keyring_vault.json    # Encrypted local fallback vault when system keyring service is unavailable (0600)
├── daemon.pid            # Active background daemon PID file (0600)
├── daemon.log            # Background daemon log file (0600)
├── models/               # SHA-256 verified ONNX models (all-MiniLM-L6-v2-onnx)
├── mesh_shared.db        # Unified SQLite DB (memories + rowid FTS5 + queries + contradictions, mode 0600)
├── mesh_<namespace>.db   # Separate isolated SQLite DB per namespace (0600)
├── audit_shared.db       # Fault-isolated SQLite audit trail for "shared" (0600)
└── audit_<namespace>.db  # Separate audit DB per namespace (0600)
```

---

## Module Map

Here is how the modules depend on each other:

```text
__init__.py (PEP 562 lazy __getattr__)
  └── Dynamically imports attributes on first access from submodules below

cli.py
  └── Lazy-routes to memory.py, io.py, namespaces.py, audit.py, context_builder.py,
      digest.py, passive.py, dashboard.py, http_server.py, mcp_server.py, daemon.py, version.py

daemon.py
  └── audit.py (permission helpers), store.py (MESH_DIR), memory.py (on warmup/start)

http_server.py
  └── memory.py, audit.py, context_builder.py (build_context_bundle), daemon.py

mcp_server.py
  └── memory.py, audit.py, context_builder.py (build_context_bundle), passive.py, daemon.py

dashboard.py
  └── memory.py, namespaces.py, io.py, audit.py, digest.py, daemon.py

context_builder.py
  └── memory.py, audit.py

io.py
  └── store.py, memory.py, passive.py, audit.py, version.py

passive.py
  └── memory.py, audit.py, daemon.py (request_user_permission)

digest.py
  └── store.py (_compute_row_effective_confidence & UTC helpers), audit.py

namespaces.py
  └── store.py, audit.py (plus sys.modules cache eviction hooks for context_builder/http/mcp/dashboard)

memory.py
  └── store.py, audit.py, daemon.py (request_user_permission)

store.py
  └── sqlite3, numpy, audit.py (permission, validation & audit-scrubbing helpers)

audit.py
  └── sqlite3 (standalone — manages `audit_<namespace>.db` files & POSIX 0700/0600 permissions)

version.py
  └── standalone — VERSION constant, semantic version tuple parser & opt-in PyPI checker
```

---

## Data Flow Diagrams

### `learn()` flow

```text
caller
  │
  ▼
Mesh.learn(content, memory_type, confidence, tags, ttl_days, local_only, deduplicate=True)
  │
  ├─► Validate content (non-empty, len <= MAX_CONTENT_LENGTH [16,384]) & memory_type (9 valid types)
  │
  ├─► _heal_namespace_embeddings_if_needed()
  │     └── O(1) check on `cache.needs_healing_ids`; if ONNX is loaded and warm-up rows exist,
  │         re-embeds only those rows in-place (preserving `accessed_at` and verifying content)
  │
  ├─► _embed_with_tag(clean_content) -> returns (embedding, emb_model_tag) atomically
  │     ├─► Check _EMBED_CACHE (OrderedDict LRU, maxsize=4096) -> <0.01ms hit
  │     └─► On miss:
  │           ├── Direct ONNX Runtime (`_DirectOnnxEmbedding`, multi-window mean pooling for >256 tokens)
  │           └── Or `_hash_embed_384` (<0.05ms) if ONNX is not yet downloaded/approved
  │
  ├─► MeshStore.count() > 0? (O(1) via in-memory `_VectorCacheEntry.all_ids`)
  │     └── YES → MeshStore.query(embedding, n=3) (SIMD cosine dot product `mat @ q_vec`)
  │           │
  │           ├─► Pass 1 (Deduplication, if deduplicate=True):
  │           │     _is_true_duplicate(clean_content, doc, similarity)?
  │           │     (exact match OR similarity >= 0.985 with identical numbers, alphanumeric digit IDs
  │           │      like db1/v2, negation polarity, and full-text word Jaccard overlap >= 0.88)
  │           │     └── YES → Merge tags, take max(confidence), call MeshStore.update() in-place
  │           │               └─► return {"memory_id": existing_id, "status": "stored", "deduplicated": True}
  │           │
  │           └─► Pass 2 (Contradiction Detection):
  │                 _is_true_contradiction(clean_content, doc, similarity)?
  │                 └── YES → Redact existing_content if existing row is `local_only=True`
  │                           MeshStore.log_contradiction(...) -> conflict dict (redacted if private)
  │
  ├─► MeshStore.upsert(memory_id, content, embedding, metadata, local_only, embedding_model=emb_model_tag)
  │     ├── Validate `memory_id` via `validate_memory_id()`
  │     ├── Normalize vector to contiguous float32 BLOB
  │     ├── Single SQLite WAL transaction:
  │     │     ├── INSERT OR REPLACE INTO memories (...)
  │     │     └── Sync row in `memories_fts` by integer `rowid` (O(log N))
  │     ├── Mutate `_VectorCacheEntry` in-place in RAM (matrix buffer + pre-tokenized lexical/IDF cache)
  │     └── Refresh tracked composite (.db + .db-wal + data_version) stamp
  │
  ├─► log_learn() -> `audit_<namespace>.db` (redacted if `local_only=True`)
  │
  └─► return {"memory_id": memory_id, "status": "stored" | "stored_with_conflict", "deduplicated": False, "conflict": conflict}
```

### `recall()` flow

```text
caller
  │
  ▼
Mesh.recall(query, n, min_similarity, min_confidence, include_stale, memory_type, tags, source_agent, include_private, record_telemetry=True)
  │
  ├─► _embed_query(clean_query) -> blends domain synonyms for short queries (computed once in `recall_with_context`)
  │
  ├─► MeshStore.hybrid_query(query_text, embedding, n=max(n*4, 25), ...)
  │     ├── 1. Pre-mask candidates in RAM by memory_type, tags, source_agent, include_private, include_stale, min_confidence
  │     ├── 2. Dense SIMD Cosine Search: `sims = mat @ q_vec` (zero matrix copy under snapshot)
  │     ├── 3. FTS5 BM25 lexical search over `memories_fts` (`_fts_lexical_scores`)
  │     ├── 4. O(|Q|) Robertson-Sparck Jones Smoothed IDF lookup from pre-cached `cache.stem_df`
  │     │      + pre-tokenized `cache.lexical_by_id` exact/synonym/compound scoring
  │     └── 5. Lexical Grounding Floor + Zero-Overlap Noise Suppression Gate -> `similarity`
  │
  ├─► For each candidate (using a single UTC `now` reference):
  │     ├── _compute_decay_metrics(ttl_days, decay_start_at, now)
  │     ├── effective_confidence = round(stored_confidence * decay_factor, 4)
  │     ├── is_stale = effective_confidence < 0.3
  │     └── Compute `composite_score = 0.72 * similarity + 0.22 * effective_confidence + 0.06 * usage_boost`
  │
  ├─► Sort by `(composite_score, similarity, effective_confidence, access_count)` DESC and slice `[:n]`
  │
  ├─► If `record_telemetry=True`:
  │     ├── MeshStore.record_recall_telemetry(...)
  │     │     ├── Single atomic SQLite WAL transaction:
  │     │     │     ├── UPDATE memories SET access_count = access_count + 1, accessed_at = ?
  │     │     │     ├── INSERT INTO queries (...)
  │     │     │     └── Periodic `_prune_queries_table_locked()` (caps at MESH_MAX_QUERY_ROWS)
  │     │     └── Copy-on-write row replacement in `synced_cache.rows_by_id`
  │     └── log_recall() -> `audit_<namespace>.db`
  │
  └─► return memories
```

### `update()` flow

```text
caller
  │
  ▼
Mesh.update(memory_id, content, memory_type, confidence, tags, ttl_days, reset_ttl, clear_ttl, local_only)
  │
  ├─► Resolve full ID via `MeshStore.resolve_id_prefix(memory_id)` (O(1) in-memory prefix lookup)
  ├─► Re-compute `_embed_with_tag(content)` ONLY if `content` was provided and changed
  ├─► MeshStore.update(...)
  │     ├── UPDATE memories SET ... WHERE id = ? AND namespace = ?
  │     │   (supports `clear_ttl=True` to set `ttl_days = NULL, decay_start_at = NULL`)
  │     ├── Sync updated text/tags in `memories_fts` using integer `rowid` (O(log N))
  │     ├── If content changed or `local_only=True`:
  │     │     ├── Scrub old content in `contradictions` table (`new_content` & `existing_content`)
  │     │     └── Scrub old content in `audit_log` via `scrub_memory_update_from_audit()`
  │     ├── Update `_VectorCacheEntry` matrix row & pre-tokenized lexical/IDF cache in-place
  │     └── Refresh tracked composite stamp
  ├─► log_update() -> `audit_<namespace>.db`
  └─► return {"memory_id": resolved_id, "status": "updated", "memory": enriched_row}
```

---

## Module Deep Dives

### `__init__.py`

**Purpose:** Zero-overhead PEP 562 lazy public API surface.
- Defines `__getattr__` and `_LAZY_EXPORTS` so `import mesh` or running lightweight CLI commands never eagerly imports `numpy`, `onnxruntime`, `fastapi`, or `sqlite3` until the specific symbol is accessed.

### `store.py`

**Purpose:** Unified persistence and 5-stage hybrid retrieval engine (`~/.mesh/mesh_<namespace>.db`).
- **Strict Validation:** Validates namespaces via `validate_namespace()` and memory/contradiction IDs via `validate_memory_id()` (`^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`).
- **Pre-Tokenized Lexical & IDF Cache (`_VectorCacheEntry`):** Pre-computes per-row normalized text, compact tokens, stems (`lexical_by_id`), and corpus document frequencies (`stem_df`) once when a row is loaded or upserted. `hybrid_query()` computes IDF in $O(\vert{}Q\vert{})$ time instead of re-tokenizing the entire database on every query.
- **$O(1)$ Healing Set (`needs_healing_ids`):** Tracks any row embedded with `"hash-384"` or `"needs-reembed"` in a set on `_VectorCacheEntry`, eliminating full-matrix scans on `learn()` and `recall()`.
- **$O(\log N)$ `rowid` FTS5 Indexing:** Links `memories_fts.rowid` directly to `memories.rowid` so FTS updates and deletes execute via `WHERE rowid = ?` B-tree lookups instead of full-table scans on an `UNINDEXED` UUID column.
- **Copy-on-Write Row Updates:** `increment_access_batch()` and `record_recall_telemetry()` replace row dictionaries via `{**rm, ...}` rather than mutating shared dicts in-place, preventing data races with concurrent snapshot readers.

### `memory.py`

**Purpose:** Public-facing `Mesh` orchestration class and embedding lifecycle manager.
- **Direct ONNX Runtime Engine (`_DirectOnnxEmbedding`):** Uses `onnxruntime.InferenceSession` + `tokenizers.Tokenizer` with automatic overlapping multi-window mean pooling for long texts (>256 tokens).
- **SHA-256 Verified Downloads with `y/n` Consent:** Verifies downloaded `model.onnx` and `tokenizer.json` files against pinned 64-character SHA-256 digests (`_ALLOWED_MODEL_SHA256` and `_ALLOWED_TOKENIZER_SHA256`) with a 200 MB stream/tarball extraction cap. Prompts the user once per session for `y/n` permission before downloading.
- **Atomic `(vector, model_tag)` Caching:** `_EMBED_CACHE` stores `(vector_tuple, model_tag)` so background ONNX hot-swaps can never mislabel a `hash-384` vector as `onnx-minilm-l6-v2`.

### `audit.py`

**Purpose:** Fault-isolated audit trail (`~/.mesh/audit_<namespace>.db`) and POSIX permission enforcement (`ensure_secure_mesh_dir` mode `0700`, `secure_file_permissions` mode `0600`).
- Provides `scrub_memory_update_from_audit()` and `purge_memory_from_audit()` so edited or deleted secrets never linger in historical `learn`, `update`, or `recall.top_result` rows.
- Automatically prunes `audit_log` to `MESH_MAX_AUDIT_ROWS` (default `10,000`).

### `context_builder.py`

**Purpose:** Single source of truth (`build_context_bundle()` and `build_context()`) for formatting memories into system prompt blocks across CLI, HTTP, and MCP.
- Calls `mesh.recall(..., record_telemetry=False)` so building context blocks never inflates `access_count` in a feedback loop.
- Collapses all newlines and control characters into single spaces, strips leading markdown heading/bullet tokens, and neutralizes case-insensitive `<mesh_context` tags (even without a closing `>`).

### `passive.py`

**Purpose:** Extracts concrete memories from session logs (`extract_memories_from_log()` and `mesh capture`).
- Checks custom `MESH_AI_API_KEY` + `MESH_AI_BASE_URL` overrides first in `detect_provider()`.
- Scrubs secrets via `_redact_secrets_for_cloud()` and asks for interactive `y/n` user permission before sending any log text to external cloud LLM APIs.
- Discards partial leading lines when seeking into large `.jsonl` or `.log` files.

### `digest.py` & `namespaces.py`

**Purpose:** Session activity summaries and namespace lifecycle management.
- Both modules delegate staleness checks to `_compute_row_effective_confidence()` in `store.py` so stale counts are identical across the entire system.
- `namespaces.py:_evict_namespace_caches()` evicts cached state across `store`, `audit`, `context_builder`, `http_server`, `mcp_server`, and `dashboard` on namespace delete or rename.

### `http_server.py`, `mcp_server.py`, `dashboard.py`, & `daemon.py`

**Purpose:** Local network, stdio, and web interfaces.
- **No `Host: testserver` Production Bypass:** Authentication is enforced via `verify_request_auth` (`http_server.py`) and `verify_dashboard_auth` (`dashboard.py`). Unit tests can inject a token header or use FastAPI `dependency_overrides`.
- **Dashboard Session Security & XSS Prevention:** `dashboard.py` exchanges the startup `?token=` launch parameter for an `HttpOnly; SameSite=Strict` session cookie (`mesh_dash_session`), strips the token from the browser URL bar, and uses delegated `data-action` / `data-id` attributes instead of inline `onclick` handlers.
- **MCP Daemon Proxy Resilience:** `mcp_server.py` checks cached keep-alive sockets for EOF via `select.select` before writing and always uses fresh connections for mutating tools (`_MUTATING_TOOLS`).

---

## Storage Schema

### `mesh_<namespace>.db` (mode `0600`)

```sql
CREATE TABLE IF NOT EXISTS memories (
    id              TEXT PRIMARY KEY,
    namespace       TEXT NOT NULL,
    content         TEXT NOT NULL,
    confidence      REAL NOT NULL DEFAULT 1.0,
    memory_type     TEXT NOT NULL DEFAULT 'fact',
    source_agent    TEXT NOT NULL DEFAULT 'unknown',
    tags            TEXT NOT NULL DEFAULT '[]',
    created_at      TEXT NOT NULL,
    accessed_at     TEXT NOT NULL,
    access_count    INTEGER NOT NULL DEFAULT 0,
    ttl_days        REAL,
    decay_start_at  TEXT,
    local_only      INTEGER NOT NULL DEFAULT 0,
    embedding       BLOB,
    embedding_model TEXT NOT NULL DEFAULT 'unknown'
);

-- Synchronized by integer `rowid` with `memories.rowid` for O(log N) updates & deletes
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    id UNINDEXED,
    content,
    tags,
    memory_type,
    source_agent,
    tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS queries (
    id              TEXT PRIMARY KEY,
    namespace       TEXT NOT NULL,
    query_text      TEXT NOT NULL,
    agent_id        TEXT NOT NULL DEFAULT 'unknown',
    best_similarity REAL,
    result_count    INTEGER NOT NULL DEFAULT 0,
    timestamp       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contradictions (
    id                  TEXT PRIMARY KEY,
    namespace           TEXT NOT NULL,
    new_memory_id       TEXT NOT NULL,
    new_content         TEXT NOT NULL,
    existing_memory_id  TEXT NOT NULL,
    existing_content    TEXT NOT NULL,
    similarity          REAL NOT NULL,
    detected_at         TEXT NOT NULL,
    resolved            INTEGER NOT NULL DEFAULT 0
);
```

---

## Testing

Run the test suite from the project root:

```bash
pytest tests/ -v
```

When testing FastAPI apps (`http_server.app` or `dashboard.api`) with `TestClient`, either pass `headers=get_auth_headers()` or override the auth dependency:

```python
from fastapi.testclient import TestClient
from mesh.http_server import app, verify_request_auth

app.dependency_overrides[verify_request_auth] = lambda: None
client = TestClient(app)
```
# Mesh — Architecture & Contributor Guide

> How the code is organized, how the high-speed hybrid storage engine works, and how to extend Mesh.

---

## Table of Contents

1. [Folder Structure](#folder-structure)
2. [Module Map](#module-map)
3. [Data Flow Diagrams](#data-flow-diagrams)
   - [learn() flow](#learn-flow)
   - [recall() flow](#recall-flow)
   - [update() flow](#update-flow)
4. [Module Deep Dives](#module-deep-dives)
   - [store.py](#storepy)
   - [memory.py](#memorypy)
   - [audit.py](#auditpy)
   - [context_builder.py](#context_builderpy)
   - [passive.py](#passivepy)
   - [digest.py](#digestpy)
   - [io.py](#iopy)
   - [namespaces.py](#namespacespy)
   - [http_server.py](#http_serverpy)
   - [mcp_server.py](#mcp_serverpy)
   - [dashboard.py](#dashboardpy)
   - [cli.py](#clipy)
   - [version.py](#versionpy)
5. [Storage Schema](#storage-schema)
6. [Performance & Caching Architecture](#performance--caching-architecture)
7. [How to Add a New Feature](#how-to-add-a-new-feature)
8. [How to Add a New CLI Command](#how-to-add-a-new-cli-command)
9. [How to Add a New MCP Tool](#how-to-add-a-new-mcp-tool)
10. [How to Add a New HTTP Endpoint](#how-to-add-a-new-http-endpoint)
11. [How to Add a New Memory Type](#how-to-add-a-new-memory-type)
12. [Testing](#testing)
13. [Key Design Decisions](#key-design-decisions)

---

## Folder Structure

```text
mesh/
├── __init__.py           # Public API surface — imports and re-exports core functions & classes
├── memory.py             # Core Mesh class — learn(), recall(), update(), forget(), patterns(), etc.
├── store.py              # Unified SQLite WAL + in-memory SIMD NumPy matrix + FTS5 BM25 engine
├── audit.py              # Fault-isolated SQLite audit trail — logs recall, learn, update, forget
├── context_builder.py    # Formats memories as system prompt blocks (markdown, plain, xml, json)
├── passive.py            # AI-powered & local regex extraction from agent session logs
├── digest.py             # Timezone-safe UTC session summary generator
├── io.py                 # High-speed batch export/import to and from JSON files
├── namespaces.py         # Namespace discovery, deletion, renaming, and statistics
├── http_server.py        # FastAPI REST API server (port 7701)
├── mcp_server.py         # Production MCP server over stdio + background passive log sync
├── dashboard.py          # FastAPI + zero-build inline HTML/JS web UI (port 7433)
├── cli.py                # Unified `mesh` command router and `mesh-*` CLI entry points
└── version.py            # Package version metadata and non-blocking PyPI update checker

tests/
├── test_memory.py        # Core unit tests: learn, hybrid recall, dedup, conflicts, decay, update
├── test_v03.py           # Privacy flags (local_only), export/import, namespace management
└── test_v04.py           # Audit trail, context builder, digest, HTTP API, MCP tools, passive capture

~/.mesh/                  # All local data lives here (created automatically on first use)
├── mesh_shared.db        # Unified SQLite DB (memories + FTS5 + queries + contradictions)
├── mesh_<namespace>.db   # Separate isolated SQLite DB per namespace
├── audit_shared.db       # Fault-isolated SQLite audit trail for "shared"
└── audit_<namespace>.db  # Separate audit DB per namespace
```

---

## Module Map

Here is how the modules depend on each other:

```text
cli.py
  └── memory.py, io.py, namespaces.py, audit.py, context_builder.py,
      digest.py, passive.py, dashboard.py, http_server.py, mcp_server.py, version.py

http_server.py
  └── memory.py, audit.py, context_builder.py

mcp_server.py
  └── memory.py, audit.py, context_builder.py, passive.py

dashboard.py
  └── memory.py, namespaces.py, io.py, audit.py, digest.py

context_builder.py
  └── memory.py

io.py
  └── store.py, memory.py, passive.py

passive.py
  └── memory.py, audit.py

digest.py
  └── store.py (UTC timestamp helpers; reads SQLite directly)

namespaces.py
  └── store.py (cache locks, paths, and UTC helpers; manages SQLite DB files)

memory.py
  └── store.py

audit.py
  └── (standalone — manages its own `audit_<namespace>.db` SQLite files)

store.py
  └── sqlite3, numpy (zero internal mesh dependencies)

version.py
  └── (standalone — checks PyPI in a background daemon thread)
```

**Core Architectural Rules:**
1. `store.py` is the primary persistence engine for memories, vectors, FTS5 indexes, queries, and contradictions.
2. `memory.py` is the only module that loads `SentenceTransformer` and computes embeddings.
3. All high-level interfaces (`mcp_server.py`, `http_server.py`, `dashboard.py`, `cli.py`) interact with `Mesh` and log events through `audit.py`.

---

## Data Flow Diagrams

### `learn()` flow

```text
caller
  │
  ▼
Mesh.learn(content, memory_type, confidence, tags, ttl_days, local_only, deduplicate=True)
  │
  ├─► Validate content & memory_type (9 valid types)
  │
  ├─► _embed(clean_content)
  │     ├─► Check _EMBED_CACHE (OrderedDict LRU, maxsize=4096) → <0.01ms hit
  │     └─► On miss: SentenceTransformer("all-MiniLM-L6-v2") under torch.inference_mode()
  │
  ├─► MeshStore.count() > 0? (O(1) via in-memory _VectorCacheEntry.all_ids)
  │     └── YES → MeshStore.query(embedding, n=3) (SIMD cosine dot product)
  │           │
  │           ├─► Pass 1 (Deduplication, if deduplicate=True):
  │           │     _is_true_duplicate(clean_content, doc, similarity)?
  │           │     (exact text match OR similarity ≥ 0.985 with same numbers & negation polarity)
  │           │     └── YES → Merge tags, take max(confidence), call MeshStore.update() in-place
  │           │               └─► return {"memory_id": existing_id, "status": "stored", "deduplicated": True}
  │           │
  │           └─► Pass 2 (Contradiction Detection):
  │                 similarity ≥ 0.82?
  │                 └── YES → MeshStore.log_contradiction(new_memory_id, ...) → conflict dict
  │
  ├─► MeshStore.upsert(memory_id, content, embedding, metadata, local_only)
  │     ├── Normalize vector to contiguous float32 BLOB
  │     ├── Single SQLite WAL transaction:
  │     │     ├── INSERT OR REPLACE INTO memories (...)
  │     │     └── Sync row in memories_fts (FTS5 virtual table)
  │     ├── Mutate _VectorCacheEntry in-place in RAM (append/update row in NumPy matrix)
  │     └── Refresh tracked (mtime_ns, file_size) stamp so cache stays hot
  │
  └─► return {"memory_id": memory_id, "status": "stored" | "stored_with_conflict", "deduplicated": False, "conflict": conflict}
```

### `recall()` flow

```text
caller
  │
  ▼
Mesh.recall(query, n, min_similarity, min_confidence, include_stale, memory_type, tags, source_agent)
  │
  ├─► _embed(clean_query) → _EMBED_CACHE or SentenceTransformer
  │
  ├─► MeshStore.hybrid_query(query_text, embedding, n=max(n*3, 15), ...)
  │     ├── 1. Dense SIMD Cosine Search: sims = cache.matrix @ q_vec (in-memory float32 dot product)
  │     ├── 2. Single SQLite connection pass:
  │     │      ├── FTS5 BM25 lexical search over `memories_fts` (_fts_lexical_scores)
  │     │      └── Fetch metadata rows for top dense ∪ lexical candidates with SQL pre-filters
  │     └── 3. Exact Token / Identifier Boost & Score Fusion:
  │            keyword_signal = max(bm25_score, exact_token_overlap)
  │            hybrid_sim = min(1.0, sem_sim + (1.0 - sem_sim) * 0.28 * keyword_signal)
  │
  ├─► For each candidate (using a single UTC `now` reference):
  │     ├── _compute_decay_metrics(ttl_days, decay_start_at, now)
  │     │     ├── decay_factor = clamp(1.0 - days_elapsed / ttl_days, 0.0, 1.0)
  │     │     └── days_until_stale = max(0, round(ttl_days * 0.7 - days_elapsed))
  │     ├── effective_confidence = round(stored_confidence * decay_factor, 4)
  │     ├── is_stale = effective_confidence < 0.3
  │     ├── Filter by min_similarity, include_stale, and min_confidence
  │     └── Compute composite_score = 0.75 * similarity + 0.18 * effective_confidence + 0.07 * usage_boost
  │
  ├─► Sort by (similarity, effective_confidence, access_count) DESC and slice [:n]
  │
  ├─► MeshStore.record_recall_telemetry(returned_ids, query_text, agent_id, best_similarity, len(memories))
  │     ├── Single atomic SQLite WAL transaction:
  │     │     ├── UPDATE memories SET access_count = access_count + 1, accessed_at = ? WHERE id IN (...)
  │     │     └── INSERT INTO queries (...)
  │     └── Refresh tracked (mtime_ns, file_size) stamp so _VECTOR_CACHE & _INITIALIZED_DBS remain valid
  │
  └─► return memories
```

### `update()` flow

```text
caller
  │
  ▼
Mesh.update(memory_id, content, memory_type, confidence, tags, ttl_days, reset_ttl, local_only)
  │
  ├─► MeshStore.get(memory_id) → verify memory exists
  ├─► Re-compute _embed(content) ONLY if `content` was provided and changed
  ├─► MeshStore.update(...)
  │     ├── UPDATE memories SET ... WHERE id = ? AND namespace = ? (preserves id, created_at, access_count)
  │     ├── Sync updated text/tags in `memories_fts`
  │     ├── If embedding changed: overwrite row `cache.matrix[idx] = norm_vec` in-place
  │     └── Refresh tracked (mtime_ns, file_size) stamp
  └─► return {"memory_id": memory_id, "status": "updated", "memory": enriched_row}
```

---

## Module Deep Dives

### `store.py`

**Purpose:** Unified persistence and hybrid retrieval engine. Everything is stored atomically in `~/.mesh/mesh_<namespace>.db` using SQLite in WAL mode.

**Three integrated subsystems inside one SQLite file:**
1. **Relational Metadata & Vector BLOBs (`memories` table):** Stores metadata alongside L2-normalized 384-dimensional `float32` vectors serialized as compact 1,536-byte binary BLOBs.
2. **In-Memory SIMD Vector Cache (`_VectorCacheEntry`):** Maintains a contiguous 2D `np.float32` matrix (`shape = (N, 384)`) per namespace so cosine similarity across all memories is a single matrix-vector multiplication (`cache.matrix @ q_vec`).
3. **Lexical BM25 Search (`memories_fts` virtual table):** Uses SQLite FTS5 (`tokenize='porter unicode61'`) to score keyword matches and boost exact technical identifiers (error codes, ports, paths, function names).

**Connection Management (`_ManagedConnection`):**
- Subclasses `sqlite3.Connection` so exiting any `with self._connect() as conn:` block commits/rolls back the transaction **and** deterministically calls `self.close()`.
- Prevents lingering file handles or memory-mapped section locks (`WinError 32`) on Windows during namespace deletion or renaming.

**Legacy Migration:**
- `_migrate_from_chroma_if_needed()` checks if `~/.mesh/chroma` exists and automatically backfills any missing SQLite `embedding` BLOBs from legacy ChromaDB collections on startup.
- `_CollectionCompatShim` (`store.collection`) emulates the legacy ChromaDB collection interface (`get`, `upsert`, `query`, `delete`, `count`) for backward compatibility.

---

### `memory.py`

**Purpose:** Public-facing `Mesh` orchestration class and embedding lifecycle manager.

**Key Constants:**
- `_MODEL_NAME = "all-MiniLM-L6-v2"`: 384-dimensional local embedding model.
- `CONTRADICTION_THRESHOLD = 0.82`: Cosine similarity threshold above which non-duplicate memories are flagged as potential conflicts.
- `DEDUP_THRESHOLD = 0.985`: Cosine similarity threshold above which semantically identical facts (with matching numbers and negation polarity) are deduplicated in-place.
- `STALE_THRESHOLD = 0.3`: Effective confidence threshold below which a TTL-enabled memory is marked stale.
- `VALID_MEMORY_TYPES`: `{"fact", "preference", "context", "result", "instruction", "process", "decision", "error", "general"}`.

**Shared Single & Batch Embedding LRU Cache (`_EMBED_CACHE`):**
- Uses a thread-safe `OrderedDict` capped at `4,096` entries.
- Both `_embed()` (single string) and `_embed_batch()` (multiple strings during imports) read from and populate `_EMBED_CACHE`.
- Deduplicates identical strings within the same batch before running `model.encode()` under `torch.inference_mode()`.

**Smart Deduplication vs. Contradiction Detection (`_is_true_duplicate`):**
- Exact normalized text matches always deduplicate.
- Near-identical paraphrases (`similarity >= 0.985`) only deduplicate if all numeric tokens (`_NUMBER_TOKEN_RE`) and negation tokens (`_NEGATION_TOKENS`) match.
- Thus, `"The API rate limit is 100 req/min"` vs. `"The API rate limit is 200 req/min"` is accurately caught as a **contradiction** rather than deduplicated.

---

### `audit.py`

**Purpose:** Fault-isolated, high-speed audit log for all read and write operations (`recall`, `learn`, `update`, `forget`).

**Key Features:**
- Writes to `~/.mesh/audit_<namespace>.db` so audit activity is completely isolated from `mesh_<namespace>.db`.
- Every logging function (`log_recall`, `log_learn`, `log_update`, `log_forget`) is wrapped in `try/except Exception: pass` so audit I/O can never interrupt agent execution.
- `_write_audit_entry()` initializes schema (if needed) and inserts the log record in a single connection pass, then updates `_INITIALIZED_AUDIT_DBS[db_path]` with the new `st_mtime_ns` so subsequent writes never re-run `CREATE TABLE` / `CREATE INDEX`.

---

### `context_builder.py`

**Purpose:** Formats top memories into system prompt blocks ready for injection into LLM contexts.

**Supported Formats:**
- `markdown` (default): Groups memories by `memory_type` under bold headings inside `<mesh_context>` tags, marking stale items with `*(possibly stale)*`.
- `plain`: Bullet list under `[MESH CONTEXT — Shared Agent Memory]`.
- `xml`: Structured `<mesh_context><memory type="..." stale="true">...</memory></mesh_context>`.
- `json`: Indented JSON object containing `namespace` and enriched memory dictionaries.

**Selection Logic:**
- When `query` is provided, calls `mesh.recall()` using hybrid search.
- When `query` is `None`, calls `mesh.inspect(limit=max(count * 3, 30))`, filters by `min_confidence` and `tags`, and sorts by `(access_count, effective_confidence)` descending so frequently used project facts surface first.

---

### `passive.py`

**Purpose:** Extracts concrete, reusable memories from raw session logs, `.log`/`.txt`/`.md` files, `.jsonl` transcripts, or directories of agent logs.

**Provider Auto-Detection (`detect_provider()`):**
1. Explicit `MESH_AI_PROVIDER` override (`anthropic`, `openai`, `groq`, `ollama`, `openai-compatible`, `local`).
2. `ANTHROPIC_API_KEY` (`claude-haiku-4-5-20251001`).
3. `OPENAI_API_KEY` (`gpt-4o-mini`).
4. `GROQ_API_KEY` (`llama3-8b-8192`).
5. Local Ollama instance at `http://localhost:11434/v1`.
6. Local zero-dependency regex/heuristic fallback (`_extract_local()`).

**Privacy Protection:**
- Automatically scans extracted sentences against `_CREDENTIAL_PATTERNS` (`password`, `api_key`, `secret`, `token`, `sk-...`, `Bearer ...`) and forces `local_only=True` so secrets are never exported.

---

### `digest.py`

**Purpose:** Generates timezone-safe UTC activity summaries over a configurable window (`--hours 24`).

- Queries `mesh_<namespace>.db` and `audit_<namespace>.db` directly using `_utc_now()` and `_parse_iso_utc()` without loading the embedding model or polluting query/audit logs.
- Reports new memories stored, total recalls, active agents, contradictions flagged, and memories whose TTL has expired.

---

### `io.py`

**Purpose:** High-speed namespace export and import (`mesh-export` and `mesh-import`).

- **`export_namespace()`:** Uses $O(1)$ `store.count()` and `store.get_exportable_memories()` to export all non-private (`local_only = 0`) memories in a single pass. Batch-fetches any fallback embeddings in one query if `include_embeddings=True`.
- **`import_namespace()`:** Resolves existing memory IDs in $O(1)$ from the store's vector cache (`all_ids`), batch-computes any missing embeddings in a single forward pass via `_embed_batch()`, and writes all imported memories in a single SQLite WAL transaction via `store.upsert_batch()`.

---

### `namespaces.py`

**Purpose:** Namespace-level management (`list_namespaces`, `delete_namespace`, `rename_namespace`, `namespace_stats`).

- Discovers namespaces directly from `~/.mesh/mesh_*.db` files on disk.
- Evicts in-memory caches (`_VECTOR_CACHE`, `_INITIALIZED_DBS`, `_TRANSIENT_EMBEDDINGS`) under `_CACHE_LOCK` and runs `PRAGMA wal_checkpoint(TRUNCATE)` before copying or unlinking SQLite files.
- `_remove_sqlite_files()` cleanly removes `.db`, `-wal`, and `-shm` files with garbage-collection retry safety for Windows.

---

### `http_server.py`

**Purpose:** FastAPI REST API server running on `127.0.0.1:7701` (`mesh-http` / `mesh-server-http`).

- Caches `(namespace, agent_id)` `Mesh` instances in `_MESH_REGISTRY` with a lock-free fast path.
- Pre-warms the embedding model and default namespace vector cache in a background thread on startup.
- Exposes `/health`, `/learn`, `/recall`, `/update`, `/forget`, `/inspect`, `/patterns`, `/contradictions`, `/contradictions/resolve`, `/context` (single-pass retrieval + formatting), and `/audit`.

---

### `mcp_server.py`

**Purpose:** Production Model Context Protocol (MCP) server over `stdio` (`mesh-server`).

- Pre-builds all 10 `types.Tool` schemas at module import time (`_MCP_TOOLS`) for instant `list_tools()` responses.
- Runs synchronous tool handlers in `asyncio.to_thread()` wrapped in `_safe_redirect_stdout_to_stderr()`—a lock-protected, reference-counted context manager that guarantees `sys.stdout` is never corrupted by concurrent threads or print statements while `stdio_server` is active.
- Includes `background_sync_worker()`, which pre-warms the model and periodically tails newly appended bytes from Claude Desktop and Antigravity agent logs using file `(mtime_ns, byte_offset)` watermarks (`_LOG_WATERMARKS`).

---

### `dashboard.py`

**Purpose:** Minimal, responsive web UI running on `127.0.0.1:7433` (`mesh-dashboard`).

- Zero frontend build step: serves `DASHBOARD_HTML` from a single self-contained Python module with FastAPI `/api/*` endpoints.
- Supports live hybrid search, type filtering, sorting, in-place memory editing (`PUT /api/memories/{memory_id}`), conflict resolution, pattern inspection, audit log filtering, namespace switching/exporting, and 24-hour digests.

---

### `cli.py`

**Purpose:** Command-line interface providing both the unified `mesh <subcommand>` dispatcher (`main()`) and standalone `mesh-*` scripts (`mesh-add`, `mesh-ask`, `mesh-update`, `mesh-forget`, `mesh-context`, `mesh-why`, `mesh-status`, `mesh-namespaces`, `mesh-export`, `mesh-import`, `mesh-capture`, `mesh-digest`, `mesh-server`, `mesh-dashboard`, `mesh-http`, `mesh-help`).

---

### `version.py`

**Purpose:** Stores `VERSION = "0.5.3"` and runs a non-blocking background daemon check against PyPI (`check_for_updates()`) during `mesh-status` and `mesh-help` (can be disabled with `MESH_NO_UPDATE_CHECK=1`).

---

## Storage Schema

### `mesh_<namespace>.db`

```sql
CREATE TABLE IF NOT EXISTS memories (
    id              TEXT PRIMARY KEY,
    namespace       TEXT NOT NULL,
    content         TEXT NOT NULL,
    confidence      REAL NOT NULL DEFAULT 1.0,
    memory_type     TEXT NOT NULL DEFAULT 'fact',
    source_agent    TEXT NOT NULL DEFAULT 'unknown',
    tags            TEXT NOT NULL DEFAULT '[]',   -- JSON array stored as text
    created_at      TEXT NOT NULL,                -- UTC ISO-8601
    accessed_at     TEXT NOT NULL,                -- UTC ISO-8601
    access_count    INTEGER NOT NULL DEFAULT 0,
    ttl_days        REAL,                         -- NULL = permanent (no decay)
    decay_start_at  TEXT,                         -- UTC ISO-8601 or NULL
    local_only      INTEGER NOT NULL DEFAULT 0,   -- 0 = exportable, 1 = private
    embedding       BLOB                          -- L2-normalized float32 vector (1536 bytes)
);

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

### `audit_<namespace>.db`

```sql
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    action          TEXT NOT NULL,           -- 'recall', 'learn', 'update', or 'forget'
    agent_id        TEXT,
    namespace       TEXT,
    query           TEXT,                    -- for recalls: the query string
    content         TEXT,                    -- for learns/updates/forgets: content or summary
    top_result      TEXT,                    -- for recalls: top result content
    top_similarity  REAL,                    -- for recalls: top result similarity
    result_count    INTEGER,                 -- for recalls: number of results
    memory_id       TEXT,                    -- for learns/updates/forgets: target memory UUID
    had_conflict    INTEGER DEFAULT 0        -- for learns: 1 if contradiction detected
);
```

---

## Performance & Caching Architecture

Mesh is engineered for sub-millisecond cached operations and single-digit millisecond uncached operations:

1. **In-Place Vector Matrix Mutations (`_VectorCacheEntry`):**
   - `upsert()`, `upsert_batch()`, `update()`, and `delete()` mutate the in-memory `np.float32` matrix directly in RAM (`upsert_vector` and `delete_vector`) rather than evicting the cache and rescanning SQLite.
2. **Self-Write File-Stat Synchronization (`_refresh_stamps_after_write_locked`):**
   - After any internal SQLite commit (`upsert`, `update`, `delete`, `record_recall_telemetry`, `log_query`, `log_contradiction`, `resolve_contradiction`), `MeshStore` immediately updates `_INITIALIZED_DBS[db_path]` and `synced_cache.(mtime_ns, file_size)` with the new file stat.
   - In-process writes never trigger false cache invalidations or schema re-initializations, while external writes from other processes are still detected via `db_path.stat()`.
3. **Single-Connection Hybrid Search & Single-Transaction Telemetry:**
   - `hybrid_query()` runs both FTS5 BM25 scoring and candidate metadata retrieval on a single SQLite connection.
   - `record_recall_telemetry()` combines `access_count` batch increments and query logging into one atomic WAL commit.
4. **Shared `OrderedDict` Embedding Cache (`_EMBED_CACHE`):**
   - Repeated queries and duplicate facts resolve their 384-dim normalized embedding in `<0.01 ms` without invoking PyTorch.

---

## How to Add a New Feature

1. **Storage layer (`mesh/store.py`):** If a new column is needed, add it to `CREATE TABLE IF NOT EXISTS memories` and the `ALTER TABLE` migration loop in `_init_db()`, include it in `_MEMORY_COLUMNS`, and handle it in `upsert()`, `upsert_batch()`, and `update()`.
2. **Core logic (`mesh/memory.py`):** Expose the parameter or method on `Mesh` and enrich returned rows in `_enrich_memory_row()` if needed.
3. **Interfaces (`mcp_server.py`, `http_server.py`, `cli.py`, `dashboard.py`):** Wire the feature into the MCP tool schema, FastAPI request model, CLI parser, or dashboard UI.
4. **Public exports (`mesh/__init__.py`):** Export any new top-level helper functions in `__all__`.
5. **Tests (`tests/`):** Add unit and integration tests with proper `_cleanup_ns()` teardown.

---

## How to Add a New CLI Command

1. Add a command function in `mesh/cli.py`:

```python
def myfeature_cmd() -> None:
    """Entry point for: mesh-myfeature"""
    parser = argparse.ArgumentParser(
        description="Short description of myfeature.",
        epilog="Example: mesh-myfeature --namespace work",
    )
    parser.add_argument("--namespace", default=None)
    args = parser.parse_args()

    ns = _resolve_namespace(args.namespace)
    # ... execute command ...
```

2. Add the subcommand routing entry to `mapping` inside `mesh/cli.py:main()` and document it in `help_cmd()`.
3. Register the script entry point in `pyproject.toml` under `[project.scripts]`:

```toml
mesh-myfeature = "mesh.cli:myfeature_cmd"
```

4. Re-install editable package metadata: `pip install -e .`

---

## How to Add a New MCP Tool

1. Add a `types.Tool` definition to `_MCP_TOOLS` in `mesh/mcp_server.py`:

```python
types.Tool(
    name="mesh_myfeature",
    description="Clear description for AI agents explaining when and how to call this tool.",
    inputSchema={
        "type": "object",
        "properties": {
            "param1": {"type": "string", "description": "..."},
            "namespace": {"type": "string", "description": "Optional namespace override."},
        },
        "required": ["param1"],
    },
)
```

2. Add an `elif name == "mesh_myfeature":` branch inside `_execute_tool_sync()` in `mesh/mcp_server.py`.
3. Update `expected_tools` in `tests/test_v04.py:test_mcp_server_tools_and_execution()`.

---

## How to Add a New HTTP Endpoint

1. Define a Pydantic request model (for `POST`/`PUT` routes) in `mesh/http_server.py`:

```python
class MyFeatureRequest(BaseModel):
    param1: str
    namespace: Optional[str] = None
    agent_id: Optional[str] = None
```

2. Add the FastAPI route handler in `mesh/http_server.py`:

```python
@app.post("/myfeature")
async def myfeature(req: MyFeatureRequest) -> dict[str, Any]:
    try:
        mesh = get_mesh(req.namespace, req.agent_id)
        # ... call mesh method ...
        return {"status": "ok", "result": ...}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
```

3. Add the route path to `required` in `tests/test_v04.py:test_http_endpoints_exist()`.

---

## How to Add a New Memory Type

1. Add the type string to `VALID_MEMORY_TYPES` in `mesh/memory.py` (all CLI `choices` and MCP `enum` lists derive from `sorted(VALID_MEMORY_TYPES)` automatically).
2. Optional: Add any heuristic alias mappings to `_TYPE_ALIASES` and `_infer_type()` in `mesh/passive.py`.
3. Optional: Add a `.type-pill.<newtype>` color rule and filter chip in `mesh/dashboard.py`.

---

## Testing

Run the full test suite from the project root:

```bash
# Run all 79 tests across test_memory.py, test_v03.py, and test_v04.py
pytest tests/ -v

# Run a specific test file
pytest tests/test_v04.py -v

# Run with coverage report
pytest tests/ --cov=mesh --cov-report=term-missing
```

**Test Isolation Pattern:**
Always use a unique namespace and clean up both the memory DB and audit DB in a `finally` block (or fixture teardown):

```python
def test_custom_feature():
    ns = f"test_custom_{uuid.uuid4().hex[:8]}"
    try:
        mesh = Mesh(namespace=ns, agent_id="test-agent")
        # ... assertions ...
    finally:
        _cleanup_ns(ns)
```

---

## Key Design Decisions

**Why Unified SQLite WAL + In-Memory NumPy SIMD + FTS5 Instead of ChromaDB?**
Storing metadata, FTS5 lexical indexes, and normalized `float32` vector BLOBs in a single SQLite WAL file per namespace eliminates multi-database synchronization bugs, removes heavy external C++/Rust binary dependencies, makes namespace backups/renames atomic file operations, and allows in-memory NumPy SIMD matrix multiplication (`cache.matrix @ q_vec`) to execute cosine similarity searches in `<0.1 ms`. Combining dense cosine similarity with SQLite FTS5 BM25 and exact token overlap gives agents both semantic understanding and exact identifier precision.

**Why `all-MiniLM-L6-v2`?**
It provides an ideal balance of semantic accuracy, compact 384-dimensional vectors (1.5 KB per memory as `float32`), zero API cost, offline privacy, and `<8 ms` single-sentence CPU inference.

**Why Deduplicate Identical Facts While Storing Contradictions?**
When agents repeatedly observe the exact same fact, creating duplicate rows pollutes recall results; reinforcing the existing memory in-place (merging tags and preserving its UUID) keeps the store clean. Conversely, when a new fact has high semantic similarity ($\ge 0.82$) but different numbers or negation polarity, Mesh stores the new memory and flags a contradiction record so the agent or human can resolve the conflict without silent data loss.

**Why Separate `audit_<namespace>.db` from `mesh_<namespace>.db`?**
Audit logging is high-frequency observability data. Isolating it in its own WAL database ensures audit writes never contend with memory transactions and allows audit logs to be inspected or rotated independently.
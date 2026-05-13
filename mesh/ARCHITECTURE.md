# Mesh — Architecture & Contributor Guide

> How the code is organized, how the pieces fit together, and how to extend Mesh.

---

## Table of Contents

1. [Folder Structure](#folder-structure)
2. [Module Map](#module-map)
3. [Data Flow Diagrams](#data-flow-diagrams)
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
5. [Storage Schema](#storage-schema)
6. [How to Add a New Feature](#how-to-add-a-new-feature)
7. [How to Add a New CLI Command](#how-to-add-a-new-cli-command)
8. [How to Add a New MCP Tool](#how-to-add-a-new-mcp-tool)
9. [How to Add a New HTTP Endpoint](#how-to-add-a-new-http-endpoint)
10. [How to Add a New Memory Type](#how-to-add-a-new-memory-type)
11. [Testing](#testing)
12. [Key Design Decisions](#key-design-decisions)

---

## Folder Structure

```
mesh/
├── __init__.py           # Public API surface — imports and re-exports everything
├── memory.py             # Core Mesh class — learn(), recall(), patterns(), etc.
├── store.py              # Persistence layer — ChromaDB + SQLite abstraction
├── audit.py              # Separate audit DB — logs every read and write
├── context_builder.py    # Formats memories as system prompt blocks
├── passive.py            # AI-powered extraction from conversation logs
├── digest.py             # Session summary generator
├── io.py                 # Export/import to/from JSON files
├── namespaces.py         # Namespace listing, deletion, renaming, stats
├── http_server.py        # FastAPI REST API (port 7701)
├── mcp_server.py         # MCP server — exposes tools to Claude Desktop/Cursor
├── dashboard.py          # FastAPI + HTML dashboard (port 7433)
└── cli.py                # Click-based CLI entry points

~/.mesh/                  # All data lives here (created at first use)
├── chroma/               # ChromaDB persistent storage (vector embeddings)
│   └── ...               # ChromaDB internal files
├── mesh_shared.db        # SQLite — memories + queries + contradictions for "shared" namespace
├── mesh_my-project.db    # SQLite — separate DB per namespace
├── audit_shared.db       # SQLite — audit trail for "shared" namespace
└── audit_my-project.db   # SQLite — separate audit DB per namespace
```

---

## Module Map

Here is how the modules depend on each other:

```
cli.py
  └── memory.py, io.py, namespaces.py, audit.py, context_builder.py, digest.py, passive.py

http_server.py
  └── memory.py, store.py, audit.py, context_builder.py

mcp_server.py
  └── memory.py, audit.py

dashboard.py
  └── memory.py, namespaces.py, io.py, audit.py, digest.py

memory.py
  └── store.py

context_builder.py
  └── memory.py

digest.py
  └── (reads SQLite directly — no dependency on memory.py)

io.py
  └── store.py, memory.py

namespaces.py
  └── (reads SQLite directly — no dependency on memory.py)

audit.py
  └── (standalone — reads/writes its own DB files)

passive.py
  └── memory.py

store.py
  └── chromadb, sqlite3  (no internal mesh dependencies)
```

**Rule:** `store.py` is the only module that touches the database. `memory.py` is the only module that creates embeddings. All other modules use `Mesh` or `MeshStore` as their interface.

---

## Data Flow Diagrams

### learn() flow

```
caller
  │
  ▼
Mesh.learn(content, ...)
  │
  ├─► _embed(content)                  → SentenceTransformer → [float, ...]
  │
  ├─► MeshStore.query(embedding, n=3)  → check for contradictions
  │     └── similarity ≥ 0.82?
  │           ├── YES → MeshStore.log_contradiction() → contradiction_id
  │           └── NO  → conflict = None
  │
  ├─► MeshStore.upsert(id, content, embedding, metadata, local_only)
  │     ├── ChromaDB.collection.upsert(...)    → vector index
  │     └── SQLite INSERT INTO memories (...)  → metadata row
  │
  └─► return {"memory_id": ..., "status": ..., "conflict": ...}
```

### recall() flow

```
caller
  │
  ▼
Mesh.recall(query, n, min_similarity, ...)
  │
  ├─► _embed(query)                        → SentenceTransformer → [float, ...]
  │
  ├─► MeshStore.query(embedding, n)        → ChromaDB cosine search
  │     └── returns: ids, documents, distances, metadatas
  │
  ├─► for each result:
  │     ├── similarity = 1.0 - distance
  │     ├── _compute_decay_factor(ttl_days, decay_start_at)
  │     ├── effective_confidence = confidence × decay_factor
  │     ├── is_stale = effective_confidence < 0.3
  │     ├── filter by min_similarity, min_confidence, include_stale
  │     └── MeshStore.increment_access(id)  → SQLite UPDATE
  │
  ├─► MeshStore.log_query(query, agent_id, best_similarity, result_count)
  │     └── SQLite INSERT INTO queries (...)
  │
  └─► return sorted(memories, by=similarity desc)
```

---

## Module Deep Dives

### `store.py`

**Purpose:** The only module that touches persistent storage. Everything else goes through this.

**Two backends:**
- `chromadb.PersistentClient` — stores embeddings, enables cosine similarity search
- `sqlite3` — stores all metadata (content, type, confidence, tags, TTL, access counts)

**Why both?** ChromaDB is optimised for vector search but its metadata storage is limited (string/int/float/bool only, no lists). SQLite gives us full SQL query power over metadata — needed for pattern detection, gap analysis, TTL calculations, and filtering by type/tag/privacy.

**Key methods:**

```python
upsert(memory_id, content, embedding, metadata, local_only)
    # Writes to both ChromaDB (vectors) and SQLite (metadata)
    # local_only becomes integer 0/1 in SQLite and is also in chroma_meta

query(embedding, n)
    # ChromaDB cosine search — returns raw dict with ids/docs/distances/metadatas
    # Handles the edge case of empty collection (count=0)

log_query(query_text, agent_id, best_similarity, result_count)
    # Writes every recall() to the queries table for pattern analysis

log_contradiction(new_memory_id, new_content, existing_memory_id, existing_content, similarity)
    # Writes conflict record to contradictions table, returns contradiction_id

get_patterns()
    # Pure SQL — aggregates queries table (top topics, gaps), memories table (unused, stale)
    # This is the only place decay math is re-run in SQL
```

**To add a new metadata field** (e.g. `source_url`):
1. Add to the SQLite schema in `_init_db()` — include a migration `ALTER TABLE ... ADD COLUMN` block
2. Add to the `upsert()` method's INSERT statement
3. Add to `list_all()` return (it already uses `SELECT *` so this is automatic)
4. Surface it in `Mesh.learn()` parameters

---

### `memory.py`

**Purpose:** The public-facing class. Orchestrates embeddings, contradiction checks, and storage. The only module that creates embeddings.

**Key globals:**
```python
_model: Optional[SentenceTransformer]  # singleton — loaded once, reused
_MODEL_NAME = "all-MiniLM-L6-v2"       # ~90MB, fast, good quality
CONTRADICTION_THRESHOLD = 0.82          # cosine similarity for conflict detection
STALE_THRESHOLD = 0.3                   # effective_confidence below this = stale
VALID_MEMORY_TYPES = {...}              # validated in learn()
```

**Embedding is lazy-loaded:** `_get_model()` only downloads/loads the model on first call. Subsequent calls reuse the global `_model` object. This means the first `learn()` or `recall()` in a session takes a few seconds; all subsequent calls are fast.

**Contradiction detection threshold:** 0.82 was chosen to catch semantically opposite or conflicting statements while allowing genuinely related-but-different memories to coexist. For example, "API limit is 100 req/min" vs "API limit is 200 req/min" scores ~0.91. "Python is our language" vs "We use TypeScript" scores ~0.78 (below the threshold — these coexist fine). You can tune this.

**Decay calculation** (`_compute_decay_factor`):
```python
decay_factor = max(0.0, 1.0 - (days_elapsed / ttl_days))
```
Linear, from 1.0 at creation to 0.0 at `ttl_days`. Stale threshold (0.3) is hit at `ttl_days * 0.7` days.

---

### `audit.py`

**Purpose:** Standalone audit trail. Every `recall()` and `learn()` call — whether through the Python API, MCP, HTTP, or CLI — should log here.

**Separate database:** audit logs live in `audit_<namespace>.db`, completely separate from `mesh_<namespace>.db`. This means audit logging can never corrupt memory data, and the audit DB can be deleted without losing memories.

**Audit logging is fire-and-forget:** every logging function wraps its body in `try/except: pass`. A logging failure must never crash a `learn()` or `recall()` call.

**Who logs?**
- `memory.py` does NOT call audit logging directly — this keeps the core clean
- `mcp_server.py` calls `log_recall()` and `log_learn()` after tool invocations
- `http_server.py` calls them after each endpoint handler
- `cli.py` implicitly logs because it uses `Mesh` which uses `MeshStore.log_query()` internally (the query log is different from the audit log — queries go to `mesh_*.db`, audit goes to `audit_*.db`)

**Note for contributors:** if you add a new entry point (e.g. a WebSocket server), you need to call `log_recall()` / `log_learn()` manually after each operation.

---

### `context_builder.py`

**Purpose:** Takes memories and formats them as a system prompt block ready to paste into any agent's context.

**Format options:**
- `markdown` — default; groups by memory type with bold headers, uses `<mesh_context>` XML wrapper
- `plain` — simple bullet list under `[MESH CONTEXT]` header
- `xml` — `<mesh_context><memory type="fact">...</memory></mesh_context>`
- `json` — raw JSON dict with all memory metadata

**Query vs no-query:** if a `query` is passed, it calls `mesh.recall()` (semantic search). If no query, it calls `mesh.inspect()`, sorts by `access_count` descending, and takes the top N. This makes no-query context meaningful — it returns the memories agents actually use, not random ones.

---

### `passive.py`

**Purpose:** Extracts memories from a raw conversation or session log, without the agent explicitly calling `learn()`.

**Provider detection** (`detect_provider()`):
1. Check `MESH_AI_PROVIDER` env var for explicit override
2. Check for API keys: `ANTHROPIC_API_KEY` → `OPENAI_API_KEY` → `GROQ_API_KEY`
3. Check if Ollama is running on localhost:11434
4. Fall back to local keyword extractor

**AI extraction:** sends the log to an LLM with a prompt that asks it to return a JSON array of `{content, type, tags, private}` objects. The response is parsed and each item is stored via `mesh.learn()`.

**Local extraction** (`_extract_local()`): pure Python, no API. Splits log into sentences, scores each for "signal" using keyword matching (`SIGNAL_KEYWORDS`), filters out filler (`NOISE_PATTERNS`), detects credentials (`_is_credential()`), and returns high-signal sentences. Much less accurate than AI extraction but works with zero dependencies.

**Fallback chain:** if AI extraction throws an exception (bad API key, timeout, etc.), the code falls back to local extraction and reports `provider_used: "local-keyword-extractor (fallback)"`.

---

### `digest.py`

**Purpose:** Summarize recent activity for a time window — new memories, contradictions, stale memories, active agents.

**Reads SQLite directly** (doesn't go through `Mesh` class). This is intentional — digest is a reporting tool, not an agent action. It shouldn't create embeddings or log to audit.

**Time window:** the `since` timestamp is computed from `datetime.now() - timedelta(hours=hours)`. All SQLite queries use `WHERE created_at >= ?` with this ISO timestamp string.

---

### `io.py`

**Purpose:** Export memories to JSON files and import them back.

**Export:** reads all non-private memories from SQLite, optionally fetches their embeddings from ChromaDB, and writes a JSON file with mesh version metadata.

**Import:** for each memory in the file:
- If `embedding` is present in the JSON, upserts it directly into ChromaDB (no re-embedding)
- If `embedding` is absent (file exported with `include_embeddings=False`), calls `mesh.learn()` to re-embed
- Writes metadata to SQLite regardless

**Why include embeddings?** Re-embedding ~500 memories on import takes 30+ seconds. Including embeddings makes import instant. But embeddings are large (~1536 floats × 4 bytes = ~6 KB each), so a 1000-memory export is ~6 MB with embeddings vs ~200 KB without.

---

### `namespaces.py`

**Purpose:** Namespace-level operations — list, delete, rename, stats.

**Discovery:** `list_namespaces()` scans `~/.mesh/` for files matching `mesh_*.db` and infers namespace names from filenames. This means namespaces are discovered from disk, not from a registry. If you manually create a DB file, it will appear.

**Delete:** removes the SQLite file and calls `chromadb.delete_collection()`. The audit DB is not deleted — audit history is preserved even after namespace deletion.

**Rename:** copies the SQLite file with `shutil.copy2`, reads all ChromaDB data with `.get(include=["documents", "embeddings", "metadatas"])`, upserts to a new collection, then deletes the originals. This is the only place we read and re-write all embeddings at once.

---

### `http_server.py`

**Purpose:** FastAPI REST API on port 7701. Any tool that can make HTTP requests can use Mesh.

**CORS:** configured with `allow_origins=["*"]` since this is a localhost-only server. Not a security concern for local use.

**`get_mesh()` helper:** creates a `Mesh` instance from the request body's `namespace` and `agent_id` fields, falling back to environment variables. Each request gets a fresh `Mesh` instance (they're cheap — just SQLite + ChromaDB client connections).

**To add an endpoint:** add a Pydantic model for the request body, add a FastAPI route function, add to the `routes` check in `test_v04.py`.

---

### `mcp_server.py`

**Purpose:** Exposes Mesh as MCP tools for Claude Desktop, Cursor, and any MCP-compatible client.

**Singleton `_mesh`:** unlike the HTTP server (fresh instance per request), the MCP server keeps a global `Mesh` instance. MCP servers are long-running processes — the singleton avoids reconnecting ChromaDB on every tool call.

**Async:** MCP requires `async def` handlers. The `get_mesh()` and all tool handlers use `async` even though `Mesh` itself is synchronous. This is fine — synchronous code runs fine inside async functions as long as it doesn't block the event loop for too long. (For very large namespaces, consider wrapping heavy calls in `asyncio.run_in_executor`.)

**To add a tool:** add to `list_tools()` with a `types.Tool` definition, add a case to `call_tool()`, and add documentation to the tool's `description` field (this is what the agent reads).

---

### `dashboard.py`

**Purpose:** Local web dashboard. FastAPI for the API layer, inline HTML/JS for the UI. Runs on port 7433 by default.

**Architecture:** the entire frontend is a single HTML string (`DASHBOARD_HTML`) embedded in the Python file. This makes the dashboard zero-dependency from a deployment perspective — no npm, no build step, no static file serving config.

**API endpoints:** the dashboard has its own set of `/api/*` endpoints. They're separate from the HTTP REST API (port 7701) — the dashboard has read-write access to everything, while the HTTP API is the integration point for external tools.

**Additional endpoints needed for editing:**
- `POST /api/memories` — create a memory (calls `mesh.learn()`)
- `PUT /api/memories/{id}` — update memory content (needs `forget()` + `learn()` since embeddings can't be patched)

---

### `cli.py`

**Purpose:** Click-based CLI entry points. Each function corresponds to a `mesh-*` command.

**Entry point registration:** in `pyproject.toml` (or `setup.py`), each function is registered as a console script:
```toml
[project.scripts]
mesh-add      = "mesh.cli:add_cmd"
mesh-ask      = "mesh.cli:ask_cmd"
mesh-context  = "mesh.cli:context_cmd"
mesh-why      = "mesh.cli:why_cmd"
mesh-status   = "mesh.cli:status_cmd"
mesh-export   = "mesh.cli:export_cmd"
mesh-import   = "mesh.cli:import_cmd"
mesh-namespaces = "mesh.cli:namespaces_cmd"
mesh-digest   = "mesh.digest:digest_cmd"
mesh-server   = "mesh.mcp_server:run"
mesh-dashboard = "mesh.dashboard:run"
```

**Namespace resolution:** every CLI command resolves namespace in order: `--namespace` flag → `MESH_NAMESPACE` env var → `"shared"`.

---

## Storage Schema

### `mesh_<namespace>.db`

```sql
CREATE TABLE memories (
    id              TEXT PRIMARY KEY,
    namespace       TEXT NOT NULL,
    content         TEXT NOT NULL,
    confidence      REAL NOT NULL DEFAULT 1.0,
    memory_type     TEXT NOT NULL DEFAULT 'fact',
    source_agent    TEXT NOT NULL DEFAULT 'unknown',
    tags            TEXT NOT NULL DEFAULT '[]',   -- JSON array stored as string
    created_at      TEXT NOT NULL,                -- ISO 8601
    accessed_at     TEXT NOT NULL,
    access_count    INTEGER NOT NULL DEFAULT 0,
    ttl_days        REAL,                         -- NULL = no decay
    decay_start_at  TEXT,                         -- ISO 8601 or NULL
    local_only      INTEGER DEFAULT 0             -- 0 = exportable, 1 = private
);

CREATE TABLE queries (
    id              TEXT PRIMARY KEY,
    namespace       TEXT NOT NULL,
    query_text      TEXT NOT NULL,
    agent_id        TEXT NOT NULL DEFAULT 'unknown',
    best_similarity REAL,
    result_count    INTEGER NOT NULL DEFAULT 0,
    timestamp       TEXT NOT NULL
);

CREATE TABLE contradictions (
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
CREATE TABLE audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    action          TEXT NOT NULL,      -- 'recall' or 'learn'
    agent_id        TEXT,
    namespace       TEXT,
    query           TEXT,               -- for recalls: the query string
    content         TEXT,               -- for learns: the content stored
    top_result      TEXT,               -- for recalls: top result content
    top_similarity  REAL,               -- for recalls: top result similarity
    result_count    INTEGER,            -- for recalls: number of results
    memory_id       TEXT,               -- for learns: the stored memory ID
    had_conflict    INTEGER DEFAULT 0   -- for learns: 1 if contradiction detected
);
```

### ChromaDB collection: `mesh_<namespace>`

Each collection stores:
- `ids`: memory UUIDs
- `documents`: memory content strings (used for display)
- `embeddings`: 384-dim float vectors from `all-MiniLM-L6-v2`
- `metadatas`: dict of `str/int/float/bool` fields — subset of SQLite metadata

Note: ChromaDB's metadata cannot store lists or nested objects. Tags are stored in SQLite only. The ChromaDB metadata is used primarily for contradiction detection context (who stored it, when).

---

## How to Add a New Feature

### Step 1: Does it need new storage?

If yes → add columns to `store.py:_init_db()` with an `ALTER TABLE` migration block for existing DBs, and update `store.py:upsert()` and `store.py:list_all()`.

### Step 2: Does it touch the core Mesh class?

If yes → add/modify methods in `memory.py`. The `Mesh` class is the single interface all other modules use — keep it clean and focused.

### Step 3: Does it need a new CLI command?

→ See [How to Add a New CLI Command](#how-to-add-a-new-cli-command).

### Step 4: Does it need a new HTTP endpoint?

→ See [How to Add a New HTTP Endpoint](#how-to-add-a-new-http-endpoint).

### Step 5: Should it be exposed in the public API?

If yes → add the import to `__init__.py` and add the name to `__all__`.

### Step 6: Write tests

Add to `test_v04.py`. Tests should:
- Use a unique namespace like `"test_myfeature_<uuid>"` to avoid conflicts
- Call `delete_namespace("test_myfeature_...")` in cleanup
- Mock API calls with `unittest.mock.patch`

---

## How to Add a New CLI Command

1. Add a function in `cli.py` following the pattern:

```python
def myfeature_cmd():
    """Entry point for: mesh-myfeature"""
    import argparse
    parser = argparse.ArgumentParser(
        description="Short description.",
        epilog="Example: mesh-myfeature --option value"
    )
    parser.add_argument("--namespace", default=None)
    parser.add_argument("--option", default="default")
    args = parser.parse_args()

    from .memory import Mesh   # import inside function to keep startup fast
    import os
    ns = args.namespace or os.environ.get("MESH_NAMESPACE", "shared")
    
    # ... do the work ...
    print("Done.")
```

2. Register it in `pyproject.toml`:

```toml
[project.scripts]
mesh-myfeature = "mesh.cli:myfeature_cmd"
```

3. Re-install: `pip install -e .`

---

## How to Add a New MCP Tool

1. Add a `types.Tool` entry to `list_tools()` in `mcp_server.py`:

```python
types.Tool(
    name="mesh_myfeature",
    description="What this tool does and when to use it.",
    inputSchema={
        "type": "object",
        "properties": {
            "param1": {"type": "string", "description": "..."}
        },
        "required": ["param1"]
    }
)
```

2. Add a case to `call_tool()`:

```python
elif name == "mesh_myfeature":
    result = mesh.myfeature(arguments["param1"])
    return [types.TextContent(
        type="text",
        text=json.dumps(result, indent=2)
    )]
```

3. Restart the MCP server (Cursor/Claude Desktop need a restart to pick up tool changes).

---

## How to Add a New HTTP Endpoint

1. Add a Pydantic model (if the endpoint takes a request body):

```python
class MyFeatureRequest(BaseModel):
    param1: str
    namespace: Optional[str] = None
    agent_id: Optional[str] = None
```

2. Add the route:

```python
@app.post("/myfeature")
async def myfeature(req: MyFeatureRequest):
    try:
        mesh = get_mesh(req.namespace, req.agent_id)
        result = mesh.myfeature(req.param1)
        return {"status": "ok", "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
```

3. Add to the routes check in `test_v04.py:test_http_endpoints_exist()`:

```python
required = [..., "/myfeature"]
```

---

## How to Add a New Memory Type

1. Add the new type to `VALID_MEMORY_TYPES` in `memory.py`:

```python
VALID_MEMORY_TYPES = {"fact", "preference", "context", "result", "instruction", "mytype"}
```

2. Update the MCP tool's `inputSchema` in `mcp_server.py`:

```python
"enum": list(VALID_MEMORY_TYPES),
```

3. Update the CLI help text in `cli.py:add_cmd()`:

```python
choices=["fact", "process", "decision", "error", "general", "mytype"],
```

4. Update the context builder's type display in `context_builder.py:_format()` if you want a specific section header for the new type.

5. Update the README's memory types table.

---

## Testing

Tests live in `test_v04.py` and cover: audit trail, context builder, digest, HTTP API, passive capture.

```bash
# Run all tests
pytest test_v04.py -v

# Run a specific test
pytest test_v04.py::test_http_learn_and_recall -v

# Run with coverage
pytest test_v04.py --cov=mesh --cov-report=term-missing
```

**Test isolation pattern:**

```python
def test_my_feature():
    ns = "test_myfeature_xyz"
    mesh = Mesh(namespace=ns, agent_id="test")
    
    # ... test code ...
    
    delete_namespace(ns)  # always clean up
```

**Mocking external calls:**

```python
from unittest.mock import patch, MagicMock

def test_passive_with_mock():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "content": [{"text": '[{"content": "test fact", "type": "fact", "tags": [], "private": false}]'}]
    }
    with patch("httpx.post", return_value=mock_resp):
        result = extract_memories_from_log(log_text="...", namespace="test_ns")
    assert result["stored_count"] >= 1
    delete_namespace("test_ns")
```

---

## Key Design Decisions

**Why ChromaDB + SQLite instead of just one?**

ChromaDB gives fast approximate nearest-neighbour search over embeddings — this is the core of semantic recall. But ChromaDB's metadata is limited (no lists, no complex types, no SQL aggregation). SQLite gives full SQL power for pattern detection, TTL calculations, access counting, and filtering. They're complementary.

**Why `all-MiniLM-L6-v2`?**

Good balance of quality, size, and speed. 384-dimensional embeddings (vs 1536 for OpenAI's ada-002). Runs locally with no API costs. Fast enough for interactive use (<100ms per embed on CPU for short texts). A bigger model like `all-mpnet-base-v2` would give better quality but is 3× slower.

**Why store memories regardless of contradictions?**

Mesh is an observation layer, not an arbiter. It surfaces conflicts for the agent to resolve. Silently discarding new information based on similarity would cause data loss and confuse agents ("I stored this, why can't I find it?"). The agent knows context Mesh doesn't — a "contradictory" memory might be an intentional update.

**Why audit in a separate database?**

Audit logging is fire-and-forget (`try/except: pass`). If the audit DB gets corrupted, deleted, or locked, it must never affect memory reads or writes. Separating the DBs makes this guarantee simple and clear.

**Why inline HTML in the dashboard?**

Zero build-step deployment. No npm, webpack, vite, or static file config. The dashboard works as a `pip install` with no extra steps. The tradeoff is that the Python file is very long — acceptable for a single-file dashboard.

**Why linear decay?**

Simplicity. A linear function is easy to reason about: "I set ttl_days=30, so after 15 days it's at 50% confidence, after 30 days it's at 0%." Exponential decay would make memories "never fully expire" which is confusing for users. S-curve decay would be more realistic but harder to explain.

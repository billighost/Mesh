# Mesh — Developer Guide

> High-speed shared semantic & hybrid memory for AI agents. One agent learns, every agent recalls.

---

## Table of Contents

1. [What Is Mesh?](#what-is-mesh)
2. [How It Works — The Big Picture](#how-it-works)
3. [Installation](#installation)
4. [Core Concepts](#core-concepts)
   - [Namespaces](#namespaces)
   - [Memory Types](#memory-types)
   - [Confidence & TTL Decay](#confidence--ttl-decay)
   - [Deduplication & Contradiction Detection](#deduplication--contradiction-detection)
   - [Privacy (`local_only`)](#privacy-local_only)
5. [Python API](#python-api)
   - [`learn()`](#learn)
   - [`recall()`](#recall)
   - [`update()`](#update)
   - [`get()`](#get)
   - [`forget()`](#forget)
   - [`inspect()`](#inspect)
   - [`patterns()`](#patterns)
   - [`contradictions()` & `resolve_contradiction()`](#contradictions--resolve_contradiction)
   - [`count()`, `clear()`, & `warmup_model()`](#count-clear--warmup_model)
6. [MCP Server (Claude Desktop, Cursor, Windsurf)](#mcp-server)
7. [HTTP REST API](#http-rest-api)
8. [CLI Tools](#cli-tools)
9. [Context Builder](#context-builder)
10. [Passive Log Capture](#passive-log-capture)
11. [Audit Trail](#audit-trail)
12. [Session Digest](#session-digest)
13. [Export & Import](#export--import)
14. [Namespace Management](#namespace-management)
15. [Web Dashboard](#web-dashboard)
16. [Environment Variables](#environment-variables)
17. [Recipes & Multi-Agent Patterns](#recipes--multi-agent-patterns)

---

## What Is Mesh?

Every AI agent starts each session with amnesia. Claude in your terminal doesn't know what Cursor in your editor just discovered. Your code-review agent can't see what your debugging agent found ten minutes ago. Every tool repeats work that has already been done.

Mesh solves this by giving agents an ultra-fast, persistent, local hybrid memory layer. Agents write memories and recall them by meaning, keywords, or exact identifiers—without ever needing to communicate directly.

```text
Agent A ──learn()──► [Mesh Unified Store] ◄──recall()── Agent B
Agent B ──update()─► [Mesh Unified Store] ◄──context()─ Agent C
```

All storage is **100% local** (`~/.mesh/`). Nothing leaves your machine.

---

## How It Works

Mesh combines three retrieval and storage mechanisms inside a single atomic SQLite WAL database per namespace (`~/.mesh/mesh_<namespace>.db`):

1. **In-Memory SIMD Vector Search (Semantic Meaning):** Every memory is embedded into a normalized 384-dimensional `float32` vector using `all-MiniLM-L6-v2` and cached in an in-memory NumPy matrix. Cosine similarity across thousands of memories runs in `<0.1 ms` via SIMD matrix-vector multiplication, with an LRU embedding cache (`4,096` entries) resolving repeated queries in `<0.01 ms`.
2. **SQLite FTS5 BM25 Search (Keyword Relevance):** Every memory's content, tags, type, and source agent are indexed in an FTS5 virtual table (`memories_fts`) with Porter stemming for fast lexical matching.
3. **Exact Token & Identifier Boost:** Exact matches on technical tokens (ports, error codes like `ERR_CODE_9482_XYZ`, file paths, environment variables) receive a deterministic score boost so exact identifiers always rank at the top.

### What happens on `learn()`
1. The content is normalized and embedded via the local `SentenceTransformer` model (or fetched instantly from the LRU cache).
2. If the namespace has existing memories, Mesh checks the top 3 closest vectors:
   - **Pass 1 (Deduplication):** If an existing memory is identical or near-identical (`similarity >= 0.985`) with the same numbers and negation polarity, Mesh reinforces the existing memory in-place (merging tags and taking the higher confidence) instead of creating a duplicate.
   - **Pass 2 (Contradiction Check):** If an existing memory is highly similar (`similarity >= 0.82`) but contains conflicting numbers or negation polarity (e.g., `"rate limit is 100"` vs. `"rate limit is 200"`), Mesh logs a contradiction record and returns a conflict warning while still storing the new memory.
3. The memory, metadata, FTS5 row, and normalized `float32` vector BLOB are committed in a single SQLite WAL transaction, and the in-memory vector matrix is updated in-place in $O(1)$ time.

### What happens on `recall()`
1. The query is embedded (or resolved from the LRU cache).
2. `MeshStore.hybrid_query()` fuses Dense SIMD Cosine similarity + FTS5 BM25 lexical scores + Exact Token Boost in a single database pass, pre-filtering by `memory_type`, `source_agent`, and `tags` if requested.
3. Each candidate's `effective_confidence` is calculated in a single UTC timestamp pass if `ttl_days` is set.
4. Results are filtered by `min_similarity`, `min_confidence`, and `include_stale`, then sorted by similarity descending.
5. `access_count` increments and query pattern logging are committed in a single atomic transaction without invalidating the in-memory vector cache.

---

## Installation

```bash
pip install mesh-context-layer
```

For development and running the test suite:

```bash
pip install -e ".[test]"
```

On first use, Mesh downloads `all-MiniLM-L6-v2` (~90 MB) once to your local Hugging Face cache. After that, everything runs completely offline.

---

## Core Concepts

### Namespaces

A namespace is an isolated memory partition stored in `~/.mesh/mesh_<namespace>.db`. Agents sharing a namespace share memory; agents in different namespaces cannot see each other's data.

```python
from mesh import Mesh

# Two agents working on the same project share memory
coder = Mesh(namespace="payments-service", agent_id="cursor-coder")
reviewer = Mesh(namespace="payments-service", agent_id="claude-reviewer")

# Completely isolated project namespace
infra = Mesh(namespace="k8s-infra", agent_id="terraform-agent")
```

You can set the default namespace globally via environment variable:

```bash
export MESH_NAMESPACE=payments-service
```

---

### Memory Types

Mesh validates 9 canonical memory types across the Python API, CLI, MCP Server, HTTP API, Passive Capture, and Dashboard:

| Type | Use it for | Example |
| :--- | :--- | :--- |
| `fact` | Objective, verifiable project facts | `"Production PostgreSQL runs on port 5432"` |
| `decision` | Architectural or product choices | `"We decided to use Redis Streams instead of Kafka"` |
| `process` | Workflows, build steps, deploy commands | `"Deploy to staging by running ./scripts/deploy.sh"` |
| `instruction` | Rules and constraints agents must follow | `"Always run pytest before opening a pull request"` |
| `preference` | User or codebase style preferences | `"User prefers TypeScript strict mode and functional components"` |
| `context` | Project background and current sprint state | `"Currently migrating auth service from v1 to v2"` |
| `result` | Outcomes of completed tasks or benchmarks | `"Auth migration finished; p99 latency dropped to 4ms"` |
| `error` | Bug root causes, incident notes, and fixes | `"ERR_CODE_9482_XYZ happens when Redis loses quorum"` |
| `general` | General notes that don't fit another category | `"Team sync happens every Tuesday at 10am UTC"` |

---

### Confidence & TTL Decay

Every memory has a **stored confidence** (`0.0` to `1.0`) and an **effective confidence** (`confidence * decay_factor`).

- **Permanent Memories (`ttl_days=None`):** Never decay (`decay_factor = 1.0`, `is_stale = False`).
- **Time-Sensitive Memories (`ttl_days=N`):** Confidence decays linearly from `1.0` to `0.0` over `N` days starting from `decay_start_at`:
  $$\text{decay\_factor} = \max\left(0.0, \min\left(1.0, 1.0 - \frac{\text{days\_elapsed}}{\text{ttl\_days}}\right)\right)$$
- **Staleness (`STALE_THRESHOLD = 0.3`):** When `effective_confidence < 0.3`, `is_stale` becomes `True`.

```python
# Temporary staging IP — decays over 14 days
mesh.learn("Staging server IP is 10.24.1.50", memory_type="fact", ttl_days=14)

# Exclude stale memories during recall
fresh_results = mesh.recall("staging server IP", include_stale=False)
```

---

### Deduplication & Contradiction Detection

When `mesh.learn()` is called with `deduplicate=True` (the default):

1. **Automatic In-Place Deduplication:** If an agent stores `"Production Redis runs on port 6379"` with `tags=["redis"]` and another agent later stores `"Production Redis runs on port 6379"` with `tags=["cache"]`, Mesh merges the tags (`["redis", "cache"]`), keeps the original UUID and `access_count`, and returns `{"deduplicated": True, "conflict": None}`.
2. **Conflict / Contradiction Detection:** If an agent stores `"The API rate limit is 100 requests per minute"` and another stores `"The API rate limit is 200 requests per minute"`, Mesh detects that the numbers differ despite high semantic similarity (`>= 0.82`). It stores the new memory, logs a contradiction record, and returns:

```python
{
    "memory_id": "new-uuid",
    "status": "stored_with_conflict",
    "deduplicated": False,
    "conflict": {
        "existing_memory_id": "old-uuid",
        "existing_content": "The API rate limit is 100 requests per minute",
        "existing_memory": "The API rate limit is 100 requests per minute",
        "similarity": 0.9612,
        "stored_by": "coding-agent",
        "stored_at": "2026-10-15T12:00:00+00:00",
        "contradiction_id": "conflict-uuid"
    }
}
```

---

### Privacy (`local_only`)

Set `local_only=True` (or `--private` in the CLI) for credentials, local paths, or sensitive tokens. Private memories can be recalled locally by your agents, but are **never included in `export_namespace()`** and are badged as `PRIVATE` in the CLI and Dashboard.

```python
mesh.learn(
    "Local dev Postgres password is dev_secret_99",
    memory_type="fact",
    local_only=True,
    tags=["credentials", "local"]
)
```

---

## Python API

### `learn()`

```python
result = mesh.learn(
    content: str,
    memory_type: str = "fact",
    confidence: float = 1.0,
    tags: Optional[list[str]] = None,
    ttl_days: Optional[float] = None,
    local_only: bool = False,
    deduplicate: bool = True,
) -> dict[str, Any]
```

Returns a dictionary with `"memory_id"`, `"status"` (`"stored"` or `"stored_with_conflict"`), `"deduplicated"` (`bool`), and `"conflict"` (`None` or conflict metadata dict).

---

### `recall()`

```python
results = mesh.recall(
    query: str,
    n: int = 5,
    min_similarity: float = 0.0,
    min_confidence: float = 0.0,
    include_stale: bool = True,
    memory_type: Optional[str] = None,
    tags: Optional[list[str]] = None,
    source_agent: Optional[str] = None,
) -> list[dict[str, Any]]
```

Each returned dictionary contains:

```python
{
    "id": "uuid",
    "content": "Production PostgreSQL runs on port 5432",
    "similarity": 0.9142,            # Fused hybrid similarity score
    "semantic_similarity": 0.8810,   # Raw dense cosine similarity
    "lexical_score": 0.7500,         # BM25 / exact token overlap score
    "composite_score": 0.8421,       # Weighted similarity + confidence + usage boost
    "source_agent": "coding-agent",
    "memory_type": "fact",
    "tags": ["db", "postgres"],
    "confidence": 1.0,
    "effective_confidence": 1.0,
    "decay_factor": 1.0,
    "is_stale": False,
    "days_until_stale": None,
    "ttl_days": None,
    "access_count": 3,
    "local_only": False,
    "created_at": "2026-10-15T09:30:00+00:00",
    "accessed_at": "2026-10-15T14:10:00+00:00",
}
```

---

### `update()`

Update an existing memory in-place by its UUID without losing its `id`, `created_at` timestamp, or `access_count`. The vector embedding is only recomputed if `content` changes.

```python
updated = mesh.update(
    memory_id="uuid-here",
    content="Production PostgreSQL runs on port 5433",
    memory_type="fact",
    confidence=0.95,
    tags=["db", "postgres", "updated"],
    ttl_days=30,
    reset_ttl=True,
    local_only=False,
)
# updated["status"] == "updated"
# updated["memory"] -> full enriched memory dict
```

---

### `get()`

Fetch a single memory by ID with all decay and staleness fields calculated:

```python
mem = mesh.get("uuid-here")
```

---

### `forget()`

Permanently delete a memory by ID from SQLite, FTS5, and the in-memory vector cache:

```python
deleted: bool = mesh.forget("uuid-here")
```

---

### `inspect()`

List memories in the namespace sorted by most recently accessed (`accessed_at DESC`), with optional `memory_type` and `tag` filters:

```python
memories = mesh.inspect(limit=20, memory_type="decision", tag="arch")
```

---

### `patterns()`

Analyze memory usage and query logs to identify top topics, knowledge gaps, unused memories, and stale memories:

```python
p = mesh.patterns()
# {
#     "top_topics": [{"query_text": "deploy", "query_count": 12, "avg_similarity": 0.84}],
#     "memory_gaps": [{"query_text": "kafka certs", "query_count": 3, "avg_similarity": 0.21}],
#     "unused_memories": [...],  # access_count == 0 and created > 7 days ago
#     "stale_memories": [...],   # effective_confidence < 0.3
# }
```

---

### `contradictions()` & `resolve_contradiction()`

```python
# List unresolved conflicts
open_conflicts = mesh.contradictions(resolved=False)

# Mark a conflict as resolved
mesh.resolve_contradiction(contradiction_id="conflict-uuid")
```

---

### `count()`, `clear()`, & `warmup_model()`

```python
from mesh import Mesh, warmup_model

# Pre-load SentenceTransformer and warm the namespace vector cache in the background
warmup_model("my-project")

mesh = Mesh(namespace="my-project")
total: int = mesh.count()   # O(1) when vector cache is hot
mesh.clear()                # Wipes memories, queries, FTS, and contradictions in this namespace
```

---

## MCP Server

The MCP server (`mesh-server`) exposes 10 tools over `stdio` for Claude Desktop, Cursor, Windsurf, and any MCP-compatible client. It also runs a background worker that warms up the embedding model on startup and incrementally captures concrete facts from agent logs.

### Configuration

**Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS or `%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "mesh": {
      "command": "mesh-server",
      "env": {
        "MESH_NAMESPACE": "my-project",
        "MESH_AGENT_ID": "claude-desktop"
      }
    }
  }
}
```

**Cursor** (`.cursor/mcp.json` in your repository root):

```json
{
  "mcpServers": {
    "mesh": {
      "command": "mesh-server",
      "env": {
        "MESH_NAMESPACE": "my-project",
        "MESH_AGENT_ID": "cursor"
      }
    }
  }
}
```

### Exposed MCP Tools (10 Tools)

| Tool Name | Description |
| :--- | :--- |
| `mesh_learn` | Store a memory with automatic deduplication, conflict detection, tags, TTL, and `local_only` privacy |
| `mesh_recall` | 3-stage hybrid search (semantic + BM25 + exact token match) with type/tag/agent filters |
| `mesh_update` | Update an existing memory in-place by `memory_id` while preserving its UUID and access stats |
| `mesh_forget` | Delete a specific memory permanently by `memory_id` |
| `mesh_inspect` | List memories in the namespace sorted by recent access, with optional `memory_type` and `tag` filters |
| `mesh_context` | Generate a formatted system prompt context block (`markdown`, `plain`, `xml`, `json`) |
| `mesh_patterns` | Surface `top_topics`, `memory_gaps`, `unused_memories`, and `stale_memories` |
| `mesh_contradictions` | List unresolved (or resolved) contradictions between stored memories |
| `mesh_resolve_contradiction` | Mark a specific `contradiction_id` as resolved |
| `mesh_sync` | Store a session turn summary and incrementally extract facts from recent agent logs |

---

## HTTP REST API

Start the standalone HTTP API server on `http://127.0.0.1:7701`:

```bash
mesh-http
# or via unified CLI:
mesh http
```

### Endpoints

| Method | Path | Description |
| :--- | :--- | :--- |
| `GET` | `/health` | Health check (`{"status": "ok", "version": "0.4", "timestamp": "..."}`) |
| `POST` | `/learn` | Store a memory (`content`, `memory_type`, `confidence`, `tags`, `local_only`, `ttl_days`, `namespace`, `agent_id`) |
| `POST` | `/recall` | Hybrid search (`query`, `count`, `memory_type`, `tags`, `source_agent`, `min_similarity`, `min_confidence`, `include_stale`, `namespace`, `agent_id`) |
| `POST` | `/update` | Update a memory in-place (`memory_id`, `content`, `memory_type`, `confidence`, `tags`, `local_only`, `ttl_days`, `namespace`, `agent_id`) |
| `POST` | `/forget` | Delete a memory by ID (`memory_id`, `namespace`, `agent_id`) |
| `GET` | `/inspect` | List memories (`?namespace=...&limit=50&memory_type=...&tag=...`) |
| `GET` | `/patterns` | Pattern & memory gap analysis (`?namespace=...`) |
| `GET` | `/contradictions` | List contradictions (`?namespace=...&resolved=false`) |
| `POST` | `/contradictions/resolve` | Resolve a contradiction (`contradiction_id`, `namespace`) |
| `POST` | `/context` | Build a formatted system prompt block (`query`, `count`, `namespace`, `format`, `min_confidence`, `memory_type`, `tags`) |
| `GET` | `/audit` | Query the audit trail (`?namespace=...&limit=50&action=all`) |

**Example (`curl`):**

```bash
# Store a memory
curl -s -X POST http://localhost:7701/learn \
  -H "Content-Type: application/json" \
  -d '{"content": "Staging Redis runs on port 6380", "memory_type": "fact", "tags": ["redis", "staging"]}'

# Update in-place
curl -s -X POST http://localhost:7701/update \
  -H "Content-Type: application/json" \
  -d '{"memory_id": "<UUID>", "content": "Staging Redis runs on port 6381"}'

# Recall with hybrid search
curl -s -X POST http://localhost:7701/recall \
  -H "Content-Type: application/json" \
  -d '{"query": "port 6381", "count": 3}'
```

---

## CLI Tools

You can invoke commands either via the unified `mesh <command>` entrypoint or the standalone `mesh-<command>` binaries:

```bash
# Store a memory
mesh-add "Production DB is PostgreSQL 16 on port 5432" --type fact --tags db,prod
mesh-add "Staging API token: sk-stage-123" --type fact --private --ttl 30

# Hybrid search
mesh-ask "what port is Postgres on?" --count 5 --type fact --tags db --no-stale

# Update an existing memory in-place
mesh-update <memory_id> --content "Production DB is PostgreSQL 16 on port 5433" --confidence 0.95

# Delete a memory by ID
mesh-forget <memory_id> --namespace shared

# Generate a system prompt block
mesh-context --query "database architecture" --format markdown --count 10

# Inspect the audit trail (who read/wrote/updated/deleted what)
mesh-why --limit 25 --action all

# Namespace status overview
mesh-status --namespace shared

# Manage namespaces
mesh-namespaces list
mesh-namespaces stats shared
mesh-namespaces rename old-ns new-ns
mesh-namespaces delete temp-ns

# Export and import
mesh-export backup.json --namespace shared
mesh-export readable.json --namespace shared --no-embeddings
mesh-import backup.json --namespace restored --overwrite
mesh-import backup.json --dry-run

# Passive log capture
mesh-capture --file session.log --namespace shared --verbose
cat agent_output.txt | mesh-capture --stdin --local-only
mesh-capture --show-provider

# Activity digest
mesh-digest --hours 24 --namespace shared
mesh-digest --hours 8 --json

# Start servers & UI
mesh-server                     # MCP stdio server
mesh-http                       # REST API on localhost:7701
mesh-dashboard --port 7433      # Web UI on localhost:7433
```

---

## Context Builder

`build_context()` formats memories into a system prompt block (`markdown`, `plain`, `xml`, or `json`) ready to prime any agent session.

```python
from mesh import build_context

# Topic-focused context (uses 3-stage hybrid recall)
focused_block = build_context(
    query="authentication and JWT cookies",
    namespace="my-project",
    count=5,
    format="markdown",
    min_confidence=0.3,
    memory_type="instruction",
    tags=["auth"],
)

# General project context (pulls most-accessed high-confidence memories)
general_block = build_context(namespace="my-project", count=10, format="markdown")
```

---

## Passive Log Capture

`extract_memories_from_log()` ingests raw conversation text, a `.log`/`.txt`/`.md`/`.jsonl` file path, or a directory of log files, extracts reusable facts, and stores them in Mesh.

```python
from mesh import extract_memories_from_log

result = extract_memories_from_log(
    log_text="./logs/claude_session.jsonl",  # Accepts raw text, file path, or directory path
    namespace="my-project",
    agent_id="passive-capture",
    dry_run=False,
    verbose=True,
    force_local=False,                       # Set True to force local regex extraction
)
# Returns: {"stored_count": ..., "skipped_count": ..., "provider_used": ..., "memories": [...]}
```

**Provider Detection Priority:**
1. `MESH_AI_PROVIDER` explicit override (`anthropic`, `openai`, `groq`, `ollama`, `openai-compatible`, `local`)
2. `ANTHROPIC_API_KEY` (`claude-haiku-4-5-20251001`)
3. `OPENAI_API_KEY` (`gpt-4o-mini`)
4. `GROQ_API_KEY` (`llama3-8b-8192`)
5. Local Ollama server (`http://localhost:11434/v1`)
6. Built-in local regex/heuristic extractor (`local-keyword-extractor` — free, offline, automatically marks credentials as `local_only=True`)

---

## Audit Trail

Every `recall`, `learn`, `update`, and `forget` event is logged to `~/.mesh/audit_<namespace>.db`.

```python
from mesh import (
    get_audit_log,
    get_audit_stats,
    log_forget,
    log_learn,
    log_recall,
    log_update,
)

# Query recent audit events ("all", "read", "write", "recall", "learn", "update", "forget")
entries = get_audit_log(namespace="my-project", limit=50, action_filter="all")

# Aggregate statistics
stats = get_audit_stats(namespace="my-project")
# {
#     "total_recalls": 42,
#     "total_learns": 15,
#     "agents": ["cursor", "claude-desktop", "human-cli"],
#     "conflicts_detected": 1,
#     "last_activity": "2026-10-15T14:20:00+00:00"
# }
```

---

## Session Digest

Generate a timezone-safe UTC summary of recent activity over the last `N` hours:

```python
from mesh import generate_digest

digest = generate_digest(namespace="my-project", hours=24)
# {
#     "namespace": "my-project",
#     "hours": 24,
#     "since": "2026-10-14T14:20:00+00:00",
#     "new_memories": [...],
#     "new_count": 6,
#     "contradictions": [...],
#     "contradiction_count": 1,
#     "stale_memories": [...],
#     "stale_count": 0,
#     "total_recalls": 19,
#     "agents_active": ["cursor", "claude-desktop"]
# }
```

---

## Export & Import

Export public memories (`local_only=False`) to JSON and batch-import them into any namespace:

```python
from mesh import export_namespace, import_namespace

# Export with 384-dim embeddings included for instant re-import
export_data = export_namespace(
    namespace="my-project",
    output_path="backup.json",
    include_embeddings=True,
)

# Import into another namespace ("skip_existing" or "overwrite")
stats = import_namespace(
    input_path="backup.json",
    target_namespace="my-project-staging",
    merge_strategy="skip_existing",
    dry_run=False,
)
# stats == {"imported": 10, "skipped": 0, "errors": 0, "namespace": "my-project-staging"}
```

When `include_embeddings=False`, `import_namespace()` automatically batch-embeds all new texts in a single forward pass via `_embed_batch()` and commits all rows in one SQLite WAL transaction.

---

## Namespace Management

```python
from mesh import delete_namespace, list_namespaces, namespace_stats, rename_namespace

# List all namespaces on disk
all_ns = list_namespaces()

# Detailed breakdown (counts, types, avg confidence, recalls, stale count)
stats = namespace_stats("my-project")

# Rename a namespace atomically
moved_count = rename_namespace("my-project", "my-project-v2")

# Delete a namespace and its SQLite files
deleted_count = delete_namespace("my-project-v2")
```

---

## Web Dashboard

Launch the local web visualizer on `http://localhost:7433`:

```bash
mesh-dashboard --namespace my-project --port 7433
```

Keyboard shortcuts inside the dashboard:
- `/` — Focus the hybrid search bar
- `n` — Open the **New Memory** modal
- `Esc` — Close the active modal

---

## Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `MESH_NAMESPACE` | `shared` | Default namespace across CLI, MCP, HTTP, and Dashboard |
| `MESH_AGENT_ID` | `mcp-agent` / `http-client` | Default agent identifier recorded on memories and audit logs |
| `MESH_HTTP_PORT` | `7701` | Port for the `mesh-http` REST API server |
| `MESH_AI_PROVIDER` | *auto-detected* | Explicit passive capture provider (`anthropic`, `openai`, `groq`, `ollama`, `openai-compatible`, `local`) |
| `MESH_AI_API_KEY` | — | Explicit API key override for passive capture |
| `MESH_AI_MODEL` | *provider default* | Explicit LLM model override for passive capture |
| `MESH_AI_BASE_URL` | *provider default* | Custom base URL for OpenAI-compatible or Ollama endpoints |
| `ANTHROPIC_API_KEY` | — | Anthropic API key for passive log extraction |
| `OPENAI_API_KEY` | — | OpenAI API key for passive log extraction |
| `GROQ_API_KEY` | — | Groq API key for passive log extraction |
| `MESH_NO_UPDATE_CHECK` | `0` | Set to `1` or `true` to disable the background PyPI version check |

---

## Recipes & Multi-Agent Patterns

### 1. Multi-Agent Handoff (Zero Direct Coordination)

```python
from mesh import Mesh

# Agent 1: Backend agent updates an API contract
backend = Mesh(namespace="checkout-v2", agent_id="backend-agent")
res = backend.learn(
    "POST /v2/checkout requires an Idempotency-Key UUID header",
    memory_type="instruction",
    tags=["api", "checkout"],
)
mem_id = res["memory_id"]

# Agent 2: Frontend agent recalls the contract before writing client code
frontend = Mesh(namespace="checkout-v2", agent_id="frontend-agent")
rules = frontend.recall("checkout endpoint headers", memory_type="instruction")
for r in rules:
    print(f"[{r['similarity']:.2f}] {r['content']} (from {r['source_agent']})")
```

### 2. Updating Evolving Facts Without Losing History

```python
# When a port or config changes, update the existing memory in-place by ID
# so its UUID, creation timestamp, and access_count are preserved:
backend.update(
    memory_id=mem_id,
    content="POST /v2/checkout requires both Idempotency-Key and X-Tenant-ID headers",
    tags=["api", "checkout", "multi-tenant"],
)
```

### 3. Priming Any CLI Agent Session

```bash
# Inject top project memories directly into Aider, Claude Code, or custom scripts:
aider --system-prompt "$(mesh-context --namespace checkout-v2 --count 15)"
```
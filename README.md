# Mesh

**High-speed shared semantic & hybrid memory layer for AI agents.**

When one agent learns something, every other agent can recall it — by meaning, keyword, or exact identifier.

[![PyPI version](https://img.shields.io/pypi/v/mesh-context-layer.svg)](https://pypi.org/project/mesh-context-layer/)
[![Python versions](https://img.shields.io/pypi/pyversions/mesh-context-layer.svg)](https://pypi.org/project/mesh-context-layer/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)

---

## The Problem

Every AI agent starts each session with amnesia. Claude in your terminal doesn't know what Claude in your editor discovered. Your coding agent cannot access what your review agent found. Every agent repeats work already done.

## The Solution

Mesh provides persistent, shared agent memory with:
- **3-Stage Hybrid Search**: Dense Vector SIMD Cosine (`cache.matrix @ q_vec` in `<0.1 ms`) + SQLite FTS5 BM25 + Exact Token Overlap.
- **Unified Local Storage**: Pure SQLite WAL per namespace — zero external database daemons, zero multi-process synchronization issues.
- **Smart Deduplication & Contradiction Tracking**: Identical facts are reinforced in-place; contradictory facts are flagged for review without silent data loss.
- **Time-to-Live (TTL) Confidence Decay**: Ephemeral facts gradually decay towards staleness.
- **Multi-Interface**: MCP Server (10 tools for Claude Desktop, Cursor, Windsurf), Web Dashboard, HTTP REST API, and CLI.

---

## Quickstart

### 1. Installation & Instant Setup

```bash
pip install mesh-context-layer

# Optional: Instant setup (spawns background daemon and warms ONNX model in <150 ms)
mesh setup
```

### Zero-Friction Auto-Daemon & Non-Blocking Background Model Downloader

Mesh is zero-configuration:
- **Zero-Friction Auto-Daemon**: On first use, Mesh automatically spawns a persistent local background daemon (`http://127.0.0.1:7701`) with zero cold start.
- **Non-Blocking Background Model Downloader**: Downloads `all-MiniLM-L6-v2` ONNX weights into `~/.mesh/models/` in a background daemon thread prioritizing fast Google Cloud Storage and CDN mirrors—**no Hugging Face token required and zero network rate limits**.
- While the background download completes, Mesh serves requests instantly in `<0.05 ms` using a concept-aware 384-dimensional semantic feature-hash engine (`onnx-warming`) and automatically backfills neural embeddings once the ONNX model lands on disk.

### 2. Python SDK

```python
from mesh import Mesh

# Agent 1: Learns an architecture constraint
agent_a = Mesh(namespace="checkout-service", agent_id="backend-agent")
agent_a.learn(
    "Production PostgreSQL 16 database runs on port 5432 with pgBouncer connection pooling",
    memory_type="fact",
    tags=["db", "postgres", "prod"]
)

# Agent 2: Recalls it without direct communication
agent_b = Mesh(namespace="checkout-service", agent_id="frontend-agent")
results = agent_b.recall("what port does production postgres use?", n=3)

for r in results:
    print(f"[{r['similarity']:.2f}] {r['content']} (from {r['source_agent']})")
```

---

## MCP Server (Claude Desktop, Cursor & Windsurf)

Mesh provides **10 MCP tools** directly to your AI agents.

### Claude Desktop Configuration
Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (Mac) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "mesh": {
      "command": "mesh-server"
    }
  }
}
```

### Cursor Configuration
Create `.cursor/mcp.json` in your project root:

```json
{
  "mcpServers": {
    "mesh": {
      "command": "mesh-server"
    }
  }
}
```

### Available MCP Tools

| MCP Tool | Description |
| :--- | :--- |
| `mesh_learn` | Store a fact, preference, instruction, or decision with deduplication and contradiction checking. |
| `mesh_recall` | 3-stage hybrid search across shared memory with confidence filtering and score breakdown. |
| `mesh_update` | Update existing memory content and tags in-place preserving UUID and history. |
| `mesh_forget` | Remove a specific memory by its ID. |
| `mesh_inspect` | Browse recent memories in a namespace with pagination and type filtering. |
| `mesh_context` | Generate formatted context blocks (Markdown, XML, JSON, or Plain) for system prompts. |
| `mesh_patterns` | Discover frequently accessed topics, memory gaps, and stale memories. |
| `mesh_contradictions` | Inspect conflicting memories identified during learning. |
| `mesh_resolve_contradiction`| Mark a contradiction as resolved after reviewing the conflict. |
| `mesh_sync` | Save a turn summary and automatically capture facts from session transcripts. |

---

## Web Dashboard

Launch the zero-build local visualizer:

```bash
mesh-dashboard --namespace checkout-service --port 7433
```

Open `http://localhost:7433` in your browser. Features:
- **Real-Time Hybrid Search** with instant score breakdowns.
- **Contradiction Inspector**: Review and resolve flagged memory conflicts.
- **Memory Gaps & Patterns**: See queries agents asked that had low recall similarity.
- **Audit Stream**: Real-time log of agent reads and writes.
- **Keyboard Shortcuts**: `/` to focus search, `n` to create a memory, `Esc` to close modals.

---

## Memory Types

| Type | Intended Purpose |
| :--- | :--- |
| `fact` | Objective project realities (e.g., ports, hostnames, library versions). |
| `preference` | User preferences, code style requirements, framework conventions. |
| `instruction` | Strict rules and guidelines agents must obey. |
| `decision` | Architectural and technical decisions with rationale. |
| `process` | Multi-step workflows and deployment sequences. |
| `context` | Broad project background or domain context. |
| `result` | Verification outcomes, benchmark results, test findings. |
| `error` | Discovered bugs, root causes, and workarounds. |
| `general` | General unstructured observations. |

---

## HTTP REST API

Start the local background server:

```bash
mesh-http --port 7701
```

From any terminal, script, or language:

```bash
# Store a memory
curl -s -X POST http://localhost:7701/learn \
  -H "Content-Type: application/json" \
  -d '{"content": "Auth service requires JWT RS256 signatures", "memory_type": "instruction"}'

# Recall memories
curl -s -X POST http://localhost:7701/recall \
  -H "Content-Type: application/json" \
  -d '{"query": "auth JWT algorithm", "n": 3}'

# Format context directly into agent system prompts
curl -s -X POST http://localhost:7701/context \
  -H "Content-Type: application/json" \
  -d '{"count": 10, "format": "markdown"}'
```

---

## CLI Commands

Mesh provides both unified `mesh <subcommand>` routing and dedicated script entry points:

```bash
# Setup & daemon management
mesh setup                        # Instant setup & model caching (<150 ms)
mesh-setup                        # Direct alias for mesh setup
mesh daemon start                 # Start background daemon (port 7701)
mesh daemon stop                  # Stop background daemon
mesh daemon status                # Inspect daemon health & model RAM residency
mesh daemon warmup                # Cache ONNX model & warm local daemon
mesh daemon unload                # Immediately release model weights from RAM
mesh-daemon status                # Direct script entry point for daemon control

# Memory operations (automatically proxy through warm local daemon)
mesh add "PostgreSQL 16 runs on port 5432" --tags db,prod --type fact
mesh ask "what port does postgres use?" --n 3
mesh update <id> --content "Updated content"
mesh forget <id>
mesh context --query "database config" --format markdown

# Inspection, tools & utilities
mesh namespaces                   # List and inspect namespaces
mesh export backup.json           # Export memories to JSON
mesh import backup.json           # High-speed batch import
mesh dashboard --port 7433        # Launch visualizer
mesh server                       # Start MCP stdio server
mesh capture --file session.log   # Extract memories from logs
mesh digest --hours 24            # UTC session summary
```

---

## Portability & Privacy

### Privacy Flags
Mark sensitive memories as `local_only=True` to guarantee they are never exported or synced:
```python
mesh.learn("Staging DB password: secret-pass", local_only=True, tags=["credentials"])
```

### Backup & Restore
```bash
# Export all public memories to JSON
mesh-export backup.json --namespace checkout-service

# High-speed batch import (with automatic deduplication)
mesh-import backup.json --namespace checkout-staging --merge-strategy skip_existing
```

---

## Detailed Changelog

### v0.6.0 (Latest)
- **Direct Zero-Network ONNX Runtime Engine (`_DirectOnnxEmbedding`)**: Replaced blocking Hugging Face Hub calls with direct `onnxruntime.InferenceSession` + `tokenizers.Tokenizer` execution (`~80 ms` cold start, `~1.5–3 ms` CPU inference, zero PyTorch overhead). Routed `_encode_texts()` directly through the 4,096-entry `_EMBED_CACHE` for `<0.01 ms` LRU cache hits.
- **Non-Blocking Multi-Mirror Background ONNX Auto-Downloader**: Automatically downloads `model.onnx` and `tokenizer.json` into `~/.mesh/models/all-MiniLM-L6-v2-onnx/` in a background daemon thread prioritizing Google Cloud Storage (`storage.googleapis.com`, immune to Hugging Face rate limits) and CDN mirrors (`hf-mirror.com` and `huggingface.co` with `HF_TOKEN` support).
- **Concept-Aware Fallback & Automatic Backfill**: Serves requests in `<0.05 ms` via a concept-aware 384-dim semantic feature-hash engine (`onnx-warming`) while background download runs, then automatically backfills neural embeddings (`_backfill_pending_embeddings`) once the ONNX model lands on disk.
- **Instant Non-Blocking `mesh setup` / `mesh-setup` CLI**: Added `mesh-setup` (`mesh setup`) and `mesh-daemon warmup`, registering OS login auto-start and launching the detached background daemon worker (`wait=False`) in `<150 ms`.
- **In-Memory Metadata Cache & Capacity-Doubling Vector Buffer (`mesh/store.py`)**: Cached full row metadata and parsed `tags_list` in RAM (`_VectorCacheEntry.rows_by_id`) alongside a capacity-doubling `float32` NumPy matrix buffer, making `get()`, `count()`, `list_all()` (`inspect`), and `query()` 100% in-memory (`<0.03 ms`, zero SQLite reads) and eliminating `np.vstack()` reallocations on `learn()`.
- **Persistent SQLite WAL Connection Pooling (`mesh/store.py` & `mesh/audit.py`)**: Replaced per-call connection closing with persistent pooled SQLite WAL connections (`_PooledConnection` and `_ManagedAuditConnection`) synchronized via composite `(.db, .db-wal)` file stamps and `PRAGMA data_version`.
- **Zero-Cold-Start Daemon Proxy Across CLI & MCP (`mesh/cli.py` & `mesh/mcp_server.py`)**: `mesh-add`, `mesh-ask`, `mesh-update`, `mesh-forget`, and `mesh-context` automatically proxy through the warm local daemon (`127.0.0.1:7701`), auto-spawning the daemon in the background (`ensure_daemon_running(wait=False)`) if stopped. Upgraded `mcp_server.py` daemon proxy to use persistent HTTP `keep-alive` connections.
- **Precision Contradiction Detection & Compatibility Fixes**: Added `_is_true_contradiction()` in `mesh/memory.py` to prevent false conflict alerts when elaborating on compatible facts while catching numeric and negation polarity conflicts. Added `max_memories` and `n` parameter aliases to `build_context()`, `mesh_context`, `/context`, and `mesh-context`. Fixed `mesh-namespaces` script entry point.

### v0.5.3
- **Instant Background Daemon Activation (`mesh daemon install-startup`)**: `install-startup` automatically triggers `start_daemon()` immediately upon registration without requiring an extra manual start command.
- **New Daemon Management & Residency Endpoints**: Added `GET /engine` and `POST /unload` REST endpoints to inspect model status and immediately release model weights from memory. Added `mesh-daemon` CLI commands (`start`, `stop`, `restart`, `status`, `unload`, `install-startup`, `remove-startup`).
- **3-Tier Lightweight ONNX / FastEmbed Engine**: Integrated `fastembed` as the primary ONNX runtime embedding engine, dropping cold startup down to ~120 ms and idle RAM to ~20 MB while preserving exact 384-dimensional `all-MiniLM-L6-v2` vector compatibility.
- **Automatic Idle Model Unloading**: Background inactivity watchdog (`MESH_IDLE_UNLOAD_SECONDS=180`) automatically unloads model weights from RAM after 3 minutes while keeping the 4,096-entry LRU embedding cache hot for `<0.01 ms` queries.
- **Cross-Process SQLite WAL Cache Sync**: Composite `(db_mtime_ns, db_size, wal_mtime_ns, wal_size)` stamp ensures external writes from other processes are detected instantly in $O(1)$ time.
- **Dashboard UI Fixes**: Escaped raw string formatting fixing JavaScript regex errors, restored action icons (`↻`, `✎`, `✕`), added `general` type chip and `Deletes` audit filter.
- **Audit Logging Precision**: Guarded `log_forget()` and `POST /forget` against false deletion events on missing IDs.

### v0.5.2
- **Windowless Background Process Spawning**: Implemented silent background daemon spawning on Windows via `pythonw.exe` / `CREATE_NO_WINDOW`.

### v0.5.1
- **High-Speed Batch Import/Export**: `import_namespace` now commits all memories in a single atomic SQLite WAL transaction via `upsert_batch()` and utilizes O(1) in-memory vector cache ID lookups, removing N+1 database queries. Missing embeddings are computed in a single forward pass.
- **MCP Stdio Redirection Safety**: Added thread-safe stdout redirection to ensure print statements never corrupt stdio JSON-RPC payloads in Claude Desktop or Cursor.
- **Optimized Context Generation**: Streamlined single-pass context assembly for `/context` HTTP endpoint and `mesh_context` MCP tool with tag and memory type filtering.
- **Fast-Path Registry**: Added lockless fast-path lookups to the HTTP server's Mesh instance registry.
- **Showcase Suite**: Added `examples/full_showcase.py` verifying all 11 core engine stages end-to-end.
- **Windows Console Resiliency**: Safe UTF-8 console output handling preventing CP1252 character map errors.

### v0.5.0
- **ChromaDB Removal**: Transitioned to a unified SQLite WAL + NumPy SIMD in-memory vector cache engine, eliminating external C++ dependencies, multi-database sync bugs, and heavy binary bloat.
- **3-Stage Hybrid Search**: Combines Dense Cosine Similarity (`<0.1 ms`), SQLite FTS5 BM25 lexical ranking, and exact token overlap boosting.
- **Automatic Deduplication Engine**: Near-identical memories ($\ge 0.985$ similarity) reinforce existing records rather than creating duplicate rows.
- **Contradiction Tracking**: Conflicting statements ($\ge 0.82$ similarity with numeric/negation polarity changes) are stored and recorded in a contradiction log for review.
- **Time-to-Live (TTL) & Confidence Decay**: Linear time-based confidence decay for temporary facts.
- **Full 10-Tool MCP Server**: Complete tool suite for LLMs.
- **Interactive Web Dashboard**: Single-file dark/light UI with search, contradiction management, pattern analysis, and audit trails.
- **Passive Capture & Daily Digests**: Automated memory extraction from AI session logs (`mesh-capture`) and Markdown summary generation (`mesh-digest`).

---

## License

MIT © [billighost](https://github.com/billighost)

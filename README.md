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

### 1. Installation

```bash
pip install mesh-context-layer
```

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

### v0.5.3 (Latest)
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

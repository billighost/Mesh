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
- **5-Stage Hybrid Search**: Dense Vector SIMD Cosine (`cache.matrix @ q_vec` in `<0.1 ms`) + SQLite FTS5 BM25 + Robertson-Sparck Jones Smoothed IDF + Domain Synonyms + Exact Token Overlap.
- **Unified Local Storage**: Pure SQLite WAL per namespace (`~/.mesh/`, mode `0700`) — zero external database daemons, zero multi-process synchronization issues.
- **Smart Deduplication & Contradiction Tracking**: Identical facts are reinforced in-place; contradictory facts are flagged for review without silent data loss.
- **Time-to-Live (TTL) Confidence Decay**: Ephemeral facts gradually decay towards staleness (`--ttl <days>` / `--clear-ttl`).
- **Explicit Consent for Background Actions**: Interactive `y/n` user permission prompts before starting background processes, registering OS login auto-start, downloading model weights, or sending logs to cloud APIs (bypassable in automation via `--yes` or `MESH_AUTO_APPROVE=1`).
- **Multi-Interface**: MCP Server (10 tools for Claude Desktop, Cursor, Windsurf), Web Dashboard, HTTP REST API, and CLI.

---

## Quickstart

### 1. Installation & Setup

```bash
pip install mesh-context-layer

# Interactive setup (asks y/n before registering auto-start, starting the daemon, or downloading the ONNX model)
mesh setup

# Non-interactive setup for CI or automated environments
mesh setup --yes
```

### Explicit Consent & Non-Blocking Embedding Engine

Mesh respects your machine and network:
- **No Silent Background Daemons or Downloads**: Mesh asks for `y/n` confirmation before starting the background HTTP daemon (`[http://127.0.0.1:7701](http://127.0.0.1:7701)`), registering OS login auto-start, or downloading the ~90 MB `all-MiniLM-L6-v2` ONNX weights into `~/.mesh/models/`.
- **Instant Local Fallback**: In non-interactive environments (or if you decline the ONNX download), Mesh serves requests in `<0.05 ms` using its built-in 384-dimensional semantic feature-hash engine (`hash-384`), and automatically heals vectors in-place if the ONNX model is downloaded later.
- **No Silent Phone-Home**: `mesh status` and `mesh help` never contact PyPI unless you pass `mesh status --check-updates` or set `MESH_CHECK_UPDATES=1`.

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
Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

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

### Cursor Configuration
Create `.cursor/mcp.json` in your project root:

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

### Available MCP Tools

| MCP Tool | Description |
| :--- | :--- |
| `mesh_learn` | Store a fact, preference, instruction, or decision with deduplication and contradiction checking. |
| `mesh_recall` | 5-stage hybrid search across shared memory (excludes `local_only` private memories by default unless `include_private=true`). |
| `mesh_update` | Update existing memory content, tags, or TTL (`clear_ttl=true` supported) in-place preserving UUID and history. |
| `mesh_forget` | Permanently delete a specific memory by its ID (scrubbing audit & contradiction logs). |
| `mesh_inspect` | Browse recent memories in a namespace (excludes `local_only` memories by default unless `include_private=true`). |
| `mesh_context` | Generate prompt-injection-safe context blocks (`markdown`, `xml`, `json`, or `plain`) for system prompts. |
| `mesh_patterns` | Discover frequently accessed topics, memory gaps, unused memories, and stale memories. |
| `mesh_contradictions` | Inspect conflicting memories identified during learning. |
| `mesh_resolve_contradiction`| Mark a contradiction ID (or 4+ char short ID) as resolved. |
| `mesh_sync` | Save a turn summary and incrementally capture facts from session logs using local extraction. |

---

## Web Dashboard

Launch the zero-build local visualizer:

```bash
mesh-dashboard --namespace checkout-service --port 7433
```

Mesh prints and opens a pre-authenticated launch URL (`http://localhost:7433/?ns=checkout-service&token=...`) that sets an `HttpOnly; SameSite=Strict` session cookie and strips the token from the URL bar. Features:
- **Real-Time Hybrid Search** with instant score breakdowns and peripheral snippets.
- **Contradiction Inspector**: Review and resolve flagged memory conflicts.
- **Memory Gaps & Patterns**: See queries agents asked that had low recall similarity.
- **Audit Stream**: Real-time log of agent reads, writes, updates, and deletes.
- **Keyboard Shortcuts**: `/` to focus search, `n` to create a memory, `Esc` to close modals.

---

## HTTP REST API

Start the local background server:

```bash
mesh-http --port 7701
```

All endpoints (except `GET /health`) require your local per-user token (`~/.mesh/auth_token`, mode `0600`) via `Authorization: Bearer <token>` or `X-Mesh-Token: <token>`:

```bash
TOKEN=$(cat ~/.mesh/auth_token)

# Store a memory
curl -s -X POST http://127.0.0.1:7701/learn \
  -H "Content-Type: application/json" \
  -H "X-Mesh-Token: $TOKEN" \
  -d '{"content": "Auth service requires JWT RS256 signatures", "memory_type": "instruction"}'

# Recall memories
curl -s -X POST http://127.0.0.1:7701/recall \
  -H "Content-Type: application/json" \
  -H "X-Mesh-Token: $TOKEN" \
  -d '{"query": "auth JWT algorithm", "n": 3}'
```

---

## CLI Commands

```bash
# Setup & daemon management
mesh setup                        # Interactive setup (daemon + ONNX model)
mesh daemon start                 # Start background daemon (port 7701)
mesh daemon stop                  # Stop background daemon
mesh daemon status                # Inspect daemon health & model RAM residency
mesh daemon warmup                # Cache ONNX model & warm local daemon
mesh daemon unload                # Immediately release model weights from RAM

# Memory operations (automatically proxy through warm local daemon)
mesh add "PostgreSQL 16 runs on port 5432" --tags db,prod --type fact
mesh ask "what port does postgres use?" --n 3
mesh update <id> --content "Updated content" --clear-ttl
mesh forget <id>
mesh context --query "database config" --format markdown

# Inspection, diagnostics & utilities
mesh inspect                      # Browse memories in namespace
mesh conflicts                    # List or --resolve contradictions
mesh patterns                     # View top queries & memory gaps
mesh doctor                       # Verify SQLite FTS5, permissions, ONNX SHA-256 & MCP configs
mesh shell                        # Interactive REPL shell
mesh completion zsh --install     # Install shell Tab completion (bash, zsh, fish, powershell)
mesh namespaces                   # List, stats, rename, or delete namespaces
mesh export backup.json           # Export non-private memories to JSON
mesh import backup.json           # High-speed validated batch import
mesh capture --file session.log   # Extract memories from logs
mesh digest --hours 24            # UTC session summary
```

---

## Security, Privacy & Boundaries

### `local_only=True` (`--private`)
Marking a memory as `local_only=True`:
- **Excludes** it from `mesh export` (`export_namespace()`).
- **Excludes** it by default from `mesh context` (`build_context()`), `mesh_recall`, `mesh_inspect`, `/recall`, and `/inspect` unless `include_private=True` is explicitly passed.
- **Redacts** its text in `audit_log`, `contradictions`, `mesh digest`, and conflict warnings returned by `learn()`.
- **Scrubs** previous versions from `audit_log` and `contradictions` whenever a memory is edited via `update()` or deleted via `forget()`.

### Namespace Isolation (`MESH_LOCKED_NAMESPACE`) & Dynamic Storage (`MESH_HOME`)
- **`MESH_HOME` / `MESH_DIR`**: Override the default `~/.mesh` directory dynamically for full test isolation or containerized volumes.
- **`MESH_LOCKED_NAMESPACE`**: Namespaces (`mesh_<namespace>.db`) are logical project partitions designed to keep different codebases separated; by default, any authenticated local agent with your `~/.mesh/auth_token` can pass a `namespace` parameter. If you want to strictly confine an MCP server or HTTP instance to a single namespace, set `MESH_LOCKED_NAMESPACE=1` (or `MESH_LOCKED_NAMESPACE=<allowed-namespace>`) in its environment.
- **`MESH_ENFORCE_AGENT_OWNERSHIP`**: When set to `1`, prevents agents from updating or forgetting memories authored by other agents (`human-cli` and `dashboard` remain exempt).
- **`MESH_INCLUDE_PRIVATE`**: Set to `0` or `false` to globally exclude `local_only` private memories in Python SDK `recall()` and `inspect()` calls.

---

## License

MIT © [billighost](https://github.com/billighost)
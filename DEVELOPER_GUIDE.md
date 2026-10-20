# Mesh — Developer Guide (v0.8.1b2)

> High-speed shared semantic & hybrid memory for AI agents. One agent learns, every agent recalls.

---

## Table of Contents

1. [What Is Mesh?](#what-is-mesh)
2. [How It Works — The Big Picture](#how-it-works)
3. [Installation & Setup](#installation--setup)
4. [Core Concepts](#core-concepts)
   - [Namespaces & Confinement (`MESH_LOCKED_NAMESPACE`)](#namespaces--confinement-mesh_locked_namespace)
   - [Memory Types](#memory-types)
   - [Confidence, TTL Decay & Clearing TTLs](#confidence-ttl-decay--clearing-ttls)
   - [Deduplication & Contradiction Detection](#deduplication--contradiction-detection)
   - [Privacy (`local_only`) & Automatic Scrubbing](#privacy-local_only--automatic-scrubbing)
   - [User Consent Policy (`y/n` Prompts)](#user-consent-policy-yn-prompts)
5. [Python API](#python-api)
   - [`learn()`](#learn)
   - [`recall()` & `recall_with_context()`](#recall--recall_with_context)
   - [`update()`](#update)
   - [`get()` & `forget()`](#get--forget)
   - [`inspect()`, `patterns()`, `contradictions()`, & `resolve_contradiction()`](#inspect-patterns-contradictions--resolve_contradiction)
   - [Engine & Lifecycle Helpers (`ensure_model_downloaded`, `warmup_model`, `unload_model`, `get_engine_status`)](#engine--lifecycle-helpers)
6. [MCP Server (Claude Desktop, Cursor, Windsurf)](#mcp-server)
7. [HTTP REST API & Authentication](#http-rest-api--authentication)
8. [CLI Tools & Interactive Shell](#cli-tools--interactive-shell)
9. [Context Builder (`build_context` & `build_context_bundle`)](#context-builder)
10. [Passive Log Capture (`mesh capture`)](#passive-log-capture)
11. [Audit Trail & Retention](#audit-trail--retention)
12. [Session Digest](#session-digest)
13. [Export & Import](#export--import)
14. [Namespace Management](#namespace-management)
15. [Web Dashboard](#web-dashboard)
16. [Environment Variables](#environment-variables)
17. [Recipes & Multi-Agent Patterns](#recipes--multi-agent-patterns)

---

## What Is Mesh?

Every AI agent starts each session with amnesia. Claude in your terminal doesn't know what Cursor in your editor just discovered. Your code-review agent can't see what your debugging agent found ten minutes ago. Every tool repeats work that has already been done.

Mesh solves this by giving agents an ultra-fast, persistent, local hybrid memory layer. Agents write memories and recall them by meaning, keywords, domain synonyms, or exact identifiers—without ever needing to communicate directly.

```text
Agent A ──learn()──► [Mesh Unified SQLite WAL + SIMD Store] ◄──recall()── Agent B
Agent B ──update()─► [Mesh Unified SQLite WAL + SIMD Store] ◄──context()─ Agent C
```

All storage is **100% local** inside `~/.mesh/` (created with `0700` directory permissions and `0600` database/token file permissions on POSIX).

---

## How It Works

Mesh combines five retrieval and scoring stages inside a single atomic SQLite WAL database per namespace (`~/.mesh/mesh_<namespace>.db`):

1. **Pre-Scoring Filter Masks:** Candidates are filtered in RAM by `memory_type`, `tags`, `source_agent`, `local_only`, `include_stale`, and `min_confidence`.
2. **In-Memory SIMD Vector Search (Semantic Meaning):** Every memory is embedded into an L2-normalized 384-dimensional `float32` vector using direct `onnxruntime` + `tokenizers` (`all-MiniLM-L6-v2`, ~2 ms inference) or the instant `<0.05 ms` `hash-384` semantic fallback when offline. Cosine similarity across thousands of memories runs in `<0.1 ms` via NumPy SIMD matrix-vector multiplication (`cache.matrix @ q_vec`).
3. **Pre-Cached Robertson-Sparck Jones Smoothed IDF & Lexical Matching:** Every memory's tokens, stems, and document frequencies (`stem_df`) are pre-computed once in `_VectorCacheEntry` so queries never re-tokenize the corpus in Python.
4. **SQLite FTS5 BM25 Search (`rowid`-indexed):** Full-text Porter-stemmed search over `memories_fts`, synchronized by integer `rowid` for $O(\log N)$ updates and deletes.
5. **Bidirectional Domain Synonym & Compound Expansion + Peripheral Context:** Expands technical synonyms (e.g. `postgres` ↔ `postgresql` ↔ `pgbouncer`, `auth` ↔ `jwt`) and returns `other_possible_memories` snippets when strict type/tag filters hide strong matches in other categories.

### What happens on `learn()`
1. Content is validated (non-empty, max `16,384` characters) and embedded via `_embed_with_tag()`, which atomically returns `(vector, embedding_model_tag)` (with `<0.01 ms` hits from the 4,096-entry `_EMBED_CACHE`). Texts longer than ~256 tokens are automatically encoded via overlapping multi-window mean pooling.
2. If the namespace has existing memories, Mesh checks the top 3 closest vectors:
   - **Pass 1 (Deduplication):** If an existing memory is identical or near-identical (`similarity >= 0.985`) with the exact same numbers, alphanumeric identifiers (e.g., `db1` vs `db2`, `v2` vs `v3`), negation polarity, and full-text word overlap, Mesh reinforces the existing memory in-place (merging tags and taking the higher confidence).
   - **Pass 2 (Contradiction Check):** If an existing memory conflicts with the new fact (subject-value conflict, numeric slot conflict like `port 5432` vs `port 5433`, negation flip, or exclusive attribute conflict), Mesh logs a contradiction record (automatically redacting `local_only` content) and returns a conflict warning while storing the new memory.
3. The memory, metadata, `rowid`-linked FTS5 row, and normalized `float32` vector BLOB are committed in a single SQLite WAL transaction, and `_VectorCacheEntry` is updated in-place in $O(1)$ amortized time.

### What happens on `recall()`
1. The query is embedded via `_embed_query()`, softly blending domain synonyms for short queries.
2. `MeshStore.hybrid_query()` fuses Dense SIMD Cosine similarity + FTS5 BM25 + Pre-cached Smoothed IDF + Exact Identifier/Synonym matching in a single pass.
3. Each candidate's `effective_confidence` is calculated using a single UTC `now` timestamp reference.
4. Results are ranked by `composite_score` (`0.72 * similarity + 0.22 * effective_confidence + 0.06 * usage_boost`).
5. When `record_telemetry=True` (the default for `recall()`, disabled for `build_context()` to prevent ranking feedback loops), `access_count` increments and bounded `queries` logging run in a single atomic WAL transaction using copy-on-write cache replacement.

---

## Installation & Setup

```bash
# Lightweight core install (ONNX Runtime + Tokenizers + FastAPI + MCP + CLI)
pip install mesh-context-layer

# Optional: Install with test suite dependencies
pip install -e ".[test]"

# Optional: Install PyTorch sentence-transformers or fastembed backends
pip install "mesh-context-layer[pytorch,fastembed]"
```

Run interactive setup to configure auto-start, launch the background daemon, and cache the SHA-256 verified ONNX model:

```bash
mesh setup
# Or skip interactive y/n prompts in CI/scripts:
mesh setup --yes
```

---

## Core Concepts

### Namespaces & Confinement (`MESH_LOCKED_NAMESPACE`)

A namespace is an isolated memory partition stored in `~/.mesh/mesh_<namespace>.db` (validated against `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`).

```python
from mesh import Mesh

coder = Mesh(namespace="payments-service", agent_id="cursor-coder")
reviewer = Mesh(namespace="payments-service", agent_id="claude-reviewer")
```

> **Security Note on Namespaces:** By default, namespaces are logical project partitions—any authenticated caller holding your local `~/.mesh/auth_token` can pass a `namespace` parameter. To strictly confine a process (such as an MCP server or HTTP server) to a single namespace, set `MESH_LOCKED_NAMESPACE=1` (locks to `MESH_NAMESPACE`) or `MESH_LOCKED_NAMESPACE=payments-service`. Any attempt to access another namespace will raise a `ValueError`.

---

### Memory Types

Mesh validates 9 canonical memory types across all interfaces:

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

### Confidence, TTL Decay & Clearing TTLs

Every memory has a **stored confidence** (`0.0` to `1.0`) and an **effective confidence** (`confidence * decay_factor`).

- **Permanent Memories (`ttl_days=None`):** Never decay (`decay_factor = 1.0`, `is_stale = (confidence < 0.3)`).
- **Time-Sensitive Memories (`ttl_days=N`):** Confidence decays linearly from `1.0` to `0.0` over `N` days starting from `decay_start_at`:
  $$\text{decay\_factor} = \max\left(0.0, \min\left(1.0, 1.0 - \frac{\text{days\_elapsed}}{\text{ttl\_days}}\right)\right)$$
- **Staleness (`STALE_THRESHOLD = 0.3`):** Unified across `store.py`, `memory.py`, `namespaces.py`, `digest.py`, and `dashboard.py` via `_compute_row_effective_confidence()`. Whenever `effective_confidence < 0.3`, `is_stale` is `True`.
- **Clearing a TTL:** You can convert a decaying memory into a permanent memory at any time via Python (`mesh.update(id, clear_ttl=True)`), CLI (`mesh update <id> --clear-ttl`), MCP (`clear_ttl: true`), HTTP (`"clear_ttl": true`), or by clearing the TTL input box in the Web Dashboard.

---

### Deduplication & Contradiction Detection

When `mesh.learn()` is called with `deduplicate=True` (the default):

1. **Automatic In-Place Deduplication:** Near-identical statements (`similarity >= 0.985`) with identical numbers, alphanumeric identifiers (`db1`, `v2`), negation polarity, and word overlap reinforce the existing memory in-place, merging tags and preserving its UUID, creation timestamp, original `source_agent`, and `access_count`.
2. **Contradiction Detection:** Statements that conflict on numbers (`port 5432` vs `port 5433`), alphanumeric slots (`db1` vs `db2`), negation polarity, or subject-value pairs are both stored and recorded in the `contradictions` table. If the existing memory has `local_only=True`, its content is redacted to `"[REDACTED local_only memory]"` in both the database and the returned `conflict` dictionary.

---

### Privacy (`local_only`) & Automatic Scrubbing

Set `local_only=True` (or `--private` in the CLI) for credentials, local paths, or sensitive notes:

- **Never Exported:** `export_namespace()` skips all `local_only=True` memories.
- **Excluded by Default on Agent Endpoints:** `build_context()`, `mesh_recall`, `mesh_inspect`, `POST /recall`, and `GET /inspect` exclude `local_only=True` memories by default unless `include_private=True` is explicitly passed (or `MESH_MCP_INCLUDE_PRIVATE=1` is set).
- **Redacted in Logs, Conflicts & Digests:** `audit_log`, `contradictions`, and `generate_digest()` automatically replace private content with `"[REDACTED local_only memory]"`.
- **Scrubbed on `update()` and `forget()`:** Editing a memory's text or marking it `local_only=True` via `update()` automatically scrubs the previous text from historical `audit_log` (`learn`, `update`, and `recall.top_result`) rows and `contradictions` rows. Calling `forget()` purges all occurrences to `"[PURGED]"`.

---

### User Consent Policy (`y/n` Prompts)

Mesh never starts background processes, downloads large models, modifies shell RC files, or uploads logs to cloud LLMs without asking the user `(y/n)` in interactive terminals:
- Approve automatically in scripts/CI with `--yes` / `-y` or `export MESH_AUTO_APPROVE=1`.
- Decline automatically in non-interactive environments (or when `export MESH_NON_INTERACTIVE=1` is set).

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

---

### `recall()` & `recall_with_context()`

```python
# Primary results list
results = mesh.recall(
    query: str,
    n: int = 5,
    min_similarity: float = 0.0,
    min_confidence: float = 0.0,
    include_stale: bool = True,
    memory_type: Optional[str] = None,
    tags: Optional[list[str]] = None,
    source_agent: Optional[str] = None,
    include_private: bool = True,
    record_telemetry: bool = True,
) -> list[dict[str, Any]]

# Primary results + peripheral `other_possible_memories` snippets + `filter_hint`
bundle = mesh.recall_with_context(
    query="database port",
    n=5,
    memory_type="fact",
    max_other_possible=5,
    include_private=False,
)
```

---

### `update()`

```python
updated = mesh.update(
    memory_id="uuid-or-short-id",
    content="Production PostgreSQL runs on port 5433",
    memory_type="fact",
    confidence=0.95,
    tags=["db", "postgres"],
    ttl_days=None,
    clear_ttl=True,       # Removes any existing TTL and makes the memory permanent
    local_only=False,
)
```

---

### `get()` & `forget()`

```python
mem = mesh.get("uuid-or-short-id")       # Returns enriched dict or None
deleted = mesh.forget("uuid-or-short-id") # Deletes & scrubs audit/conflict logs
```

---

### `inspect()`, `patterns()`, `contradictions()`, & `resolve_contradiction()`

```python
# List memories sorted by accessed_at DESC
memories = mesh.inspect(limit=20, memory_type="decision", tag="arch", include_private=False)

# Surface top queries, low-similarity memory gaps, unused memories, and stale memories
p = mesh.patterns()

# List and resolve contradictions (raises ValueError if contradiction_id is not found)
open_conflicts = mesh.contradictions(resolved=False)
mesh.resolve_contradiction("conflict-uuid-or-short-id")
```

---

### Engine & Lifecycle Helpers

```python
from mesh import (
    ensure_model_downloaded,
    get_engine_status,
    set_idle_unload_seconds,
    unload_model,
    warmup_model,
)

# Download ONNX model (prompts y/n interactively unless assume_yes=True)
pair = ensure_model_downloaded(blocking=True, timeout=60.0, assume_yes=True)

# Warm the ONNX model and heal any hash-384 vectors in the namespace
warmup_model("my-project", assume_yes=True)

# Inspect model residency and cache metrics in O(1)
status = get_engine_status()

# Immediately unload neural weights from RAM
unload_model()
```

---

## MCP Server

The MCP server (`mesh-server`) exposes 10 tools over `stdio`.
- By default, `mesh_recall` and `mesh_inspect` set `include_private=False` so `local_only` secrets are not sent to external LLM providers unless the agent or user explicitly passes `include_private=True` (or sets `MESH_MCP_INCLUDE_PRIVATE=1`).
- When the local HTTP daemon is running on `127.0.0.1:7701`, `mesh-server` proxies calls through it using fresh connections for mutating operations and pre-checked keep-alive sockets for reads.

---

## HTTP REST API & Authentication

Start the HTTP API server on `[http://127.0.0.1:7701](http://127.0.0.1:7701)`:

```bash
mesh-http --port 7701
```

Every endpoint except `GET /health` requires your per-user 256-bit token stored in `~/.mesh/auth_token` (created atomically with `0600` permissions) via `Authorization: Bearer <token>` or `X-Mesh-Token: <token>`.

| Method | Path | Description |
| :--- | :--- | :--- |
| `GET` | `/health` | Unauthenticated health & engine status check |
| `GET` | `/engine` | Authenticated engine & cache status |
| `POST` | `/unload` | Unload neural model weights from RAM |
| `POST` | `/learn` | Store a memory |
| `POST` | `/recall` | 5-stage hybrid search + `other_possible_memories` (`include_private=False` default) |
| `POST` | `/update` | Update a memory in-place (`clear_ttl` supported) |
| `POST` | `/forget` | Delete a memory by ID or short prefix |
| `GET` | `/inspect` | List memories (`include_private=False` default) |
| `GET` | `/patterns` | Pattern & memory gap analysis |
| `GET` | `/contradictions` | List unresolved or resolved contradictions |
| `POST` | `/contradictions/resolve` | Resolve a contradiction by ID or short prefix (404 if not found) |
| `POST` | `/context` | Build a prompt-injection-safe system prompt block |
| `GET` | `/audit` | Query the audit trail |

> **Unit Testing Note:** To test `app` (in `http_server.py`) or `api` (in `dashboard.py`) with FastAPI's `TestClient` without passing headers on every call, override the auth dependency:
> ```python
> from mesh.http_server import app, verify_request_auth
> app.dependency_overrides[verify_request_auth] = lambda: None
> ```

---

## CLI Tools & Interactive Shell

```bash
# Setup & Diagnostics
mesh setup [--yes]
mesh doctor                       # Verifies ~/.mesh permissions, SQLite FTS5, ONNX SHA-256, daemon & MCP configs
mesh status [--check-updates]     # Namespace summary (only checks PyPI when --check-updates is passed)

# Core Memory CRUD
mesh add "Fact text" --type fact --tags db,prod [--private] [--ttl 30]
mesh ask "query" --count 5 [--type fact] [--tags db] [--no-stale] [--no-private]
mesh inspect [--limit 30] [--type fact] [--tag db] [--no-private]
mesh update <id> [--content "..."] [--clear-ttl] [--private | --public]
mesh forget <id>

# Interactive REPL Shell & Completions
mesh shell --namespace my-project  # Supports `ns <new-ns>` to switch namespaces live
mesh completion zsh --install      # Supports bash, zsh, fish, powershell (idempotent)
```

---

## Context Builder

`build_context()` and `build_context_bundle()` format top memories into a system prompt block (`markdown`, `plain`, `xml`, or `json`):
- Excludes `local_only=True` memories by default (`include_private=False`).
- Calls `recall(..., record_telemetry=False)` so generating prompts never inflates `access_count`.
- Collapses newlines/control characters, strips leading markdown heading/bullet markers, and neutralizes case-insensitive `<mesh_context` tags so untrusted memory text cannot break out of the block or forge prompt sections.

---

## Passive Log Capture

`extract_memories_from_log()` and `mesh capture` extract reusable facts from raw text, `.log`/`.txt`/`.md`/`.jsonl` files, or log directories:
- Redacts API keys, bearer tokens, URI passwords, and secret assignments via `_redact_secrets_for_cloud()` before any external API call.
- Prompts the user with `y/n` permission before sending redacted log text to any cloud LLM (`anthropic`, `openai`, `groq`, or custom `MESH_AI_BASE_URL` + `MESH_AI_API_KEY`).
- Falls back automatically to the offline regex/heuristic extractor (`--local-only` / `force_local=True`).

---

## Audit Trail & Retention

Every `recall`, `learn`, `update`, and `forget` is logged to `~/.mesh/audit_<namespace>.db` (mode `0600`).
- Automatically caps `audit_log` at `MESH_MAX_AUDIT_ROWS` (default `10,000`) and `queries` at `MESH_MAX_QUERY_ROWS` (default `10,000`) per namespace so database files never grow without bound.

---

## Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `MESH_HOME` / `MESH_DIR` | `~/.mesh` | Root directory override for Mesh data, database files, and auth tokens |
| `MESH_NAMESPACE` | `shared` | Default namespace across CLI, MCP, HTTP, and Dashboard |
| `MESH_DEFAULT_NAMESPACE` | `shared` | Fallback namespace when `MESH_NAMESPACE` is not set |
| `MESH_LOCKED_NAMESPACE` | — | Set to `1` or `<namespace>` to strictly lock the process to a single namespace |
| `MESH_AGENT_ID` | `mcp-agent` / `http-client` | Default agent identifier recorded on memories and audit logs |
| `MESH_ENFORCE_AGENT_OWNERSHIP` | `0` | Set to `1` to prevent agents from modifying memories owned by other agents (`human-cli` and `dashboard` exempt) |
| `MESH_INCLUDE_PRIVATE` | `1` | Set to `0` or `false` to globally exclude private (`local_only`) memories from SDK recalls/inspects |
| `MESH_MCP_INCLUDE_PRIVATE` | `0` | Set to `1` to include `local_only` memories by default in `mesh_recall` and `mesh_inspect` |
| `MESH_AUTH_TOKEN` / `MESH_TOKEN` | `~/.mesh/auth_token` | Explicit token override for daemon, MCP proxy, and dashboard authentication |
| `MESH_AUTH_REQUIRED` | `1` | Set to `0` to disable HTTP authentication in isolated testing environments |
| `MESH_AUTO_APPROVE` | `0` | Set to `1` or `true` to auto-approve all interactive `y/n` permission prompts |
| `MESH_NON_INTERACTIVE` | `0` | Set to `1` or `true` to auto-decline interactive prompts without blocking |
| `MESH_NO_AUTO_DAEMON` | `0` | Set to `1` to prevent automated background daemon launching on CLI/MCP calls |
| `MESH_NO_DAEMON_PROXY` | `0` | Set to `1` to disable proxying CLI and MCP commands through the warm daemon |
| `MESH_HTTP_HOST` | `127.0.0.1` | Host interface for the HTTP daemon |
| `MESH_HTTP_PORT` | `7701` | Port for the HTTP daemon |
| `MESH_IDLE_UNLOAD_SECONDS` | `180` | Inactivity seconds before neural model weights are unloaded from RAM (`0` to disable) |
| `MESH_NO_MODEL_DOWNLOAD` | `0` | Set to `1` to disable ONNX model downloads and always use `hash-384` |
| `MESH_EMBEDDING_PROVIDER` | *auto-detected* | Explicitly select embedding provider (`onnx` or `hash-384`) |
| `MESH_FALLBACK_DEVICE` | `cpu` | Device target when hardware acceleration or neural providers are unavailable |
| `MESH_AUTO_CONTRADICTION_DETECTION` | `1` | Set to `0` to bypass automatic contradiction and conflict analysis on `learn()` |
| `MESH_ALLOW_LOG_SYNC` | `0` | Set to `1` to explicitly permit `mesh_sync` to ingest session logs from disk |
| `MESH_ENABLE_AUTO_SYNC` | `0` | Set to `1` to enable automatic background log ingestion |
| `MESH_ALLOW_CLOUD_SYNC` | `0` | Set to `1` to permit passive extraction via cloud AI providers after redaction |
| `MESH_LOG_LEVEL` | `INFO` | Logging verbosity level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `MESH_MAX_CONTENT_LENGTH` | `16384` | Maximum characters allowed in a single memory |
| `MESH_MAX_AUDIT_ROWS` | `10000` | Maximum rows retained in `audit_log` per namespace |
| `MESH_MAX_QUERY_ROWS` | `10000` | Maximum rows retained in `queries` per namespace |
| `MESH_CHECK_UPDATES` | `0` | Set to `1` to opt into background PyPI version checks |
| `MESH_AI_PROVIDER` | *auto-detected* | Explicit passive capture provider (`anthropic`, `openai`, `groq`, `ollama`, `openai-compatible`, `local`) |
| `MESH_AI_API_KEY` | — | API key override for passive capture (works with `MESH_AI_BASE_URL`) |
| `MESH_AI_BASE_URL` | *provider default* | Custom base URL for OpenAI-compatible or Ollama endpoints |
| `MESH_AI_MODEL` | *provider default* | Explicit LLM model override for passive capture |
# Mesh — Developer Guide

> Shared semantic memory for AI agents. One agent learns, every agent recalls.

---

## Table of Contents

1. [What Is Mesh?](#what-is-mesh)
2. [How It Works — The Big Picture](#how-it-works)
3. [Installation](#installation)
4. [Core Concepts](#core-concepts)
   - [Namespaces](#namespaces)
   - [Memory Types](#memory-types)
   - [Confidence & Decay](#confidence--decay)
   - [Contradiction Detection](#contradiction-detection)
   - [Privacy (local_only)](#privacy-local_only)
5. [Python API](#python-api)
   - [learn()](#learn)
   - [recall()](#recall)
   - [forget()](#forget)
   - [inspect()](#inspect)
   - [patterns()](#patterns)
   - [contradictions()](#contradictions)
   - [resolve_contradiction()](#resolve_contradiction)
6. [MCP Server (Claude Desktop & Cursor)](#mcp-server)
7. [HTTP REST API](#http-rest-api)
8. [CLI Tools](#cli-tools)
9. [Context Builder](#context-builder)
10. [Passive Capture](#passive-capture)
11. [Audit Trail](#audit-trail)
12. [Session Digest](#session-digest)
13. [Export & Import](#export--import)
14. [Namespace Management](#namespace-management)
15. [Dashboard](#dashboard)
16. [Environment Variables](#environment-variables)
17. [Recipes & Patterns](#recipes--patterns)

---

## What Is Mesh?

Every AI agent starts each session with amnesia. Claude in your terminal doesn't know what Claude in your editor just discovered. Your review agent can't see what your coding agent found an hour ago. Every tool repeats work that's already been done.

Mesh solves this by giving agents a shared, persistent, semantic memory layer. Agents write memories. Agents read memories. They never need to talk to each other directly.

```
Agent A ──learn()──► [Mesh Store] ◄──recall()── Agent B
Agent B ──learn()──► [Mesh Store] ◄──recall()── Agent C
```

All storage is **local** (`~/.mesh/`). Nothing leaves your machine.

---

## How It Works

Mesh has two storage layers working in tandem:

**ChromaDB** handles the semantic layer — every memory is converted to a vector embedding using a local sentence transformer model (`all-MiniLM-L6-v2`). When you `recall()`, Mesh embeds your query and performs a cosine similarity search. This is why you can ask "what are the API rate limits?" and get back "The production API allows 100 requests per minute" — even though those exact words don't match.

**SQLite** handles the metadata layer — every memory has a row in a local SQLite database (`~/.mesh/mesh_<namespace>.db`) that stores the content, type, confidence, tags, TTL, timestamps, access counts, and privacy flag.

When you call `learn()`:
1. The content is embedded with `SentenceTransformer`
2. Existing memories are checked for contradictions (cosine similarity ≥ 0.82)
3. The embedding is upserted into ChromaDB
4. Metadata is written to SQLite
5. A `dict` is returned with the memory ID and any conflict warning

When you call `recall()`:
1. The query is embedded
2. ChromaDB returns the top N most similar memories
3. Each result's confidence is multiplied by a decay factor (if TTL is set)
4. Results are filtered by `min_similarity` and `min_confidence`
5. Every query is logged to the `queries` table for pattern analysis
6. Results are returned sorted by similarity, with full decay metadata

---

## Installation

```bash
pip install mesh-context-layer
```

**First run** downloads `all-MiniLM-L6-v2` (~90 MB) once to your local model cache. After that, everything runs offline.

---

## Core Concepts

### Namespaces

A namespace is an isolated memory partition. Agents sharing a namespace share memory. Agents in different namespaces are completely isolated.

```python
# Two agents on the same project share memory
agent_a = Mesh(namespace="my-project", agent_id="coder")
agent_b = Mesh(namespace="my-project", agent_id="reviewer")

# This agent is isolated
other = Mesh(namespace="other-project", agent_id="coder")
```

Set the default namespace globally via environment variable:

```bash
export MESH_NAMESPACE=my-project
```

**Storage:** each namespace gets its own SQLite file (`mesh_<namespace>.db`) and ChromaDB collection (`mesh_<namespace>`), both in `~/.mesh/`.

---

### Memory Types

| Type | Use it for |
|------|-----------|
| `fact` | Objective, verifiable information |
| `preference` | User or system preferences and settings |
| `context` | Situational background and project state |
| `result` | Outputs and outcomes of completed tasks |
| `instruction` | Rules, guidelines, and constraints to follow |

```python
mesh.learn("The API rate limit is 100 req/min", memory_type="fact")
mesh.learn("User prefers tabs over spaces", memory_type="preference")
mesh.learn("We're mid-sprint, focusing on auth", memory_type="context")
mesh.learn("Refactored auth module, all tests pass", memory_type="result")
mesh.learn("Always run tests before pushing to main", memory_type="instruction")
```

The type is stored in metadata and used for grouping in context output and filtering in recall.

---

### Confidence & Decay

Every memory has a **stored confidence** (0.0–1.0) and an **effective confidence** (confidence × decay_factor).

**Stored confidence** represents how certain you are about the information at the time of storage. Use values below 0.7 for uncertain information:

```python
mesh.learn("I think staging runs on port 4000", confidence=0.6)
```

**TTL decay** (`ttl_days`) makes a memory's effective confidence decay linearly to 0 over the specified number of days. After `ttl_days` days, the memory is considered **stale** (effective_confidence < 0.3). Use this for information that goes out of date:

```python
# Staging server IP — probably changes every few weeks
mesh.learn("Staging IP is 192.168.1.100", ttl_days=14)

# Sprint goal — stale after a week
mesh.learn("Sprint goal: ship auth flow", ttl_days=7)

# Permanent fact — no TTL
mesh.learn("Python is our primary language")
```

When you `recall()`, every result includes:

```python
{
    "confidence": 1.0,           # stored confidence
    "effective_confidence": 0.6, # after decay
    "decay_factor": 0.6,         # multiplier applied
    "is_stale": False,           # True if effective_conf < 0.3
    "days_until_stale": 4,       # estimated days remaining
}
```

---

### Contradiction Detection

When you `learn()` something new, Mesh checks whether any existing memory has a cosine similarity ≥ 0.82. If so, it records a contradiction and returns a warning — but **still stores the new memory**. The agent decides what to do.

```python
mesh.learn("The API rate limit is 100 req/min")

result = mesh.learn("The API rate limit is 200 req/min")
# result["status"] == "stored_with_conflict"
# result["conflict"] == {
#     "existing_memory_id": "...",
#     "existing_content": "The API rate limit is 100 req/min",
#     "similarity": 0.91,
#     "stored_by": "coder-agent",
#     "contradiction_id": "..."
# }
```

To list and resolve contradictions:

```python
# Get all unresolved contradictions
conflicts = mesh.contradictions()

# After reviewing, mark as resolved
mesh.resolve_contradiction(conflict["id"])
```

---

### Privacy (local_only)

Mark any memory as `local_only=True` to ensure it **never** appears in exports or future cloud syncs. Use this for credentials, API keys, and sensitive configuration:

```python
mesh.learn(
    "Database password: hunter2",
    local_only=True
)
```

Local-only memories are stored and recalled normally — they're just excluded from `export_namespace()` output and flagged in the dashboard.

---

## Python API

### `learn()`

```python
result = mesh.learn(
    content: str,
    memory_type: str = "fact",      # fact | preference | context | result | instruction
    confidence: float = 1.0,        # 0.0 – 1.0
    tags: list[str] = [],           # optional labels
    ttl_days: float = None,         # days before stale
    local_only: bool = False        # exclude from exports
) -> dict
```

**Returns:**

```python
{
    "memory_id": "uuid",
    "status": "stored" | "stored_with_conflict",
    "conflict": None | {
        "existing_memory_id": "...",
        "existing_content": "...",
        "similarity": 0.91,
        "stored_by": "agent-name",
        "contradiction_id": "..."
    }
}
```

---

### `recall()`

```python
results = mesh.recall(
    query: str,
    n: int = 5,                     # max results to return
    min_similarity: float = 0.0,    # filter below this
    min_confidence: float = 0.0,    # filter below this effective confidence
    include_stale: bool = True      # set False to exclude stale memories
) -> list[dict]
```

**Each result:**

```python
{
    "id": "uuid",
    "content": "The API rate limit is 100 req/min",
    "similarity": 0.91,
    "source_agent": "coder-agent",
    "memory_type": "fact",
    "confidence": 1.0,
    "effective_confidence": 0.85,
    "decay_factor": 0.85,
    "is_stale": False,
    "days_until_stale": 8,
    "local_only": False,
    "created_at": "2025-01-15T10:30:00"
}
```

Results are sorted by similarity descending.

---

### `forget()`

```python
mesh.forget(memory_id: str) -> None
```

Deletes from both ChromaDB and SQLite. Permanent.

---

### `inspect()`

```python
memories = mesh.inspect(limit: int = 20) -> list[dict]
```

Returns all memories in the namespace, sorted by most recently accessed. Good for listing everything without a query.

---

### `patterns()`

```python
patterns = mesh.patterns() -> dict
```

**Returns:**

```python
{
    "top_topics": [
        {"query_text": "deploy process", "query_count": 8, "avg_similarity": 0.76}
    ],
    "memory_gaps": [
        # Queries asked 2+ times with avg_similarity < 0.4
        # These are things agents keep asking about but memory doesn't answer well
        {"query_text": "auth flow", "query_count": 4, "avg_similarity": 0.28}
    ],
    "unused_memories": [
        # Created 7+ days ago, access_count = 0
        {"id": "...", "content": "...", "created_at": "..."}
    ],
    "stale_memories": [
        # effective_confidence < 0.3
        {"content": "...", "effective_confidence": 0.12}
    ]
}
```

Use `patterns()` to understand what agents are looking for and what's missing from your memory store.

---

### `contradictions()`

```python
conflicts = mesh.contradictions(resolved: bool = False) -> list[dict]
```

Returns unresolved (or resolved) contradiction records.

---

### `resolve_contradiction()`

```python
mesh.resolve_contradiction(contradiction_id: str) -> None
```

Marks a contradiction as resolved. Does not delete either memory — you manage that separately with `forget()`.

---

## MCP Server

The MCP server exposes Mesh as tools directly inside Claude Desktop, Cursor, and any MCP-compatible client.

**Setup — Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json`):

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

**Setup — Cursor** (`.cursor/mcp.json` in project root):

```json
{
  "mcpServers": {
    "mesh": {
      "command": "mesh-server",
      "env": { "MESH_NAMESPACE": "my-project" }
    }
  }
}
```

**Available MCP tools:**

| Tool | Description |
|------|-------------|
| `mesh_learn` | Store a memory with optional type, confidence, tags, TTL, privacy flag |
| `mesh_recall` | Semantic search with similarity and decay info |
| `mesh_forget` | Delete a memory by ID |
| `mesh_inspect` | List all memories in the namespace |
| `mesh_patterns` | Surface gaps, stale/unused memories, top topics |
| `mesh_resolve_contradiction` | Mark a conflict as resolved |

**Prompt to give your agent:**

```
You have access to Mesh shared memory via the mesh_* tools.
- At the start of each task, call mesh_recall to check what's already known.
- When you discover something important (a decision, a fact, a preference), call mesh_learn.
- If mesh_learn returns a conflict warning, review and resolve it.
- Use local_only=true for any credentials or sensitive information.
```

---

## HTTP REST API

Mesh exposes a local REST API on port 7701. Start it with:

```bash
mesh-server-http
# or
MESH_NAMESPACE=my-project uvicorn mesh.http_server:app --port 7701
```

Any tool that can make HTTP requests can use Mesh through this API — shell scripts, curl, n8n, Zapier local, etc.

### Endpoints

#### `POST /learn`

```bash
curl -X POST http://localhost:7701/learn \
  -H "Content-Type: application/json" \
  -d '{
    "content": "Production DB is on port 5432",
    "memory_type": "fact",
    "namespace": "my-project",
    "agent_id": "my-script"
  }'
```

```json
{
  "status": "ok",
  "result": {
    "memory_id": "...",
    "status": "stored",
    "conflict": null
  }
}
```

#### `POST /recall`

```bash
curl -X POST http://localhost:7701/recall \
  -H "Content-Type: application/json" \
  -d '{"query": "what port is the database on?", "count": 3, "namespace": "my-project"}'
```

```json
{
  "status": "ok",
  "count": 1,
  "results": [
    {
      "content": "Production DB is on port 5432",
      "similarity": 0.89,
      "memory_type": "fact",
      "effective_confidence": 1.0,
      "is_stale": false
    }
  ]
}
```

#### `POST /forget`

```bash
curl -X POST http://localhost:7701/forget \
  -d '{"memory_id": "uuid-here", "namespace": "my-project"}'
```

#### `GET /inspect?namespace=my-project&limit=20`

Returns all memories in the namespace.

#### `GET /patterns?namespace=my-project`

Returns pattern analysis output.

#### `GET /audit?namespace=my-project&limit=50`

Returns the audit trail of reads and writes.

#### `GET /health`

```json
{"status": "ok", "version": "0.4", "timestamp": "..."}
```

---

## CLI Tools

### `mesh-add` — store a memory

```bash
# Basic usage
mesh-add "The deploy script is at ./scripts/deploy.sh"

# With type and tags
mesh-add "Prod DB password rotated May 1" --type fact --tags db,security

# Private (never exported)
mesh-add "My API key is sk-abc123" --private

# With TTL (goes stale after 14 days)
mesh-add "Staging IP: 192.168.1.100" --ttl 14

# Specific namespace
mesh-add "Sprint goal: ship auth" --namespace my-project
```

### `mesh-ask` — search memories

```bash
# Basic search
mesh-ask "what is the deploy process?"

# More results
mesh-ask "authentication" --count 10

# Filter by type
mesh-ask "preferences" --type preference

# Specific namespace
mesh-ask "database config" --namespace my-project
```

**Example output:**

```
Found 2 memories in namespace 'shared':

  1. The deploy script is at ./scripts/deploy.sh
     type=process | similarity=0.87 | confidence=1.00 | from=human-cli

  2. Always run tests before deploying
     type=instruction | similarity=0.71 | confidence=1.00 | from=coder-agent
```

### `mesh-context` — generate a system prompt block

```bash
# Pipe directly into an agent
aider --system-prompt "$(mesh-context)" my-file.py

# Focused context
mesh-context --query "deploy process" --count 5

# Different formats
mesh-context --format xml
mesh-context --format plain
mesh-context --format json

# Capture to variable
CONTEXT=$(mesh-context --namespace my-project)
```

### `mesh-why` — audit trail

```bash
# Recent reads and writes
mesh-why

# Filter to reads only
mesh-why --action read

# More entries
mesh-why --limit 50 --namespace my-project
```

### `mesh-status` — overview

```bash
mesh-status

# Output:
# Mesh Status — namespace: shared
#   Memories:       12 total, 2 private, 1 stale
#   Exportable:     10
#   Total recalls:  47
#   Last activity:  2025-01-15T14:22:00
#   Types:          {'fact': 8, 'instruction': 3, 'preference': 1}
```

### `mesh-digest` — session summary

```bash
# Last 24 hours
mesh-digest

# Last 2 hours
mesh-digest --hours 2

# Machine-readable
mesh-digest --json
```

---

## Context Builder

The context builder formats memories as a ready-to-paste system prompt block. Use it to prime any agent with relevant knowledge before a task.

```python
from mesh import build_context

# General context (top memories by access frequency)
prompt_block = build_context(namespace="my-project", count=10)

# Focused on a topic
prompt_block = build_context(
    query="authentication and security",
    namespace="my-project",
    count=5,
    format="markdown"   # markdown | plain | xml | json
)

# Inject into your agent's system prompt
system_prompt = f"""
You are a helpful coding assistant.

{prompt_block}

Now help the user with their task.
"""
```

**Markdown output example:**

```
<mesh_context>
The following facts were stored by AI agents working on this project.
Trust these as ground truth unless you have newer information.

**Fact**
- The API rate limit is 100 requests per minute
- Production database is PostgreSQL 15 on port 5432

**Instruction**
- Always run tests before merging to main
- Use feature branches, never push directly to main

**Preference**
- User prefers TypeScript over JavaScript for new code

</mesh_context>
```

---

## Passive Capture

Passive capture extracts memories automatically from a conversation or session log — without the agent explicitly calling `learn()`.

```python
from mesh import extract_memories_from_log

# Feed in a raw conversation log
log = """
We decided to use Redis for session storage.
The staging environment is at staging.acme.com.
Note that the API keys expire every 90 days.
Okay sure, let me know if you need anything else.
"""

result = extract_memories_from_log(
    log_text=log,
    namespace="my-project",
    dry_run=False,  # set True to preview without storing
    verbose=True
)
# result["stored_count"] == 3
# result["provider_used"] == "anthropic"
```

**Provider detection order:**
1. `MESH_AI_PROVIDER` env var (explicit override)
2. `ANTHROPIC_API_KEY` → uses Claude Haiku
3. `OPENAI_API_KEY` → uses OpenAI-compatible API
4. `GROQ_API_KEY` → uses Llama3 on Groq
5. Ollama running locally → uses detected model
6. Local keyword extractor (no API required)

The local extractor uses heuristics — it looks for sentences with signal words like "always", "decided", "the X is Y", "remember", etc. and filters out conversational filler. It also detects credential-like content and marks it `private`.

**Shell pipeline:**

```bash
# Capture your terminal session and extract memories at the end
script -q session.log
# ... work ...
mesh-passive session.log --namespace my-project
```

---

## Audit Trail

Every `learn()` and `recall()` call is logged to a separate audit database (`~/.mesh/audit_<namespace>.db`).

```python
from mesh import get_audit_log, get_audit_stats

# Recent entries
entries = get_audit_log(namespace="my-project", limit=20)
# entries[0] == {
#     "timestamp": "2025-01-15T14:22:00",
#     "action": "recall",
#     "agent_id": "coder-agent",
#     "query": "what is the deploy process?",
#     "top_result": "The deploy script is at ./deploy.sh",
#     "top_similarity": 0.87,
#     "result_count": 3
# }

# Filter by action
recalls = get_audit_log(namespace="my-project", action_filter="read")
learns  = get_audit_log(namespace="my-project", action_filter="write")

# Aggregate stats
stats = get_audit_stats(namespace="my-project")
# {
#     "total_recalls": 47,
#     "total_learns": 12,
#     "agents": ["coder-agent", "review-agent", "claude-desktop"],
#     "conflicts_detected": 2,
#     "last_activity": "2025-01-15T14:22:00"
# }
```

---

## Session Digest

A digest summarizes activity over a time window — what was learned, contradictions detected, and what's going stale.

```python
from mesh import generate_digest

digest = generate_digest(namespace="my-project", hours=24)
# {
#     "new_count": 8,
#     "new_memories": [...],
#     "contradiction_count": 1,
#     "contradictions": [...],
#     "stale_count": 2,
#     "stale_memories": [...],
#     "total_recalls": 23,
#     "agents_active": ["coder-agent", "claude-desktop"]
# }
```

---

## Export & Import

```python
from mesh import export_namespace, import_namespace

# Export all non-private memories to a file
export_namespace(
    namespace="my-project",
    output_path="backup.json",
    include_embeddings=True  # False = smaller, human-readable file
)

# Import on another machine or namespace
import_namespace(
    input_path="backup.json",
    target_namespace="my-project",   # override namespace from file
    merge_strategy="skip_existing",  # or "overwrite"
    dry_run=False
)
```

**The export format:**

```json
{
  "mesh_version": "0.3",
  "exported_at": "2025-01-15T14:22:00Z",
  "namespace": "my-project",
  "memory_count": 10,
  "skipped_local_only": 2,
  "memories": [
    {
      "id": "uuid",
      "content": "The API rate limit is 100 req/min",
      "memory_type": "fact",
      "confidence": 1.0,
      "source_agent": "coder-agent",
      "tags": ["api"],
      "created_at": "2025-01-10T09:00:00",
      "embedding": [0.023, -0.14, ...]
    }
  ]
}
```

If `include_embeddings=False`, the embedding field is omitted. On import, missing embeddings are regenerated by re-running the sentence transformer.

---

## Namespace Management

```python
from mesh import list_namespaces, delete_namespace, rename_namespace, namespace_stats

# List all namespaces
namespaces = list_namespaces()
# [{"namespace": "my-project", "memory_count": 10, "local_only_count": 2, "last_updated": "..."}]

# Detailed stats for one namespace
stats = namespace_stats("my-project")
# {"memory_count": 10, "type_breakdown": {"fact": 7, "instruction": 3}, ...}

# Delete (irreversible)
count = delete_namespace("old-project")

# Rename (copies data, deletes old)
count = rename_namespace("my-project", "my-project-archived")
```

---

## Dashboard

```bash
# Start with default namespace
mesh-dashboard

# Specific namespace and port
mesh-dashboard --namespace my-project --port 7433

# Don't auto-open browser
mesh-dashboard --no-browser
```

Opens at `http://localhost:7433`. The dashboard provides a full UI for browsing, searching, adding, editing, and deleting memories, viewing contradictions, patterns, audit logs, and digests.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MESH_NAMESPACE` | `shared` | Default namespace for all tools |
| `MESH_AGENT_ID` | `mcp-agent` / `http-client` | Agent ID used in logs |
| `MESH_HTTP_PORT` | `7701` | Port for the HTTP REST API |
| `MESH_AI_PROVIDER` | auto-detected | Override passive capture AI provider (`anthropic`, `openai`, `groq`, `ollama`, `local`) |
| `ANTHROPIC_API_KEY` | — | Used by passive capture for Claude Haiku |
| `OPENAI_API_KEY` | — | Used by passive capture for OpenAI |
| `GROQ_API_KEY` | — | Used by passive capture for Groq |

---

## Recipes & Patterns

### Multi-agent pipeline

```python
# Agent 1: discovery
scout = Mesh(namespace="research-sprint", agent_id="scout")
scout.learn("Found 3 competitor APIs: Stripe, Braintree, Adyen")
scout.learn("Stripe has the best developer docs")
scout.learn("Adyen requires manual approval to access sandbox")

# Agent 2: uses what Agent 1 learned — no direct communication
analyst = Mesh(namespace="research-sprint", agent_id="analyst")
results = analyst.recall("which payment APIs did we evaluate?")
# Finds all three memories from scout automatically
```

### Guard against stale credentials

```python
mesh.learn(
    "Staging API key: sk-staging-abc123",
    local_only=True,
    ttl_days=30,   # force review after a month
    memory_type="fact",
    tags=["credentials", "staging"]
)
```

### Inject memory into every agent session

```bash
#!/bin/bash
# wrapper.sh — inject Mesh context into any agentic tool

CONTEXT=$(mesh-context --namespace my-project --count 10 --format plain)
export AGENT_SYSTEM_PROMPT="$CONTEXT"
exec "$@"
```

### Check memory before doing work

```python
mesh = Mesh(namespace="my-project", agent_id="my-agent")

# Always check before duplicating work
existing = mesh.recall("has the auth module been refactored?", n=1, min_similarity=0.7)
if existing and existing[0]["is_stale"] is False:
    print(f"Already done: {existing[0]['content']}")
else:
    # do the work...
    mesh.learn("Refactored auth module — moved to JWT, all tests green", memory_type="result")
```

### Automated end-of-session digest

```bash
# Add to your project's .envrc or a post-session hook
mesh-digest --namespace my-project --hours 8
```

### Shell one-liners

```bash
# What has my team's agents been storing?
mesh-ask "recent decisions" --count 10 --namespace team-project

# What keeps getting asked that we don't have memory for?
mesh-namespaces stats my-project  # check stale + gap counts

# Quick status before starting work
mesh-status --namespace my-project && mesh-ask "current sprint goal"
```

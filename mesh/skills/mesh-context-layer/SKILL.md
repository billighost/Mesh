---
name: mesh-context-layer
description: >-
  Complete guide to installing, configuring, and fully leveraging Mesh — the
  high-speed shared semantic & hybrid memory layer for AI agents. Covers all
  10 MCP tools, the CLI, Python SDK, environment variables, memory lifecycle,
  contradiction management, privacy controls, and operational best practices.
  Activate this skill whenever you need to store, recall, update, inspect, or
  manage persistent agent memory across sessions.
---

# Mesh Context Layer — Full Agent Skill

> **Mesh** gives every AI agent persistent, shared memory. When one agent learns
> something, every other agent can recall it — by meaning, keyword, or exact
> identifier. This skill teaches you how to use Mesh to its full potential.

---

## 1. Installation & First-Time Setup

### 1.1 Install the Package

Mesh is a **Python package** published on PyPI as `mesh-context-layer`.
It requires **Python ≥ 3.10**.

```bash
pip install mesh-context-layer
```

> **Tip:** To verify it installed correctly, run: `mesh doctor`
> This checks SQLite FTS5, `~/.mesh` permissions, ONNX SHA-256 checksums,
> daemon status, and MCP config presence.

### 1.2 Interactive Setup

After installing, run the guided setup wizard:

```bash
mesh setup
```

This will interactively ask you (y/n) whether to:
1. **Download the ONNX embedding model** (~90 MB `all-MiniLM-L6-v2`) into
   `~/.mesh/models/` — if you decline, Mesh falls back to its built-in
   `hash-384` engine (< 0.05 ms, 384-dim) and auto-heals vectors later.
2. **Start the background HTTP daemon** on `http://127.0.0.1:7701`.
3. **Register OS login auto-start** for the daemon.
4. **Install the Mesh agent skill** into all detected AI coding agent skill
   folders (Antigravity, Gemini CLI, Claude Code, Cursor, Codex, OpenCode,
   Windsurf, and the universal `.agents/` standard).

For CI / automated environments, skip prompts with:

```bash
mesh setup --yes
```

### 1.3 Install the Agent Skill Separately

To install (or re-install) the Mesh skill into all detected AI agent skill
folders without running the full setup:

```bash
mesh install-skills
mesh install-skills --yes    # Skip y/n prompt
```

### 1.4 MCP Server Configuration

Mesh exposes **10 MCP tools** over stdio. Configure your AI client to launch
the MCP server:

#### Antigravity / Gemini CLI (`~/.gemini/config/mcp_config.json`)

```json
{
  "mcpServers": {
    "mesh": {
      "command": "mesh-server",
      "env": {
        "MESH_NAMESPACE": "my-project",
        "MESH_AGENT_ID": "antigravity-mcp"
      }
    }
  }
}
```

#### Claude Desktop (`claude_desktop_config.json`)

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

#### Cursor (`.cursor/mcp.json`)

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

> **Important:** Set `MESH_NAMESPACE` to your project name. This isolates
> memories into separate SQLite databases (`~/.mesh/mesh_<namespace>.db`).
> Set `MESH_AGENT_ID` to identify which agent wrote each memory.

---

## 2. Core Concepts

### 2.1 Namespaces

Namespaces are **logical project partitions**. Each namespace gets its own
SQLite WAL database and audit trail in `~/.mesh/`. Use different namespaces
for different projects so memories never bleed across.

- Default namespace comes from `MESH_NAMESPACE` env var.
- Every tool accepts an optional `namespace` override parameter.
- Manage namespaces via CLI: `mesh namespaces` (list, rename, delete, stats).

### 2.2 Memory Types

Every memory has a `memory_type` that classifies it. Use the most specific type:

| Type          | When to Use                                                          |
| :------------ | :------------------------------------------------------------------- |
| `fact`        | Objective, verifiable information (ports, versions, URLs, configs)   |
| `decision`    | Architectural or design choices and their rationale                  |
| `instruction` | Rules, constraints, or mandates the team must follow                 |
| `process`     | Multi-step workflows, runbooks, deployment procedures                |
| `preference`  | User or system preferences (formatting, style, tool choices)         |
| `context`     | Project background, domain knowledge, business logic                 |
| `result`      | Outcomes of completed tasks, experiment results, benchmarks          |
| `error`       | Bugs discovered, error patterns, and their fixes                     |
| `general`     | Anything that doesn't fit neatly into the above categories           |

### 2.3 Confidence & TTL Decay

- **Confidence** (0.0–1.0): How certain the information is. Use < 0.7 for
  tentative or unverified info.
- **TTL (Time-to-Live)**: Set `ttl_days` to make ephemeral facts gradually
  decay. After the TTL expires, effective confidence decays toward 0. A memory
  is considered **stale** when effective confidence drops below 0.3.
- **Clearing TTL**: Use `clear_ttl=true` on `mesh_update` to make a temporary
  memory permanent.

### 2.4 Privacy (`local_only`)

Marking a memory `local_only=true`:
- **Excludes** it from `mesh export`, `mesh_context`, `mesh_recall`,
  `mesh_inspect`, and HTTP API responses **by default**.
- **Redacts** its text in audit logs, contradictions, and conflict warnings.
- **Scrubs** previous versions from audit and contradiction logs on
  `update()` or `forget()`.
- To explicitly include private memories, pass `include_private=true`.

### 2.5 Deduplication & Contradiction Detection

When you `mesh_learn` a new memory:
1. **Deduplication**: If an identical or near-identical memory exists
   (similarity ≥ 0.985 + matching numbers/identifiers + high word overlap),
   the existing memory is reinforced in-place (tags merged, confidence
   takes the max).
2. **Contradiction Detection**: If a conflicting memory is found, the new
   memory is still stored, a `conflict` warning is returned, and a
   contradiction record is logged for later review.

---

## 3. The 10 MCP Tools — Complete Reference

### 3.1 `mesh_learn` — Store a Memory

**Purpose:** Store a fact, decision, instruction, or any information in shared
agent memory with automatic deduplication and contradiction checking.

**Required Parameters:**
- `content` (string): The specific fact to remember. Be precise and atomic —
  one fact per memory.

**Optional Parameters:**
- `memory_type` (string, default `"fact"`): One of the 9 types above.
- `confidence` (number, default `1.0`): 0.0–1.0. Use < 0.7 for tentative info.
- `tags` (string array): Topic labels, e.g. `["api", "auth", "db"]`.
- `ttl_days` (number): Days before confidence decays. E.g. `30` for a temp IP,
  `7` for a sprint goal.
- `local_only` (boolean, default `false`): Set true for sensitive/private data.
- `namespace` (string): Override the default namespace.

**Best Practices:**
- Write **atomic, specific** content: `"Production PostgreSQL 16 runs on port 5432"`
  NOT `"Database info"`.
- Tag generously — tags power filtered recall later.
- Use `memory_type="decision"` for "we chose X because Y" so decisions are
  easily found.
- Use `memory_type="error"` + relevant tags when you discover a bug and its fix.
- Set `ttl_days` on anything that might change (IP addresses, sprint goals,
  feature flags, staging URLs).

**Example:**
```json
{
  "content": "Auth service requires JWT RS256 signatures with 2048-bit keys",
  "memory_type": "instruction",
  "tags": ["auth", "jwt", "security"],
  "confidence": 1.0
}
```

---

### 3.2 `mesh_recall` — Search Memory

**Purpose:** Search shared agent memory using 5-stage hybrid search: dense
vector cosine similarity + BM25 full-text + Robertson-Sparck Jones IDF +
domain synonyms + exact token overlap.

**Required Parameters:**
- `query` (string): Natural language question, keywords, or exact identifiers.

**Optional Parameters:**
- `n` (integer, default `5`, max `50`): Max primary results.
- `min_similarity` (number, default `0`): Minimum similarity threshold (0–1).
- `min_confidence` (number, default `0`): Minimum effective confidence after
  decay.
- `include_stale` (boolean, default `true`): Set false to exclude decayed
  memories.
- `memory_type` (string): Filter to a single memory type.
- `tags` (string array): Require returned memories to have these tags.
- `source_agent` (string): Filter by which agent stored the memory.
- `include_private` (boolean, default `false`): Set true to include
  `local_only` memories.
- `include_other_possible` (boolean, default `true`): Include peripheral
  snippets of related or filter-excluded memories.
- `max_other_possible` (integer, default `5`, max `20`): Max peripheral
  snippets.
- `namespace` (string): Override the default namespace.

**Best Practices:**
- **Call `mesh_recall` at the START of every task** to check if relevant
  context already exists. This is the single most important habit.
- Use natural language queries — the hybrid search handles synonyms and
  meaning: `"what port does postgres use?"`.
- Check `other_possible_memories` in the response — they may contain relevant
  context that didn't make the top results.
- Filter by `memory_type` when you know what you're looking for:
  `memory_type="decision"` to find past architectural choices.
- Filter by `tags` to scope results: `tags=["auth"]`.

**Example:**
```json
{
  "query": "database connection configuration",
  "n": 5,
  "tags": ["db"],
  "include_stale": false
}
```

---

### 3.3 `mesh_update` — Modify an Existing Memory

**Purpose:** Update a memory in-place by its ID without losing its UUID,
creation timestamp, original author, or access count.

**Required Parameters:**
- `memory_id` (string): UUID or 4+ character short ID prefix.

**Optional Parameters (provide only what you want to change):**
- `content` (string): New text (automatically re-embeds the vector).
- `memory_type` (string): Updated type.
- `confidence` (number): Updated confidence score.
- `tags` (string array): Updated tag list (replaces existing tags entirely).
- `ttl_days` (number): Updated TTL (resets the decay timer).
- `clear_ttl` (boolean, default `false`): Set true to remove TTL and make
  permanent.
- `local_only` (boolean): Updated privacy flag.
- `namespace` (string): Override the default namespace.

**Best Practices:**
- Use `mesh_update` instead of forget + re-learn when information changes —
  it preserves history and avoids losing the access count.
- When information becomes permanent, use `clear_ttl=true` to remove the TTL.
- When updating content on a private memory, existing audit and contradiction
  records are automatically scrubbed.

---

### 3.4 `mesh_forget` — Delete a Memory

**Purpose:** Permanently delete a specific memory by ID, scrubbing its data
from audit logs and contradiction records.

**Required Parameters:**
- `memory_id` (string): UUID or 4+ character short ID prefix.

**Optional Parameters:**
- `namespace` (string): Override the default namespace.

> **Caution:** This is permanent and irreversible. All audit and contradiction
> records mentioning this memory are also scrubbed. Prefer `mesh_update` to
> correct information rather than deleting and re-creating.

---

### 3.5 `mesh_inspect` — Browse Memories

**Purpose:** List memories sorted by most recently accessed. Useful for
browsing what's in the namespace without a specific query.

**Optional Parameters:**
- `limit` (integer, default `10`, max `200`): How many memories to return.
- `memory_type` (string): Filter by type.
- `tag` (string): Filter by a single tag.
- `include_private` (boolean, default `false`): Include private memories.
- `namespace` (string): Override the default namespace.

**When to Use:**
- To get an overview of what's currently stored.
- To verify a memory was stored correctly after `mesh_learn`.
- To find the ID of a memory you need to update or delete.
- To audit what a specific namespace contains.

---

### 3.6 `mesh_context` — Generate Context Blocks

**Purpose:** Generate a formatted context block of top project memories ready
to prime an agent session or system prompt.

**Optional Parameters:**
- `query` (string): Optional topic to focus the context block on.
- `count` (integer, default `10`, max `50`): Number of memories to include.
  Also accepts `max_memories` as an alias.
- `format` (string, default `"markdown"`): Output format — `markdown`, `plain`,
  `xml`, or `json`.
- `min_confidence` (number, default `0.3`): Minimum confidence threshold.
- `memory_type` (string): Filter by type.
- `tags` (string array): Filter by tags.
- `include_private` (boolean, default `false`): Include private memories.
- `namespace` (string): Override the default namespace.

**When to Use:**
- At the **beginning of a new session** to load relevant project context.
- When building system prompts that need project-specific grounding.
- When handing off context between agents.

**Best Practices:**
- Use the `query` parameter to focus context on the current task rather than
  pulling everything: `query="authentication flow"`.
- Use `format="xml"` for structured agent consumption or `format="markdown"`
  for human-readable output.
- Context blocks are **prompt-injection-safe** — all content is sanitized.

---

### 3.7 `mesh_patterns` — Analyze Usage Patterns

**Purpose:** Analyze memory usage patterns across all agents in the namespace.

**Returns:**
- `top_topics`: Most frequently accessed topics.
- `memory_gaps`: Queries that agents frequently searched but got poor results
  for — these are knowledge gaps you should fill.
- `unused_memories`: Memories that have never or rarely been accessed.
- `stale_memories`: Memories with decayed confidence.

**Optional Parameters:**
- `namespace` (string): Override the default namespace.

**When to Use:**
- Periodically (e.g. end of sprint, end of session) to identify gaps in
  project knowledge.
- To clean up unused or stale memories.
- To discover what topics agents ask about most.

---

### 3.8 `mesh_contradictions` — View Conflicts

**Purpose:** List unresolved (or resolved) contradictions between stored
memories.

**Optional Parameters:**
- `resolved` (boolean, default `false`): Set true to list already-resolved
  contradictions instead.
- `namespace` (string): Override the default namespace.

**When to Use:**
- After `mesh_learn` returns a `"stored_with_conflict"` status.
- Periodically to review and clean up conflicting information.
- Before making decisions that depend on potentially conflicting facts.

---

### 3.9 `mesh_resolve_contradiction` — Resolve a Conflict

**Purpose:** Mark a contradiction as resolved after you've reviewed the
conflicting memories and decided which is correct.

**Required Parameters:**
- `contradiction_id` (string): UUID or 4+ character short ID prefix.

**Optional Parameters:**
- `namespace` (string): Override the default namespace.

**Workflow:**
1. Call `mesh_contradictions` to list unresolved conflicts.
2. Review the conflicting memories (both `new_content` and `existing_content`).
3. Decide which is correct — use `mesh_update` to correct the wrong one, or
   `mesh_forget` to delete the outdated one.
4. Call `mesh_resolve_contradiction` to mark the conflict as handled.

---

### 3.10 `mesh_sync` — Save Session Summary

**Purpose:** Store a session turn summary and capture concrete facts from
recent agent session activity.

**Optional Parameters:**
- `summary` (string): A brief summary of discoveries or work completed.
- `namespace` (string): Override the default namespace.

**When to Use:**
- At the **end of a session or significant task** to capture what was
  accomplished.
- To create a breadcrumb trail that other agents (or your future self) can
  follow.

**Best Practices:**
- Keep summaries factual and concrete: `"Migrated auth service from JWT HS256
  to RS256. Updated all middleware to validate RS256 tokens. Tests passing."`.
- Don't duplicate individual facts already stored via `mesh_learn` — sync is
  for the narrative summary.

---

## 4. Recommended Workflow

### 4.1 Session Start

```
1. mesh_recall  →  "What do I know about <current task>?"
2. mesh_context →  Generate a focused context block for the session
3. mesh_contradictions  →  Check for unresolved conflicts
```

### 4.2 During Work

```
4. mesh_learn   →  Store every non-trivial discovery, decision, or instruction
5. mesh_recall  →  Check existing knowledge before making decisions
6. mesh_update  →  Correct or enrich existing memories when info changes
```

### 4.3 Session End

```
7. mesh_sync    →  Summarize what was accomplished
8. mesh_patterns →  (Periodically) Identify knowledge gaps and unused memories
9. mesh_contradictions → Review and resolve any conflicts
```

---

## 5. What to Learn (Store) and What Not To

### DO Store:
- Architecture decisions and their rationale
- Configuration values (ports, URLs, connection strings, feature flags)
- Error patterns and their proven fixes
- API contracts and interface boundaries
- Team conventions and coding standards
- Deployment procedures and runbook steps
- Sprint goals and deadlines (with `ttl_days`)
- Temporary environment details (staging IPs, test credentials → `local_only`)
- Outcomes of experiments or investigations

### DON'T Store:
- Entire file contents (too large, not useful as memories)
- Transient debugging output
- Obvious or trivially discoverable information
- Information that changes every few minutes
- Secrets or credentials without `local_only=true`

---

## 6. Environment Variables

| Variable                      | Purpose                                                       | Default          |
| :---------------------------- | :------------------------------------------------------------ | :--------------- |
| `MESH_NAMESPACE`              | Default namespace for all operations                          | `"shared"`       |
| `MESH_AGENT_ID`               | Identifies the agent authoring memories                       | `"unknown"`      |
| `MESH_HOME` / `MESH_DIR`      | Override `~/.mesh` data directory                             | `~/.mesh`        |
| `MESH_LOCKED_NAMESPACE`       | Lock MCP/HTTP server to a single namespace                    | Not set          |
| `MESH_ENFORCE_AGENT_OWNERSHIP`| Prevent agents from modifying other agents' memories           | `0`              |
| `MESH_INCLUDE_PRIVATE`        | Globally control private memory visibility in SDK              | `1` (include)    |
| `MESH_AUTO_APPROVE`           | Skip interactive y/n prompts (for CI/automation)              | `0`              |
| `MESH_CHECK_UPDATES`          | Enable opt-in PyPI update checks                              | `0`              |
| `MESH_MAX_AUDIT_ROWS`         | Max audit log entries before pruning                          | `10000`          |
| `MESH_MAX_QUERY_ROWS`         | Max query telemetry rows before pruning                       | `10000`          |
| `MESH_IDLE_UNLOAD_SECONDS`    | Seconds of inactivity before unloading ONNX weights from RAM  | `180`            |

---

## 7. CLI Quick Reference

```bash
# Setup & diagnostics
mesh setup                          # Interactive first-time setup
mesh doctor                         # Verify everything is healthy
mesh status                         # Show namespace, daemon, and model status

# Daemon management
mesh daemon start                   # Start background HTTP daemon (port 7701)
mesh daemon stop                    # Stop daemon
mesh daemon status                  # Health check
mesh daemon warmup                  # Pre-load ONNX model into RAM

# Memory CRUD
mesh add "fact content" --type fact --tags tag1,tag2
mesh ask "natural language query" --n 5
mesh update <id> --content "new content" --clear-ttl
mesh forget <id>

# Browsing & analysis
mesh inspect                        # Browse recent memories
mesh context --query "topic" --format markdown
mesh conflicts                      # List contradictions
mesh patterns                       # Usage analytics

# Bulk operations
mesh export backup.json             # Export namespace to JSON
mesh import backup.json             # Batch import from JSON
mesh capture --file session.log     # Extract memories from logs

# Utilities
mesh namespaces                     # List/manage namespaces
mesh shell                          # Interactive REPL
mesh install-skills                 # Install skill to all AI agent folders
mesh completion powershell --install  # Install tab completion
mesh-dashboard --namespace myproj   # Launch web dashboard
```

---

## 8. Python SDK Quick Reference

```python
from mesh import Mesh

# Initialize with namespace and agent identity
m = Mesh(namespace="my-project", agent_id="my-agent")

# Store
result = m.learn(
    content="API rate limit is 100 req/min per user",
    memory_type="fact",
    tags=["api", "rate-limit"],
    confidence=1.0,
    ttl_days=90  # Will decay after 90 days
)

# Recall
results = m.recall("what is the API rate limit?", n=5)
for r in results:
    print(f"[{r['similarity']:.2f}] {r['content']}")

# Update
m.update(memory_id="abcd1234", content="API rate limit is 200 req/min per user")

# Forget
m.forget(memory_id="abcd1234")

# Context block
ctx = m.build_context(query="rate limiting", count=10, format="markdown")

# Patterns
patterns = m.get_patterns()
print(patterns["memory_gaps"])  # Topics agents ask about but can't find
```

---

## 9. Anti-Patterns to Avoid

| Anti-Pattern | Why It's Bad | Do This Instead |
| :--- | :--- | :--- |
| Never calling `mesh_recall` at task start | You repeat work other agents already did | Always recall before starting |
| Storing vague content like `"DB stuff"` | Useless for hybrid search | Be specific: `"PostgreSQL 16 on port 5432"` |
| Forgetting + re-learning instead of updating | Loses access count, history, and UUID | Use `mesh_update` to modify in-place |
| Storing secrets without `local_only=true` | Secrets leak via exports and context blocks | Always set `local_only=true` for credentials |
| Ignoring contradictions | Conflicting memories cause bad decisions | Review and resolve contradictions promptly |
| Storing ephemeral info without `ttl_days` | Stale IPs/URLs linger forever | Set appropriate TTL for temp info |
| Dumping entire files as one memory | Too large, poor recall quality | Extract atomic facts from files |
| Using the same namespace for all projects | Cross-project memory pollution | One namespace per project |

---

## 10. Troubleshooting

| Issue | Solution |
| :--- | :--- |
| `mesh-server` command not found | Ensure `pip install mesh-context-layer` completed and the Python Scripts dir is on PATH |
| MCP tools not appearing | Check your `mcp_config.json` — the `command` should be `mesh-server` or `python -m mesh.mcp_server` |
| Recall returns 0 results | Verify the namespace matches where you stored memories; try `mesh inspect` to see what's stored |
| ONNX model not loading | Run `mesh setup` to download it, or set `MESH_AUTO_APPROVE=1` and re-run |
| Stale memories appearing | Use `include_stale=false` in recall, or `mesh_update` to refresh TTL |
| Permission errors on `~/.mesh` | Run `mesh doctor` to diagnose; ensure the dir has 0700 and files have 0600 permissions |
| Daemon won't start | Check `mesh daemon status`; ensure port 7701 is free; check `~/.mesh/daemon.log` |

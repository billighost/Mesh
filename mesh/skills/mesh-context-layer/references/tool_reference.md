# Mesh MCP Tool Reference (v0.8.2)

Complete reference for all 17 Mesh Model Context Protocol (MCP) tools.

---

## 1. Session Brief & Work Log

### `mesh_project_brief`
Single-call session-start brief: loads namespace health, open goals and tasks, recently completed work, and key architecture facts within a token budget.
- `max_tokens` (int, default: 500): Token budget cap for the brief (approximated at 4 chars/token).
- `include_work_items` (bool, default: true): Include open tasks, goals, and recently completed items.
- `namespace` (string, optional): Namespace override (defaults to active project namespace).

### `mesh_log`
Generate a project changelog combining completed tasks, evidence, and architectural decisions over a time window.
- `since` (string, default: "7d"): Time window (e.g. "24h", "7d", "30d") or ISO timestamp.
- `format` (string, enum: ["markdown", "json"], default: "markdown"): Output format.
- `namespace` (string, optional): Namespace override.

---

## 2. Work Items & Task Lifecycle

### `mesh_task_create`
Create a first-class task or goal in Mesh to track work across sessions.
- `title` (string, required): Clear, concise title for the task or goal.
- `kind` (string, enum: ["task", "goal"], default: "task"): Whether high-level goal or actionable task.
- `parent_id` (string, optional): Parent goal ID or short ID if this is a subtask.
- `description` (string, optional): Detailed context or acceptance criteria.
- `priority` (string, enum: ["low", "medium", "high", "critical"], default: "medium"): Priority tier.
- `tags` (array of string, optional): Categorisation tags.
- `namespace` (string, optional): Namespace override.

### `mesh_task_update`
Update status, priority, or notes of an existing task or goal.
- `item_id` (string, required): UUID or short ID (e.g. 'task-1', 'goal-2') of the work item.
- `status` (string, enum: ["todo", "in_progress", "blocked", "done", "cancelled"], optional): New status.
- `priority` (string, enum: ["low", "medium", "high", "critical"], optional): Updated priority tier.
- `title` (string, optional): Updated title.
- `description` (string, optional): Updated description.
- `note` (string, optional): Reason for transition or blocker note.
- `namespace` (string, optional): Namespace override.

### `mesh_task_complete`
Mark a task or goal completed with a mandatory summary of changes and optional evidence.
- `item_id` (string, required): UUID or short ID of the work item.
- `summary` (string, required): Detailed summary of what was completed, decisions made, or changes made.
- `evidence` (array of string, optional): List of modified file paths, git commit SHAs, or links.
- `namespace` (string, optional): Namespace override.

### `mesh_task_list`
List tasks and goals in the namespace with deterministic priority and creation-date sorting.
- `status` (string, enum: ["all", "open", "closed", "todo", "in_progress", "blocked", "done", "cancelled"], default: "open"): Status filter.
- `kind` (string, enum: ["task", "goal"], optional): Filter by kind.
- `parent_id` (string, optional): Filter subtasks of a specific goal.
- `limit` (int, default: 20): Maximum items to return.
- `namespace` (string, optional): Namespace override.

---

## 3. Core Memory Operations

### `mesh_learn`
Store information in shared agent memory. Deduplicates identical facts and detects contradictions.
- `content` (string, required): The specific fact, decision, process, instruction, or secret pointer.
- `memory_type` (string, enum: ["fact", "preference", "context", "result", "instruction", "process", "decision", "error", "secret_ref", "general"], default: "fact"): Category of memory.
- `confidence` (number, 0.0-1.0, default: 1.0): Confidence score.
- `tags` (array of string, optional): Topic labels.
- `ttl_days` (number, optional): Days before confidence decays to 0.
- `local_only` (bool, default: false): Mark private to prevent export or context injection.
- `secret_ref` (string, optional): Secret locator URI (e.g. 'env://STRIPE_KEY', 'keyring://mesh/shared/STRIPE_KEY').
- `vault_secrets` (bool, default: false): Vault detected secrets into OS keyring and store a pointer.
- `namespace` (string, optional): Namespace override.

### `mesh_recall`
Search shared agent memory using 5-stage hybrid search (dense SIMD cosine + BM25/IDF + synonyms).
- `query` (string, required): Natural language query or keywords.
- `n` (int, 1-50, default: 5): Maximum primary results.
- `min_similarity` (number, 0.0-1.0, default: 0.0): Similarity floor.
- `min_confidence` (number, 0.0-1.0, default: 0.0): Confidence floor.
- `include_stale` (bool, default: true): Include decayed/stale memories.
- `memory_type` (string, optional): Filter by memory type.
- `tags` (array of string, optional): Filter by tags.
- `source_agent` (string, optional): Filter by author agent ID.
- `include_other_possible` (bool, default: true): Include peripheral related snippets.
- `max_other_possible` (int, 0-20, default: 5): Max peripheral snippets.
- `namespace` (string, optional): Namespace override.

### `mesh_update`
Update an existing memory in-place by ID without losing UUID, creation time, or access metrics.
- `memory_id` (string, required): UUID or short ID prefix.
- `content` (string, optional): New text content.
- `memory_type` (string, optional): Updated memory type.
- `confidence` (number, optional): Updated confidence score.
- `tags` (array of string, optional): Updated tags.
- `ttl_days` (number, optional): Reset decay timer.
- `clear_ttl` (bool, default: false): Make memory permanent.
- `local_only` (bool, optional): Updated privacy flag.
- `secret_ref` (string, optional): Updated secret locator URI.
- `namespace` (string, optional): Namespace override.

### `mesh_forget`
Delete a specific memory permanently by ID (scrubbing audit & conflict logs).
- `memory_id` (string, required): UUID or short ID prefix.
- `namespace` (string, optional): Namespace override.

### `mesh_approve`
Approve a quarantined memory by ID.
- `memory_id` (string, required): UUID or short ID prefix.
- `namespace` (string, optional): Namespace override.

---

## 4. Discovery & Analysis

### `mesh_context`
Generate a formatted context block of top project memories.
- `query` (string, optional): Topic query to focus context.
- `count` (int, default: 10): Maximum memories to include.
- `format` (string, enum: ["markdown", "plain", "xml", "json"], default: "markdown"): Output format.
- `min_confidence` (number, default: 0.3): Minimum confidence.
- `memory_type` (string, optional): Filter by memory type.
- `tags` (array of string, optional): Filter by tags.
- `namespace` (string, optional): Namespace override.

### `mesh_inspect`
List memories in the namespace sorted by most recently accessed.
- `limit` (int, default: 10, max: 200): Maximum results.
- `memory_type` (string, optional): Filter by type.
- `tag` (string, optional): Filter by tag.
- `quarantined_only` (bool, default: false): List quarantined items.
- `namespace` (string, optional): Namespace override.

### `mesh_patterns`
Analyse memory usage patterns: top topics, knowledge gaps, unused and stale memories.
- `namespace` (string, optional): Namespace override.

### `mesh_contradictions`
List contradictions between stored memories.
- `resolved` (bool, default: false): Include already-resolved contradictions.
- `namespace` (string, optional): Namespace override.

### `mesh_resolve_contradiction`
Mark a contradiction record as resolved.
- `contradiction_id` (string, required): UUID or short ID of contradiction.
- `namespace` (string, optional): Namespace override.

### `mesh_sync`
Store session turn summary and optionally ingest local agent logs (when permitted by env).
- `summary` (string, optional): Brief summary of turn.
- `include_history` (bool, default: false): Ingest past log tails.
- `quarantine` (bool, optional): Quarantine ingested items.
- `namespace` (string, optional): Namespace override.

# Mesh Usage Examples (v0.8.1b1)

Practical patterns for AI coding agents interacting with Mesh.

---

## 1. Storing Durable Information (`mesh_learn`)

### Architecture Decisions
```json
{
  "content": "API routing migrated from Flask to FastAPI; all new endpoints must use Pydantic models in app/schemas.py.",
  "memory_type": "decision",
  "tags": ["api", "architecture", "fastapi"],
  "confidence": 1.0
}
```

### Environment Facts & Ports
```json
{
  "content": "Local Postgres runs on port 5433 (not default 5432) to avoid collision with system daemon.",
  "memory_type": "fact",
  "tags": ["database", "postgres", "ports"],
  "confidence": 1.0
}
```

### Gotchas & Bug Fixes
```json
{
  "content": "Pytest fails with StarletteDeprecationWarning if httpx2 is not installed; do not pin starlette directly.",
  "memory_type": "error",
  "tags": ["testing", "pytest", "dependencies"],
  "confidence": 0.95
}
```

### Non-Sensitive Secret Pointer
```json
{
  "content": "Stripe test webhook secret is configured in 1Password vault 'Dev' under item 'Stripe Webhook'.",
  "memory_type": "secret_ref",
  "secret_ref": "op://Dev/Stripe Webhook/credential",
  "tags": ["stripe", "secrets", "auth"],
  "local_only": true
}
```

---

## 2. Updating Without Duplication (`mesh_recall` -> `mesh_update`)

When information changes (e.g., port changed from 5433 to 5434):
1. **Search existing memory:**
   `mesh_recall(query="Postgres port")`
   Result: `[{"id": "mem-a1b2", "content": "Local Postgres runs on port 5433..."}]`
2. **Update in-place:**
   ```json
   {
     "memory_id": "mem-a1b2",
     "content": "Local Postgres runs on port 5434 after docker-compose update.",
     "tags": ["database", "postgres", "ports"]
   }
   ```
   *Result:* Memory `mem-a1b2` is updated. No duplicate is created, access counts are preserved, and contradiction detection is avoided.

---

## 3. Work Items Lifecycle (`task_create` -> `task_complete`)

### Step 1: Create Goal
```json
// mesh_task_create
{
  "title": "Migrate database schema to v3 with SQLite WAL mode",
  "kind": "goal",
  "priority": "high",
  "tags": ["database", "schema", "wal"]
}
// Returns: {"short_id": "goal-1"}
```

### Step 2: Break into Actionable Tasks
```json
// mesh_task_create
{
  "title": "Add work_items and work_events tables in store.py",
  "kind": "task",
  "parent_id": "goal-1",
  "priority": "high",
  "tags": ["database", "schema"]
}
// Returns: {"short_id": "task-2"}
```

### Step 3: Transition Status as Work Starts
```json
// mesh_task_update
{
  "item_id": "task-2",
  "status": "in_progress",
  "note": "Working on SQLite DDL migration function."
}
```

### Step 4: Complete with Mandatory Summary & Evidence
```json
// mesh_task_complete
{
  "item_id": "task-2",
  "summary": "Implemented schema v3 migration with automatic backup. Added work_items and work_events tables with indices.",
  "evidence": ["mesh/store.py", "tests/test_work_items.py", "git:59a96ce"]
}
```

---

## 4. Generating Project Changelog (`mesh_log`)

When the user asks *"What did we do today?"* or *"Summarize recent work"*:
```json
// mesh_log
{
  "since": "24h",
  "format": "markdown"
}
```
*Output:*
```markdown
# Project Changelog (last 24h)

## Completed Tasks
- **Add work_items and work_events tables in store.py** (task-2)
  *Completed by claude-code at 2026-10-19 09:30*
  Summary: Implemented schema v3 migration with automatic backup. Added work_items and work_events tables with indices.
  Evidence: mesh/store.py, tests/test_work_items.py, git:59a96ce

## Key Decisions
- [decision] Schema bumped to v3 with automatic backup before migration.
```

---

## 5. End-of-Session Handoff

Before closing a session, write a checkpoint so the next agent (or session) resumes immediately:
```json
// mesh_learn
{
  "content": "Handoff: Completed schema v3 migration and work items data layer. Next agent should implement installer and check_skill.py validation script.",
  "memory_type": "context",
  "tags": ["handoff", "status", "next-steps"],
  "confidence": 1.0
}
```

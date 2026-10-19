# Mesh Release Notes — v0.8.0

**Release Date:** 2026-10-19  
**Package:** `mesh-context-layer`  
**License:** MIT  

---

## Highlights

Mesh v0.8.0 is a major milestone introducing first-class agent work tracking, token-budgeted project briefs, a unified multi-agent installer, hardened security gates, and reproducible scale benchmarks.

### 1. First-Class Work Items (`work_items` & `work_events`)
- Deterministic tracking for goals and granular tasks stored in SQLite with WAL mode.
- Tasks are decoupled from vector decay, memory deduplication, and contradiction resolution.
- Schema bumped to version 3 with automated pre-migration SQLite backups (`.backup-v0.7.2-<timestamp>`) and forward-compatibility guards.
- New MCP tools: `mesh_task_create`, `mesh_task_update`, `mesh_task_complete`, `mesh_task_list`.
- Mandatory completion summary and evidence tracking (file paths, commit SHAs, URLs) on `mesh_task_complete`.

### 2. Session-Start Brief & Project Changelog
- `mesh_project_brief` MCP tool and `mesh brief` CLI: single-call session primer loading namespace health, open tasks, recent completions, and core facts within an exact token budget (p50 latency: < 7ms).
- `mesh_log` MCP tool and `mesh log` CLI: chronological changelog combining completed tasks, evidence, and architectural decisions over time windows (`24h`, `7d`, `30d`).

### 3. Unified Multi-Agent Installer (`mesh install` / `mesh uninstall`)
- One-command configuration for Claude Code, Claude Desktop, Antigravity IDE & CLI, Gemini CLI, Cursor, Windsurf, OpenCode, and OpenAI Codex.
- Generates atomic backups (`.bak.<timestamp>`), dry-run preview with line diffs, project vs user scopes, and byte-exact clean uninstall.
- Injects a single source of truth rules block (<= 12 lines) into agent instructions between idempotent markers.

### 4. Benchmarks & Scale Suite
- Empirical latency measurements across all 17 operations documented in `benchmarks/RESULTS.md`.
- Scale benchmarks across 500, 1k, 5k, and 10k memories + work items.
- Project brief and task queries execute under 10ms at scale.

### 5. Security & Threat Model
- Added `SECURITY.md` referencing GitHub Private Vulnerability Reporting and `bb2010ng@gmail.com`.
- Added `THREAT_MODEL.md` covering prompt injection defenses, secret locators (`secret_ref`), and execution boundaries.
- Clean packaging guaranteed: 0 leaked media files, automated SBOM (`dist/sbom.json`) and provenance.

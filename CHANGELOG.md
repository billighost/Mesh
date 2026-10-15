# Changelog

All notable changes to **Mesh** (`mesh-context-layer`) are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.5.3] - 2026-10-15

### Added
- **Instant Background Daemon Activation (`mesh daemon install-startup`)**:
  - `install-startup` now automatically triggers `start_daemon()` immediately upon registration, eliminating the need for a separate manual `mesh daemon start` command.
- **New Daemon Management & Residency Endpoints**:
  - Added `GET /engine` and `POST /unload` HTTP REST endpoints to inspect neural model status and immediately free model weights from RAM on demand.
  - Added the `mesh-daemon` CLI command supporting `start`, `stop`, `restart`, `status`, `unload`, `install-startup`, and `remove-startup`.

### Performance & Optimizations
- **3-Tier Lightweight ONNX / FastEmbed Engine**:
  - Integrated `fastembed` as the primary ONNX runtime embedding engine, cutting cold startup times down to ~120 ms and reducing idle RAM to ~20 MB while producing 100% compatible 384-dimensional `all-MiniLM-L6-v2` vectors.
- **Automatic Idle Model Unloading**:
  - Added a background inactivity watchdog (`MESH_IDLE_UNLOAD_SECONDS=180`) that unloads neural weights from RAM after 3 minutes of inactivity while keeping the 4,096-entry LRU embedding cache hot for instant `<0.01 ms` query resolution.
- **Cross-Process SQLite WAL Cache Sync**:
  - Replaced single-file stat tracking with a composite `(db_mtime_ns, db_size, wal_mtime_ns, wal_size)` stamp, ensuring writes from external processes (CLI, Dashboard, or HTTP daemon) are detected instantly in $O(1)$ time.

### Fixed & Hardened
- **Dashboard Syntax & UI Fixes**:
  - Corrected `DASHBOARD_HTML` to a raw string (`r"""..."""`), fixing the fatal JavaScript `Uncaught SyntaxError: Invalid regular expression: missing /` caused by backslash corruption in `escAttr()`.
  - Restored all missing action button icons (`↻`, `✎`, `✕`), added the `general` memory type chip and `Deletes` audit filter chip, and fixed active namespace resurrection bugs.
- **MCP Stdio Protocol Safety**:
  - Implemented reference-counted thread-safe stdout-to-stderr redirection (`_safe_redirect_stdout_to_stderr()`), preventing rogue print statements or background worker threads from corrupting JSON-RPC stdio streams with Claude Desktop and Cursor.
- **Audit Logging Precision**:
  - Guarded `log_forget()` and `POST /forget` so false deletion events are never written to `audit_log` when a requested memory ID is not found.

---

## [0.5.2] - 2026-10-15

### Added
- Windowless background process spawning (`CREATE_NO_WINDOW` / `pythonw.exe`) on Windows for silent background daemon execution.

---

## [0.5.1] - 2026-10-15

### Added
- **Comprehensive Showcase & Benchmark Suite (`examples/full_showcase.py`)**: 11-stage end-to-end verification covering vector warmup, deduplication, contradiction detection, TTL decay, hybrid search, context generation, export/import, namespace management, audit logging, passive capture, MCP tools, and HTTP REST endpoints[cite: 1].
- **Cross-Platform Console Compatibility**: Reconfigured output streams to UTF-8 with fallback replacement to ensure resilient CLI operations on Windows (CP1252) environments[cite: 1].

### Performance & Optimizations
- **High-Speed Batch Import Pipeline (`mesh.io:import_namespace`)**:
  - Implemented `upsert_batch()` writing all imported memories in a single atomic SQLite WAL transaction[cite: 1].
  - Batch-computed missing embeddings in a single forward pass with LRU cache integration[cite: 1].
  - Pre-resolved existing IDs in $O(1)$ from the in-memory vector cache, eliminating N+1 database queries during bulk imports[cite: 1].
- **Single-Query Export Embedding Resolution (`mesh.io:export_namespace`)**:
  - Replaced individual vector lookups with batch ID resolution for missing embeddings[cite: 1].
- **Single-Pass Context Builder**:
  - Streamlined `/context` HTTP endpoint and `mesh_context` MCP tool to retrieve and format system prompt context in a single optimized pass with full memory type and tag filtering support[cite: 1].
- **Lockless Registry Fast-Path**:
  - Added fast-path read check in `mesh.http_server:get_mesh()` before acquiring registry locks[cite: 1].

### Fixed & Hardened
- **MCP Stdio Protocol Safety**:
  - Implemented thread-safe `_safe_redirect_stdout_to_stderr()` context manager in `mesh/mcp_server.py`, preventing unexpected stdout prints from corrupting JSON-RPC stdio communication with Claude Desktop, Cursor, and Windsurf[cite: 1].
- **Documentation Synchronization**:
  - Fully updated `ARCHITECTURE.md` and `DEVELOPER_GUIDE.md` to reflect the unified SQLite WAL + NumPy SIMD storage engine, test isolation patterns, and active configuration flags[cite: 1].

---

## [0.5.0] - 2026-10-15

### Major Architecture Evolution (Zero External DB Dependencies)
- **ChromaDB Removal & Unified SQLite Architecture**:
  - Completely replaced the legacy dual-database architecture (ChromaDB + SQLite) with a single, unified SQLite WAL database (`mesh_<namespace>.db`) storing metadata, full-text FTS5 tables, and normalized `float32` vector BLOBs together[cite: 1].
  - Eliminates multi-process SQLite/Chroma synchronization issues, reduces memory footprint, and makes namespace backups or renames atomic single-file operations[cite: 1].
- **3-Stage Hybrid Search Engine**:
  - **Dense Cosine Similarity**: In-memory NumPy SIMD matrix multiplication (`cache.matrix @ q_vec`) executing vector search in `<0.1 ms`[cite: 1].
  - **Lexical BM25 Ranking**: SQLite FTS5 full-text indexing for exact keyword matches[cite: 1].
  - **Exact Token Overlap Boost**: Heuristic boosting for identifiers, paths, ports, and configuration keys[cite: 1].
- **In-Memory Vector Cache (`_VectorCacheEntry`)**:
  - Automatically loads and synchronizes namespace embeddings in memory for instant query responses[cite: 1].

### Intelligence & Memory Quality
- **Automatic Deduplication Engine**:
  - Near-identical facts ($\ge 0.985$ cosine similarity) are reinforced in-place rather than creating duplicate records, updating timestamps, access counts, and merging tags[cite: 1].
- **Contradiction Detection & Tracking**:
  - Conflicting memories ($\ge 0.82$ similarity with numeric shifts or negation polarity differences) are stored safely while recording a structured conflict entry for human/agent review without silent data loss[cite: 1].
- **Time-to-Live (TTL) & Linear Confidence Decay**:
  - Time-sensitive memories decay linearly towards staleness (`STALE_THRESHOLD = 0.3`) using `ttl_days` and `decay_start_at`[cite: 1].
- **In-Place Memory Updates (`mesh.update`)**:
  - Modify existing memory content and tags while preserving its UUID, creation date, and access history[cite: 1].

### Tooling & Interfaces
- **10 Full MCP Server Tools**:
  - `mesh_learn`, `mesh_recall`, `mesh_update`, `mesh_forget`, `mesh_inspect`, `mesh_context`, `mesh_patterns`, `mesh_contradictions`, `mesh_resolve_contradiction`, `mesh_sync`[cite: 1].
- **Modern Web Dashboard (`mesh-dashboard`)**:
  - Zero-build single-file web interface (`http://localhost:7433`) featuring dark/light mode, real-time hybrid search, quick modal shortcuts (`/` focus search, `n` new memory, `Esc`), contradiction manager, memory pattern analysis, and live audit viewer[cite: 1].
- **Passive Log Capture (`mesh-capture`)**:
  - Extracts durable learnings and facts from chat transcripts, terminal outputs, and build logs using Anthropic, OpenAI, Groq, Ollama, or local heuristic extraction[cite: 1].
- **Session Digests (`mesh-digest`)**:
  - Generate automated Markdown summaries of newly stored knowledge, patterns, and contradictions over configurable time horizons[cite: 1].
- **Isolated Audit Trail**:
  - Independent WAL database (`audit_<namespace>.db`) for high-frequency logging of memory reads, writes, and updates without database lock contention[cite: 1].

---

## [0.4.4] - 2026-05-13

### Changed
- Restructured project layout into standard `src`/module layout with `hatchling` build backend[cite: 1].
- Resolved Windows CP1252 terminal encoding exceptions during CLI execution[cite: 1].
- Added comprehensive unit and integration test suites (`tests/test_memory.py`, `tests/test_v03.py`, `tests/test_v04.py`)[cite: 1].

---

## [0.3.0] - 2026-05-13

### Added
- **Privacy Flags (`local_only=True`)**: Mark sensitive memories to guarantee they are never exported or synced across shared instances[cite: 1].
- **Namespace Export & Import (`mesh-export`, `mesh-import`)**: JSON-based backup and migration supporting `skip_existing` and `overwrite` strategies[cite: 1].
- **Namespace Management CLI (`mesh-namespaces`)**: Commands to inspect, list, calculate statistics, rename, and delete namespaces[cite: 1].
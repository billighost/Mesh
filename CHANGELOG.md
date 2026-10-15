# Changelog

All notable changes to **Mesh** (`mesh-context-layer`) are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.5.1] - 2026-10-15

### Added
- **Comprehensive Showcase & Benchmark Suite (`examples/full_showcase.py`)**: 11-stage end-to-end verification covering vector warmup, deduplication, contradiction detection, TTL decay, hybrid search, context generation, export/import, namespace management, audit logging, passive capture, MCP tools, and HTTP REST endpoints.
- **Cross-Platform Console Compatibility**: Reconfigured output streams to UTF-8 with fallback replacement to ensure resilient CLI operations on Windows (CP1252) environments.

### Performance & Optimizations
- **High-Speed Batch Import Pipeline (`mesh.io:import_namespace`)**:
  - Implemented `upsert_batch()` writing all imported memories in a single atomic SQLite WAL transaction.
  - Batch-computed missing embeddings in a single forward pass with LRU cache integration.
  - Pre-resolved existing IDs in $O(1)$ from the in-memory vector cache, eliminating N+1 database queries during bulk imports.
- **Single-Query Export Embedding Resolution (`mesh.io:export_namespace`)**:
  - Replaced individual vector lookups with batch ID resolution for missing embeddings.
- **Single-Pass Context Builder**:
  - Streamlined `/context` HTTP endpoint and `mesh_context` MCP tool to retrieve and format system prompt context in a single optimized pass with full memory type and tag filtering support.
- **Lockless Registry Fast-Path**:
  - Added fast-path read check in `mesh.http_server:get_mesh()` before acquiring registry locks.

### Fixed & Hardened
- **MCP Stdio Protocol Safety**:
  - Implemented thread-safe `_safe_redirect_stdout_to_stderr()` context manager in `mesh/mcp_server.py`, preventing unexpected stdout prints from corrupting JSON-RPC stdio communication with Claude Desktop, Cursor, and Windsurf.
- **Documentation Synchronization**:
  - Fully updated `ARCHITECTURE.md` and `DEVELOPER_GUIDE.md` to reflect the unified SQLite WAL + NumPy SIMD storage engine, test isolation patterns, and active configuration flags.

---

## [0.5.0] - 2026-10-15

### Major Architecture Evolution (Zero External DB Dependencies)
- **ChromaDB Removal & Unified SQLite Architecture**:
  - Completely replaced the legacy dual-database architecture (ChromaDB + SQLite) with a single, unified SQLite WAL database (`mesh_<namespace>.db`) storing metadata, full-text FTS5 tables, and normalized `float32` vector BLOBs together.
  - Eliminates multi-process SQLite/Chroma synchronization issues, reduces memory footprint, and makes namespace backups or renames atomic single-file operations.
- **3-Stage Hybrid Search Engine**:
  - **Dense Cosine Similarity**: In-memory NumPy SIMD matrix multiplication (`cache.matrix @ q_vec`) executing vector search in `<0.1 ms`.
  - **Lexical BM25 Ranking**: SQLite FTS5 full-text indexing for exact keyword matches.
  - **Exact Token Overlap Boost**: Heuristic boosting for identifiers, paths, ports, and configuration keys.
- **In-Memory Vector Cache (`_VectorCacheEntry`)**:
  - Automatically loads and synchronizes namespace embeddings in memory for instant query responses.

### Intelligence & Memory Quality
- **Automatic Deduplication Engine**:
  - Near-identical facts ($\ge 0.985$ cosine similarity) are reinforced in-place rather than creating duplicate records, updating timestamps, access counts, and merging tags.
- **Contradiction Detection & Tracking**:
  - Conflicting memories ($\ge 0.82$ similarity with numeric shifts or negation polarity differences) are stored safely while recording a structured conflict entry for human/agent review without silent data loss.
- **Time-to-Live (TTL) & Linear Confidence Decay**:
  - Time-sensitive memories decay linearly towards staleness (`STALE_THRESHOLD = 0.3`) using `ttl_days` and `decay_start_at`.
- **In-Place Memory Updates (`mesh.update`)**:
  - Modify existing memory content and tags while preserving its UUID, creation date, and access history.

### Tooling & Interfaces
- **10 Full MCP Server Tools**:
  - `mesh_learn`, `mesh_recall`, `mesh_update`, `mesh_forget`, `mesh_inspect`, `mesh_context`, `mesh_patterns`, `mesh_contradictions`, `mesh_resolve_contradiction`, `mesh_sync`.
- **Modern Web Dashboard (`mesh-dashboard`)**:
  - Zero-build single-file web interface (`http://localhost:7433`) featuring dark/light mode, real-time hybrid search, quick modal shortcuts (`/` focus search, `n` new memory, `Esc`), contradiction manager, memory pattern analysis, and live audit viewer.
- **Passive Log Capture (`mesh-capture`)**:
  - Extracts durable learnings and facts from chat transcripts, terminal outputs, and build logs using Anthropic, OpenAI, Groq, Ollama, or local heuristic extraction.
- **Session Digests (`mesh-digest`)**:
  - Generate automated Markdown summaries of newly stored knowledge, patterns, and contradictions over configurable time horizons.
- **Isolated Audit Trail**:
  - Independent WAL database (`audit_<namespace>.db`) for high-frequency logging of memory reads, writes, and updates without database lock contention.

---

## [0.4.4] - 2026-05-13

### Changed
- Restructured project layout into standard `src`/module layout with `hatchling` build backend.
- Resolved Windows CP1252 terminal encoding exceptions during CLI execution.
- Added comprehensive unit and integration test suites (`tests/test_memory.py`, `tests/test_v03.py`, `tests/test_v04.py`).

---

## [0.3.0] - 2026-05-13

### Added
- **Privacy Flags (`local_only=True`)**: Mark sensitive memories to guarantee they are never exported or synced across shared instances.
- **Namespace Export & Import (`mesh-export`, `mesh-import`)**: JSON-based backup and migration supporting `skip_existing` and `overwrite` strategies.
- **Namespace Management CLI (`mesh-namespaces`)**: Commands to inspect, list, calculate statistics, rename, and delete namespaces.

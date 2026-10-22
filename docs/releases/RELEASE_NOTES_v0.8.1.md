# Mesh v0.8.1

**Released 2026-10-22** · [PyPI](https://pypi.org/project/mesh-context-layer/0.8.1/) · [Full changelog](https://github.com/billighost/mesh/blob/main/CHANGELOG.md#081---2026-10-22-beta)

Mesh v0.8.1 is a comprehensive hardening, stability, and documentation refresh release consolidating findings from three independent black-box evaluation runs (Run A, Run B, and Run C) and Run T field-test audits. Every fix is backed by dedicated regression tests (323 tests total across Python 3.10–3.13 on Ubuntu, macOS, and Windows).

## Highlights

- **Thread-Safe SQLite Connection Pooling (C-001):** Re-entrant mutex synchronization on pooled SQLite connections preventing `InterfaceError` cursor collisions and deferred retirement during in-flight operations (`tests/test_thread_safety_c001.py`).
- **Ingestion Privacy Transparency (C-002):** `Mesh.learn()` and all entry points now accurately surface the effective stored `local_only` state when secrets or policies trigger automatic quarantining (`tests/test_learn_local_only_c002.py`).
- **Agent Lifecycle Hooks (T-003):** Opt-in `--hooks` flag on `mesh install` automates session-start project brief injection (`mesh brief`) on supported agents (Claude Code, Antigravity) with atomic backup and rollback support.
- **Enhanced Secret Scanning & Task Quarantine (A-004, B-005, T-002):** Expanded detection patterns to catch arbitrary prefix tokens and locker/pass codes; task updates and completions containing credentials are now quarantined automatically.
- **Robust Multi-Platform Skill Installation (T-001):** Implemented exponential backoff and garbage collection retry loops in `_safe_replace_dir` to eliminate transient Windows file lock contention during skill updates.
- **Complete End-User Documentation:** Comprehensive overhaul of `DOCS.md`, `ARCHITECTURE.md`, `CONTRIBUTING.md`, `DEVELOPER_GUIDE.md`, and `README.md`, including an isolated public field-test archive under `docs/field-test/`.

## Upgrading

```bash
pip install --upgrade mesh-context-layer
mesh install --force
```

Restart your MCP clients (Claude Desktop, Cursor, Antigravity, etc.) or daemons afterwards. Existing databases and configurations remain fully backward-compatible.

## Verification

- **323 automated tests pass** on Ubuntu, macOS, and Windows across Python 3.10, 3.11, 3.12, and 3.13.
- Full documentation consistency and skill limit verification pass (`scripts/check_docs.py` and `scripts/check_skill.py`).

**Full diff:** https://github.com/billighost/mesh/compare/v0.8.0...v0.8.1

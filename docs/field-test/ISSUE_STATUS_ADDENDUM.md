# Field Test Issue Status — Run C Addendum (v0.8.1b2)

**Branch:** `fix/retest-c001-c002`  
**Base Version:** `0.8.1b1` | **Target Version:** `0.8.1b2` (Beta)  
**Evaluation Source:** Run C — `C:\Users\bb201\Documents\bun2\report\issues.md`  
**Scope:** the two issues raised by Run C, plus the two optional diagnostic items approved at the plan gate.

This addendum is written next to the existing Run A/Run B record (`ISSUE_STATUS.md`); the earlier report and its raw data are unchanged. The nine fixes verified by Run C (the Run A/Run B issues) were not modified.

---

## 1. Summary

| Issue | Severity | Subsystem | Status | Commit | Regression test |
|---|---|---|---|---|---|
| **C-001** — SQLite connection shared across threads causes `SQLITE_MISUSE` | Medium | Store / Concurrency | **RESOLVED** | `583f8d2` | `tests/test_thread_safety_c001.py` (8 tests) |
| **C-002** — `Mesh.learn()` payload omits `local_only` | Low | SDK / API | **RESOLVED** | `4da79f8` | `tests/test_learn_local_only_c002.py` (10 tests) |
| Optional — mocked model-download fallback test | — | Model Download | **ADDED** | `7312bd0` | `tests/test_model_download_fallback.py` (9 tests) |
| Optional — doctor project-vs-user MCP precedence | — | CLI / Doctor | **ADDED** | `79ca896` | `tests/test_doctor_scope_precedence.py` (6 tests) |

Deferred at the plan gate: bounded Windows SQLite lock retry (not implemented in this release).

---

## 2. C-001 — Thread-safe pooled SQLite connections

- **Reported By:** Run C (`C-001`)
- **Severity:** Medium (correctness under in-process concurrency)
- **Symptom:** With 4 writer and 4 reader threads in one process, a reader raised `sqlite3.ProgrammingError: bad parameter or other API misuse`. During internal reproduction, `/brief` returned HTTP 500 with the same `InterfaceError`, a reader raised `IndexError: tuple index out of range` from `dict(sqlite3.Row)` conversion, and pool eviction raised `sqlite3.ProgrammingError: Cannot operate on a closed database`.
- **Root Cause:** `mesh/store.py:_connect()` caches one `_PooledConnection` per database path in `_CONNECTION_POOL`. `check_same_thread=False` permits the connection object to cross threads, but SQLite cursor/statement and row-factory state are not safe for simultaneous use. Eviction could also close a connection while another thread was inside `with self._connect()`.
- **Resolution (`583f8d2`):**
  1. Each `_PooledConnection` owns a re-entrant lock (`_mesh_lock`) that is held for the whole `with conn:` block and around direct `conn.execute(...)` probes (`PRAGMA data_version`, content-version reads). Nested checkouts on the same thread remain re-entrant.
  2. A separate lifecycle lock (`_mesh_state_lock`) guards a checkout reference count and a retired/closed state, so `retire()` never waits for in-flight users and the physical `sqlite3.Connection.close()` runs only when the last checkout leaves.
  3. `_connect()` now returns a `_ConnectionCheckout` context manager; registration and checkout of a newly created connection happen inside `_CACHE_LOCK`, closing the window where another thread could evict a not-yet-checked-out connection.
  4. Evicted connections are finalized after the pool lock is released.
- **Chosen design:** the narrow per-connection lock plus lifecycle reference count (plan design T), not a process-wide global lock. The cost is measured in §4.
- **Regression tests:** `tests/test_thread_safety_c001.py` — serialized concurrent context use (peak concurrency asserted 1), same-thread re-entry, deferred close while in use, SDK writers/readers, 32 concurrent ASGI HTTP requests, 16 concurrent MCP tool calls, 4-process namespace sharing (`tests/c001_mp_worker.py`), and a pool-eviction race with `MESH_MAX_POOL_SIZE=2`.
- **Pre-fix proof:** 5 of the 8 tests failed on the pre-fix tree (concurrent context use, SDK stress, HTTP stress, MCP stress, eviction); 8 of 8 pass after the fix (25.94 s).

---

## 3. C-002 — `Mesh.learn()` reports the stored privacy flag

- **Reported By:** Run C (`C-002`)
- **Severity:** Low (API ergonomics; privacy state was stored correctly)
- **Symptom:** The documented re-test assertion `res["local_only"] is True` failed. The row was persisted as `local_only=1`, but callers could only infer this from `warnings` or a second query.
- **Root Cause:** `MeshStore.upsert()` returned `None`, and `Mesh.learn()` built its result without a privacy field. The deduplication return path had the same gap. No surface reported the value that was actually stored after `guard_ingest` forced redaction.
- **Resolution (`4da79f8`):**
  1. `MeshStore.upsert(...) -> bool` returns the effective `local_only` value persisted after the ingestion guards, audit scrub, and any forced-private escalation.
  2. `Mesh.learn()` publishes `"local_only"` on both the insert path (`stored_local_only`) and the deduplication path (`stored_local_only` from the updated row), and passes the stored value to `log_learn`.
  3. MCP `mesh_learn`, HTTP `/learn`, dashboard `/api/memories`, and CLI `mesh add` report the stored value rather than the caller's requested flag. `allow_redacted_public` on a trusted-local caller correctly reports `False` while the stored row is public and redacted.
- **Semantics:** the contract is *stored value after guards*, not *requested value*. A secret asked for `local_only=False` returns `True`; a redacted trusted-local public write returns `False`.
- **Regression tests:** `tests/test_learn_local_only_c002.py` — public, private, secret-requested-public, trusted-local force-public, dedup private/public, HTTP, dashboard, MCP and CLI. Every test reads the SQLite row that was written, rather than trusting the response alone.
- **Pre-fix proof:** all 10 tests failed on the pre-fix tree (9 with `KeyError: 'local_only'`, 1 exercising the CLI path); 10 of 10 pass after the fix.

---

## 4. Concurrency cost measurement (C-001)

Both trees were benchmarked with the same script on an otherwise idle machine, fresh `MESH_HOME` per run, no daemon, no model download, no network. Fixed tree: this branch. Baseline: `baseline-wt` at `3973bfe` (pre-fix). Order: fixed, baseline, baseline, fixed (ABBA); ranges across both runs of each tree.

| Workload | Fixed (0.8.1b2) | Baseline (3973bfe) |
|---|---|---|
| `learn` median | 50.6–50.8 ms | 51.0–56.0 ms |
| `recall` median | 25.1–25.3 ms | 25.1–28.0 ms |
| `project_brief` median | 2.69–2.76 ms | 2.66–2.92 ms |
| 8 readers × 40 iterations × 3 ops | 147.8 and 130.1 ops/s (median 139.0), 0 errors | 144.0 and 147.6 ops/s (median 145.8), 0 errors |
| 4 processes × 25 items (task create/update/complete + learn) | 7.652 s, 0 errors, 100/100 stored and completed | 8.005 s, 0 errors, 100/100 |

The per-connection lock adds no measurable single-thread cost (medians overlap run-to-run). Concurrent reader throughput on an idle machine is within run-to-run noise: the fixed tree's two runs bracket the baseline's two runs (fixed max 147.8 > baseline max 147.6; fixed min 130.1 < baseline min 144.0). The baseline number is produced by unsafe concurrent use of one connection, which is exactly the defect; it cannot be treated as a performance target. The multi-process workload, which never shared a connection, is unchanged within noise.

---

## 5. Optional items

### 5.1 Mocked ONNX model-download fallback (`7312bd0`)

No production code changed. `tests/test_model_download_fallback.py` mocks every `httpx` request and proves:

- HTTP error, connection error, truncation, and checksum mismatch on the primary mirror fall through to the pinned `hf-mirror.com` secondary;
- a payload that fails SHA-256 verification on every mirror is never installed (fail-closed, temp files removed);
- an already-verified file skips the network entirely;
- the background worker installs a verified model+tokenizer pair and only then switches the backend to `onnx`;
- the mirror lists remain HTTPS-only, pinned to one Hugging Face revision, and contain only `huggingface.co` and `hf-mirror.com` — no new network destination was added.

### 5.2 Doctor project-vs-user MCP precedence (`79ca896`)

`mesh doctor` gains a read-only check: when a project-scope MCP config declares the `mesh` server, it reports either `project takes precedence over user` (both scopes declare Mesh) or `project config active` (project only). Configs without a Mesh entry and unreadable JSON are ignored. The test suite asserts both files are byte-identical before and after doctor runs.

---

## 6. Verification

- **Full suite:** `317 passed` (`pytest -q`, venv, 288.53 s).
- **Earlier-fix regression:** `tests/test_agent_scenarios.py tests/test_installer.py tests/test_memory.py tests/test_security_audit_v08.py tests/test_work_items.py` → `91 passed` in 68.21 s.
- **`ruff check mesh tests`:** PASSED.
- **`mypy mesh`:** 85 errors, byte-identical error set to the pre-fix tree (pre-existing; no new errors).
- **`scripts/check_skill.py`:** PASSED.
- **`scripts/check_docs.py`:** PASSED after the version-stamp update.
- **Build:** `mesh_context_layer-0.8.1b2-py3-none-any.whl` / `mesh_context_layer-0.8.1b2.tar.gz`, `twine check` PASSED (see `RETEST_PLAN_T.md` for the exact `dist/` path and SHA-256).

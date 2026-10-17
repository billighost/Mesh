# Mesh v0.6.3

**Released 2026-10-18** · [PyPI](https://pypi.org/project/mesh-context-layer/0.6.3/) · [Full changelog](https://github.com/billighost/mesh/blob/main/CHANGELOG.md#063---2026-10-18)

0.6.3 is a hardening release. It closes privacy leaks found in a full code review, makes concurrent writes safe, tightens retrieval precision, and fixes a long list of operational paper cuts. Most of the work is invisible when everything is running correctly — which is the point — but a handful of defaults changed for security reasons, so read **Upgrading** before you install.

## Highlights

### Privacy

- **`patterns` no longer exposes private memories.** `unused_memories` and `stale_memories` exclude `local_only` memories by default, return a whitelist of fields (no embeddings), and leave private recall out of topic and gap analysis. Opt back in with `include_private`.
- **Credentials are caught on every ingestion path.** `mesh_learn`, HTTP `/learn`, the dashboard, CLI `mesh add`, and imports now run the same poison-attempt and credential checks as `mesh_sync`. Detected credentials are redacted and forced `local_only`; prompt-injection attempts are rejected.
- **Untrusted content is labelled.** `mesh_recall`, `mesh_inspect`, `mesh_patterns`, and `mesh_sync` responses carry `untrusted_reference_data: true` plus a notice.
- **Agent-log sync is opt-in.** Cursor, Claude, and Antigravity logs are only read when `MESH_ALLOW_LOG_SYNC=1` or `MESH_ENABLE_AUTO_SYNC=1`.

### Trust boundary

- **Daemon identity is verified before the token is sent.** The CLI, MCP proxy, and daemon control commands complete an HMAC challenge-response against `/identity` first, so a process squatting the port can no longer harvest tokens or serve forged data. Token comparisons are constant-time.
- **`/health` no longer leaks internals.** It returns only `status` and `version`; process and engine details moved behind authentication.
- **Non-loopback binds require an allowlist.** The HTTP server and dashboard refuse a non-loopback interface unless `MESH_ALLOWED_HOSTS` is set.
- **Context output is sanitized.** Unicode is NFKC-normalized and zero-width characters are stripped before breakout-tag removal; the JSON context format returns a whitelist of fields.

### Correctness under load

- **`learn()` is atomic.** A per-namespace write lock (in-process plus cross-process file lock) and `BEGIN IMMEDIATE` transactions prevent duplicates and missed contradictions when several agents write at once. Updates, deletes, imports, and re-embedding share the lock.
- **Cross-process cache thrash is gone.** Vector-cache validity follows a `content_version` counter that telemetry writes do not bump, and recall telemetry is buffered and flushed in batches.
- **Filters are applied before the top-k cut.** A rare memory type can no longer return nothing in a large namespace.
- **Relevance matching is stricter.** Substring matching was replaced with whole-token or same-stem matching, so `port` no longer matches `report`, the prefix fallback is tighter (`pass` no longer matches `password`), and synonym-only matches get a reduced weight.

### Operations

- **No more phantom namespaces.** Read-only paths (CLI reads, MCP read tools, HTTP read routes, `warmup_model`) no longer create database files; they return `did_you_mean` suggestions instead.
- **Model bootstrap is visible.** Checksum failures, skipped downloads, and `hash-384` fallbacks are logged and recorded in `~/.mesh/model_status.json`, then surfaced by `mesh doctor`, `mesh status`, and MCP metadata (`embedding_degraded`).
- **Errors are honest.** MCP tool failures return `isError=true`; deleting a missing namespace raises `NamespaceNotFoundError`; read commands fall back to local execution when the daemon times out while mutating commands report an unknown outcome instead of silently retrying.

## Upgrading

```bash
pip install --upgrade mesh-context-layer
```

Restart any long-running `mesh-server`, `mesh-http`, or MCP client so the new code loads.

### Behavior changes to know about

1. `patterns` hides private memories by default. Pass `include_private=True` for the old view.
2. `mesh_sync` does not read agent logs unless `MESH_ALLOW_LOG_SYNC=1` or `MESH_ENABLE_AUTO_SYNC=1`.
3. Ingestion redacts detected credentials and rejects prompt-injection attempts.
4. Binding HTTP or the dashboard to a non-loopback address requires `MESH_ALLOWED_HOSTS`.
5. `GET /health` returns only `status` and `version`.
6. Unknown `merge_strategy` values in imports raise `ValueError`.
7. MCP tool failures are reported with `isError=true`.
8. `build_context()` rejects unknown keyword arguments instead of ignoring them.
9. Memory-gap detection requires at least 2 queries, matching the documentation.
10. `delete_namespace()` on a missing namespace raises `NamespaceNotFoundError`.

### New environment variables

`MESH_ALLOW_LOG_SYNC`, `MESH_ALLOWED_HOSTS`, `MESH_EMBED_CACHE_SIZE`, `MESH_TELEMETRY_BATCH`, `MESH_TELEMETRY_FLUSH_SECONDS`.

## Also in this release

The Vite + React landing site now lives in `landing/site/`, with the brand assets in `landing/assets/brand/`. Build it with `npm install && npm run build` from that directory. It is excluded from the Python wheel and sdist.

## Verification

- 126 tests pass locally. CI runs the suite on Python 3.10, 3.11, and 3.12 for every push and pull request to `main`.
- A new retrieval benchmark with hard negatives (`tests/bench/`) guards against relevance regressions.
- New operator scripts cover concurrency stress, privacy auditing, and clean-install checks.

**Full diff:** https://github.com/billighost/mesh/compare/v0.6.2...v0.6.3

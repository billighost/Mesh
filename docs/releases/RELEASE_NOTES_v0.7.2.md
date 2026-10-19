# Mesh v0.7.2

**Released 2026-10-19** · [PyPI](https://pypi.org/project/mesh-context-layer/0.7.2/) · [Full changelog](https://github.com/billighost/mesh/blob/main/CHANGELOG.md#072---2026-10-19)

0.7.2 is the second audit round. It closes guard bypasses reachable from the SDK, hardens the HTTP/MCP edge, fixes cache and telemetry integrity issues, and makes long-running downloads survive short-lived CLI commands. Every item is pinned by `tests/test_audit_round2.py` or `tests/test_deep_scan_regressions.py` (189 tests total).

## Security

- **Guards now run inside `Mesh.learn()` / `Mesh.update()`**, not just at the MCP/HTTP boundary — SDK and in-process callers can no longer bypass credential redaction or prompt-injection rejection.
- **Unicode and ReDoS bypasses closed.** NFKC fullwidth text, zero-width characters, and soft hyphens no longer hide blocked phrases; oversized input is rejected before regex matching; credential detection is linear on long unbroken text and no longer flags ordinary prose.
- **`/mcp` whitelists context keys**, rejects caller-supplied security-context overrides, binds `agent_id`, and returns proper JSON-RPC errors with `isError: true`.
- **Consistent HTTP semantics** with 400/404/409/429/500, redacted 500 bodies, bounded `/identity` nonces, and a strict loopback-origin regex.
- **Ownership and privacy edges:** contradiction resolution enforces `MESH_ENFORCE_AGENT_OWNERSHIP`; recall filters zero/orthogonal vectors; private rows stay out of `mesh_inspect` totals; the dashboard default namespace no longer drifts with browsing history.
- **URL passwords containing `/`** are redacted before cloud extraction.

## Fixed

- **Bounded namespace caches.** Store, vector, and HTTP pools share an LRU with a total cap; exceeding it returns `429` instead of unbounded growth.
- **Sync correctness.** Watermarks are lock-protected and advance only on success; rotation/truncation resets them; foreground sync seeds at EOF unless `include_history=True`; background failures surface as `last_sync_error`; sync reports captured/stored/deduplicated/error counts.
- **Strict booleans.** `"false"`, `"0"`, and `"no"` parse as false.
- **Cache/vector integrity.** Case-variant namespaces no longer share rows, stale `Mesh(create=False)` handles revalidate after deletion, similarity snapshots are copies, vector deletion keeps the index consistent, and `list_all` sorts before limiting.
- **Telemetry is no longer lost:** failed flushes requeue, missing databases requeue, and deletion scrubs pending query events.
- **FTS recovers from transient errors** instead of staying disabled.
- **Audit scrubbing applies to collection upserts** as it does to `update()`.
- **Version parsing** handles `rc` and local labels correctly.
- **Digest** distinguishes a missing namespace from an empty one and reads searches/stored counts from the right source.
- **Auth tokens:** corrupt files are repaired atomically, concurrent creation never overwrites the winner, and `start_daemon` keeps `daemon.pid` when identity verification times out but the process is alive.
- **Model downloads now survive the CLI.** They run in a detached helper process (`mesh._model_download`), so `mesh setup` and `mesh daemon start` no longer drop the download at exit.
- Passive capture no longer reinterprets resolved log text as a path, and JSONL chat logs are parsed correctly.

## Upgrading

```bash
pip install --upgrade mesh-context-layer
```

Restart `mesh-server`, `mesh-http`, or your MCP client afterwards. No data migration is required.

## Verification

- 189 tests pass locally (39 new audit regressions); CI runs the suite on Python 3.10, 3.11, and 3.12.
- The 0.6.4 mcp pin and the 0.6.5 contradiction fix remain intact.

**Full diff:** https://github.com/billighost/mesh/compare/v0.7.1...v0.7.2

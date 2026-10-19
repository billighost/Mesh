# Mesh v0.7.1

**Released 2026-10-18** · [PyPI](https://pypi.org/project/mesh-context-layer/0.7.1/) · [Full changelog](https://github.com/billighost/mesh/blob/main/CHANGELOG.md#071---2026-10-18)

0.7.1 is a remediation release from a full audit of the 0.7.x tree. It fixes privacy leaks, import safety, TTL semantics, namespace and daemon edge cases, and makes environment parsing defensive. The new `tests/test_remediation.py` pins each fix as a regression test.

## Privacy and security

- **Imports can no longer overwrite private memories.** A Mesh export never contains private rows, so an entry claiming an existing private ID is hand-crafted or hostile. `merge_strategy="overwrite"` now skips it.
- **Private recall queries are redacted from the audit log** when any returned memory is private — searching for the exact private text no longer reprints it in `audit_log`.
- **Privatizing a memory scrubs its contradiction text**, and rewriting content scrubs matching query telemetry.
- **`mesh update` now runs the same ingestion guard as `mesh learn`**: control/zero-width stripping, prompt-injection rejection, and credential redaction. It was previously a bypass.
- **Passive-capture redaction hardened** for URL credentials with empty usernames or `@` in the password, and for `Authorization:` headers. Zero-width/bidi characters are stripped before poison matching.
- **No unconsented cloud extraction.** An unavailable LLM provider degrades to local extraction before any log text is sent.

## Behavior fixes

- **`ttl_days=0` means "no TTL"** everywhere: `mesh update --ttl 0` clears an existing TTL, `reset_ttl` restarts the stored duration, and `mesh learn` stores permanently.
- **Imports are deterministic and idempotent.** Entries without IDs get a content-derived UUID, so re-importing the same file skips rather than duplicates.
- **Namespace operations refuse unsafe targets:** symlink escapes, case-insensitive collisions (`mesh_Work.db` vs `mesh_work.db`), and deletion when a WAL checkpoint cannot complete.
- **No stale vectors** after updates; telemetry stamp refreshes no longer hide another process's writes.
- **Interrupted model downloads recover** when the cached copy is valid, and `onnx-warming` no longer sticks forever.
- **Daemon-proxy writes are never silently re-executed** when the outcome is ambiguous, and caller context (private defaults, ownership, workspace, sync) travels with proxied calls.
- **MCP and HTTP `include_private` defaults agree**, including the `MESH_MCP_INCLUDE_PRIVATE` environment variable.
- **Malformed numeric environment variables never crash a session.** New `mesh/envutil.py` applies documented defaults and bounds to `MESH_HTTP_PORT`, `MESH_IDLE_UNLOAD_SECONDS`, `MESH_EMBED_CACHE_SIZE`, and peers; `nan`/`inf`/garbage fall back cleanly.
- **Passive capture bounds memory use** for piped logs and truncates the oldest content first.
- `mesh.__all__` now matches the lazy export table, and the dashboard reports save/resolve/delete failures instead of failing silently.

## Documentation

`SKILL.md` now documents per-agent skill directories, separate Antigravity and Gemini CLI MCP config files, `ttl_days=0` semantics, full `guard_ingest()` coverage, newer environment variables, and the daemon startup subcommands.

## Upgrading

```bash
pip install --upgrade mesh-context-layer
```

Restart `mesh-server`, `mesh-http`, or your MCP client afterwards. No data migration is required.

## Verification

- 142 tests pass locally (15 new remediation regressions); CI runs the suite on Python 3.10, 3.11, and 3.12.
- The 0.6.4 mcp pin and the 0.6.5 contradiction fix remain intact.

**Full diff:** https://github.com/billighost/mesh/compare/v0.7.0...v0.7.1

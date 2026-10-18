# Changelog

All notable changes to `mesh-context-layer` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html) (pre-1.0: minor versions may include behavior changes).

## [0.7.2] - 2026-10-19

Second security and correctness audit round. Every fix is pinned by `tests/test_audit_round2.py` and `tests/test_deep_scan_regressions.py`.

### Security

- **Ingestion guards now live inside `Mesh.learn()` and `Mesh.update()`**, not only at the MCP/HTTP boundary. SDK callers and in-process code can no longer bypass credential redaction or prompt-injection rejection.
- **Poison and credential checks resist Unicode and ReDoS bypasses.** NFKC fullwidth text, zero-width characters, and soft hyphens no longer split a blocked phrase past the regexes; oversized input is rejected before matching; credential detection is linear on long unbroken text. Credential detection also stops flagging ordinary prose while still redacting real secrets.
- **`/mcp` hardened.** Context keys are whitelisted, security-context overrides from callers are rejected, and `agent_id` is bound by the server. Protocol errors now return JSON-RPC errors with `isError: true`.
- **Consistent HTTP semantics.** Dashboard and API routes return 400/404/409/429/500 deliberately and redact internal details from 500 responses. `/identity` bounds nonce length (≤128) and charset. The local-origin regex accepts only genuine loopback origins.
- **Ownership and privacy edges.** `resolve_contradiction` enforces `MESH_ENFORCE_AGENT_OWNERSHIP`; recall filters zero/orthogonal vectors; `mesh_inspect` totals respect filters and exclude private rows; the dashboard's default namespace no longer drifts with browse history, and launch nonces are bounded and pruned.
- **Secrets in URLs** whose password contains `/` are redacted before any cloud upload.

### Fixed

- **Bounded caches.** Store, vector, and HTTP caches now share a namespace-level LRU with a total cap; exceeding it returns `429` instead of growing without limit.
- **Sync correctness.** Log watermarks are lock-protected, advance transactionally only on success, and reset on rotation/truncation; foreground `mesh_sync` seeds at EOF unless `include_history=True`; background sync records and exposes `last_sync_error`; results report `captured_count`, `stored_count`, `deduplicated_count`, and `error`.
- **Strict booleans.** `"false"`, `"0"`, and `"no"` now parse as false everywhere instead of falling through Python truthiness.
- **Cache and vector integrity.** Case-variant namespaces no longer share cached rows; a stale cached `Mesh(create=False)` is revalidated after out-of-band deletion; similarity snapshots are copies, not live matrix views; deleting a vector keeps the index consistent; `list_all` sorts by confidence before applying the limit.
- **Telemetry loss.** Failed flushes requeue events, flushes requeue when the database is missing, and delete scrubs pending query telemetry for the deleted content.
- **FTS resilience.** A transient FTS error no longer permanently disables FTS; `clear_namespace` scopes its FTS cleanup correctly.
- **Audit scrubbing on collection upserts**, matching the behavior of `update()`.
- **Version parsing** no longer misreads `rc` or local labels, and `is_version_compatible` compares them correctly.
- **Digest accuracy.** A missing namespace is reported as missing instead of as an empty one; searches and stored counts are read from the correct stats source.
- **Auth token durability.** A corrupt token file is repaired on disk atomically; concurrent creation or repair never overwrites the winner; `start_daemon` keeps `daemon.pid` when identity verification times out but the process is alive.
- **Model downloads outlive the CLI.** Downloads now run in a detached helper process (`mesh._model_download`), so `mesh setup` / `mesh daemon start` no longer lose the download when the short-lived command exits.
- **Passive capture** no longer reinterprets already-resolved log text as a path, and JSONL chat formats are extracted correctly.
- Pydantic request models bound their inputs; tags receive a `tag+semantic` match reason.
- Package version bumped to `0.7.2`.

### Documentation

- `SKILL.md` clarifies that ingestion guards are enforced inside `Mesh.learn()` / `Mesh.update()` for every caller, and documents the `tag+semantic` match reason.

## [0.7.1] - 2026-10-18

A remediation release from a full audit of the 0.7.x tree: privacy leaks, import safety, TTL semantics, namespace and daemon edge cases, and defensive environment parsing. All are covered by the new `tests/test_remediation.py` regression suite.

### Security

- **Imports can no longer overwrite private memories.** A Mesh export never contains private rows, so an entry claiming an existing private ID is hand-crafted or hostile. `merge_strategy="overwrite"` now skips it and leaves the stored memory untouched.
- **Private recall queries are redacted from the audit log.** If any returned memory is private, the logged query text is replaced with `[REDACTED]` — a search for the exact private text would otherwise reproduce it in `audit_log`.
- **Privatizing a memory scrubs its contradiction text**, and rewriting content scrubs matching query telemetry, so neither surface keeps the old plaintext.
- **`mesh update` runs the same ingestion guard as `mesh learn`.** Content edits now strip control/zero-width characters, reject prompt-injection attempts, and redact credentials; previously `mesh update` was a bypass.
- **Passive capture redaction hardened:** URL-embedded credentials with empty usernames (`redis://:pass@host`) or passwords containing `@` are fully redacted, as are `Authorization:` headers. Zero-width/bidi characters are stripped before poison-pattern matching.
- **No unconsented cloud extraction.** A configured-but-unavailable LLM provider now degrades to local extraction *before* dispatch, so log text is never POSTed without the consent prompt.

### Fixed

- **`ttl_days=0` now means "no TTL"** instead of "expired": `mesh update <id> --ttl 0` clears an existing TTL like `clear_ttl`, `reset_ttl` restarts the existing duration, and `mesh learn` stores the memory permanently. Effective-confidence math follows the same rule.
- **Imports are deterministic and idempotent.** Entries without IDs receive a content-derived UUID, so re-importing the same file skips instead of duplicating.
- **Namespace safety.** Namespace paths are resolved inside the Mesh directory and refuse symlink escapes; destructive operations refuse to run when a case-insensitive filesystem maps two casings to the same database (`mesh_Work.db` vs `mesh_work.db`); deletion refuses to proceed if a WAL checkpoint cannot complete, rather than orphaning plaintext frames; `list_namespaces()` aggregates every namespace value stored in a shared file.
- **No stale vectors.** Updates trust the embedding BLOB on disk instead of reusing an old in-memory vector, and telemetry stamp refreshes no longer make another process's writes invisible.
- **Interrupted model downloads recover.** A file that passes the size check but fails SHA-256 is replaced from the verified cache; checksum results are cached; `onnx-warming` no longer sticks forever when no model is available.
- **Daemon-proxy correctness.** Ambiguous write failures (daemon answered, then the connection dropped) are reported as unknown outcomes and never silently re-executed; `context` travels with proxied calls so private defaults, ownership enforcement, workspace, and sync flags match the caller instead of the daemon's environment.
- **MCP and HTTP private-memory defaults honor the caller's context.** `default_include_private` from the MCP client, the `MESH_MCP_INCLUDE_PRIVATE` env var, and HTTP `inspect` / `patterns` / `context` now agree instead of silently diverging.
- **Malformed numeric environment variables never crash or corrupt a session.** New `mesh/envutil.py` parses `MESH_HTTP_PORT`, `MESH_IDLE_UNLOAD_SECONDS`, `MESH_EMBED_CACHE_SIZE`, and other numeric settings with documented defaults and bounds; `nan`/`inf`/garbage fall back cleanly.
- **Passive capture bounds memory use** when reading piped multi-GB logs, and joins selected files chronologically so truncation drops the oldest content.
- **`mesh.__all__` now matches the lazy export table**, so IDEs and static analysers see the real public API.
- **Test clients**: `get_audit_log()` filters strictly by namespace.
- **Dashboard** save, conflict-resolve, and namespace-delete failures show an error instead of failing silently.
- Package version bumped to `0.7.1`.

### Documentation

- `SKILL.md` documents skill install locations per agent, separate Antigravity and Gemini CLI MCP config files, `ttl_days=0` semantics, the full `guard_ingest()` coverage, and the newer environment variables (`MESH_TOKEN`, `MESH_NO_AUTO_DAEMON`, `MESH_ALLOW_DAEMON_DOWNLOAD`, `MESH_WORKSPACE`, `MESH_MODEL_SHA256`, `MESH_TOKENIZER_SHA256`, `MESH_AI_*`) and daemon subcommands.

## [0.7.0] - 2026-10-18

This release redesigns the command-line experience around a single design system. Commands, flags, and stored data are unchanged; the differences are in how results are presented and how scripted use behaves.

### Added

- **`mesh/ui.py`**, a stdlib-only presentation layer: a consistent colour policy, Unicode symbols with an automatic ASCII fallback, aligned tables, cards, key/value blocks, a spinner, and y/n/text prompts. No new dependencies.
- **`--json` on `mesh ask`, `mesh inspect`, `mesh status`, `mesh conflicts`, `mesh patterns`, and `mesh namespaces`** for stable, machine-readable output.
- **`mesh add` reads content from stdin when piped**, so `cat notes.txt | mesh add --tags notes` works.
- **`mesh help <command>`**, a shortcut for `mesh <command> --help`. Every command now carries usage examples in its help text.
- **Output environment variables:** `NO_COLOR` / `MESH_NO_COLOR` disable colour, `FORCE_COLOR` / `MESH_FORCE_COLOR` force it, `MESH_ASCII=1` selects ASCII symbols, and `MESH_NO_SPINNER=1` disables animation.

### Changed

- **`mesh update` with no changes is now an error** (`exit 1`) instead of a silent no-op.
- **`mesh forget` asks for confirmation only in an interactive terminal.** `-y` skips the prompt; scripts and agents are unaffected and never block.
- **Declined or non-interactive permission prompts explain what happened** and how to approve explicitly (`--yes`) instead of skipping silently.
- **Presentation only:** `digest`, `io`, `passive` capture, `memory` warnings, `daemon` prompts and `mesh daemon *`, the update notice, and the dashboard/HTTP banners now use the shared design system. Behaviour and security rules are unchanged.
- **Ctrl+C and `| head` exit quietly** (status 130 and 0 respectively) instead of printing a traceback or broken-pipe noise.

### Fixed

- The generated **PowerShell completion script** ended without a final newline, which produced a syntax error when appended to a profile.
- Package version bumped to `0.7.0`.

## [0.6.5] - 2026-10-18

### Fixed

- **Contradictions are detected under the `hash-384` fallback.** The subject-value check required a 0.70 cosine similarity even when both statements shared an identical subject and assigned different numbers to the same slot (`primary database is db1` vs `primary database is db2`). Hash embeddings score that pair at ~0.64, so conflict detection silently returned nothing whenever the ONNX model was unavailable — including a fresh install before the model download completes. Numeric slot conflicts are now structural and independent of embedding similarity; non-numeric value changes still require the similarity gate. Added a regression test that runs without the neural model.
- Package version bumped to `0.6.5`.

## [0.6.4] - 2026-10-18

### Fixed

- **Fresh installs resolved an incompatible `mcp`.** The dependency is now `mcp>=1.0.0,<2`. mcp 2.0 replaced the `Server.list_tools()` / `Server.call_tool()` decorators with constructor callbacks, so an install that picked up `mcp 2.3.0` failed at import: `mesh.mcp_server`, and every CLI entry point that imports it, raised `AttributeError: 'Server' object has no attribute 'list_tools'`. mesh keeps the mcp 1.x handler API until the 2.x handler model is supported.
- Package version bumped to `0.6.4`.

## [0.6.3] - 2026-10-18

This release fixes a set of privacy, concurrency, search-quality, and documentation issues found in a full code review. It contains several **behavior changes** for security reasons; see "Changed (behavior changes)" below before upgrading.

### Security

- **Private memories no longer leak through `patterns`.** `get_patterns()` (`mesh_patterns`, `GET /patterns`, `mesh patterns`, dashboard) now excludes `local_only` memories from `unused_memories` and `stale_memories` by default, returns a whitelist of fields only (no embeddings), and excludes queries made with private recall from topic and gap analysis. New opt-in: `include_private`.
- **Daemon identity verification.** The CLI, MCP proxy, and daemon control commands now verify the daemon with an HMAC challenge-response (`/identity`) **before** sending the bearer token, so a process squatting the port can no longer harvest tokens or serve forged data. `/health` no longer returns the process ID or engine details. Token comparisons use constant-time checks.
- **Ingestion guards on every path.** `mesh_learn`, HTTP `/learn`, the dashboard, CLI `mesh add`, and imports now run the same poison-attempt and credential checks that `mesh_sync` already used. Detected credentials are redacted and forced `local_only`; prompt-injection attempts are rejected.
- **Untrusted-content markers.** `mesh_recall`, `mesh_inspect`, `mesh_patterns`, and `mesh_sync` responses now include `untrusted_reference_data: true` and a notice.
- **`mesh_sync` log reading is now opt-in.** Agent logs (Cursor, Claude, Antigravity) are only read when `MESH_ALLOW_LOG_SYNC=1` or `MESH_ENABLE_AUTO_SYNC=1`.
- **Host allowlist hardening.** `testserver` was removed from the static allowed hosts of the HTTP server and dashboard. Binding to a non-loopback interface now requires `MESH_ALLOWED_HOSTS`, and the startup warning is accurate.
- **`MESH_LOCKED_NAMESPACE` enforcement** verified and enforced across MCP, HTTP, the daemon proxy, and the dashboard.
- **Context sanitizer hardening.** Unicode is normalized (`NFKC`) and zero-width characters are stripped before breakout-tag removal; the JSON context format now sanitizes content and returns a whitelist of fields.

### Fixed

- **Mixed embedding-space guard** is now applied in the main recall path (`recall_with_context`), in `discover_other_possible`, and in `learn()` deduplication and contradiction checks. Any two differing known model tags are treated as different spaces.
- **`learn()` is now atomic.** A per-namespace write lock (in-process plus cross-process file lock) and `BEGIN IMMEDIATE` transactions prevent duplicates and missed contradictions when several agents write at once. `update`, `forget`, contradiction resolution, imports, and re-embedding use the same lock.
- **Cross-process cache thrash.** Vector-cache validity now follows a `content_version` counter that telemetry writes do not bump, and recall telemetry (`access_count`, `queries`) is buffered and flushed in batches. Two agents recalling alternately no longer force each other to rebuild the cache.
- **Filters are applied before the top-k cut.** `memory_type`, tag, agent, privacy, staleness, and confidence filters are now a pre-mask, so a rare type can no longer return nothing in large namespaces.
- **Relevance noise.** Substring matching (`port` matching `report` or `import`) was replaced with whole-token or same-stem matching; the prefix fallback is stricter (`pass` no longer matches `password`); over-broad synonym groups (`port`≈`url`≈`host`≈`ip`≈`endpoint`, `main`≈`production`) were split; synonym-only matches receive a reduced weight and no longer qualify for the lexical grounding floor. A retrieval benchmark with hard negatives now guards against regressions.
- **`port = 5432` is now parsed** by `_extract_subject_value_pair` (the `\b` before `=` bug).
- **Imports label embeddings correctly.** `import_namespace` stores the real embedding model tag, queues hash vectors for re-embedding, rejects untrusted or degenerate vectors, validates `merge_strategy` (unknown values now raise `ValueError`), and returns real statistics in `--dry-run`.
- **Memory use.** The embedding cache now stores `float32` arrays (configurable via `MESH_EMBED_CACHE_SIZE`) and is cleared by `unload_model()`. The re-embed queue is bounded and deduplicated, and at most one healing thread runs per namespace.
- **Namespace sprawl.** Read-only paths (CLI reads, MCP read tools, HTTP read routes, `warmup_model`) no longer create database files for non-existent namespaces and return `did_you_mean` suggestions. Learning into a new namespace reports `namespace_created` and any similar existing names.
- **Model bootstrap is visible.** Checksum failures, skipped non-interactive downloads, and fallbacks to `hash-384` are logged and recorded in `~/.mesh/model_status.json`, and surfaced in `mesh doctor`, `mesh status`, and MCP response metadata (`embedding_degraded`). Pinned ONNX hashes were re-verified against the real artifacts.
- **MCP errors** are now returned with `isError=true` instead of as ordinary content.
- **CLI.** Read commands fall back to local execution when the daemon times out; mutating commands report an unknown outcome instead of silently retrying. `mesh namespaces delete` now handles locked files on Windows with a clear message, and deleting a non-existent namespace is an error instead of a silent "0 removed".
- **`namespaces`.** A failed `rename_namespace` now cleans up and leaves the original untouched; audit-rename failures are no longer swallowed; `list_namespaces` reports namespaces with unreadable databases instead of hiding them; `last_updated` now reflects edits and access, not just creation.
- **`mesh_patterns` memory gaps** now require at least 2 searches, matching the documentation (previously 1).
- **`digest`.** Rows with unparseable timestamps are no longer counted as new; the window label is always `last Nh` (48 hours was labelled "today"); entries include short IDs.
- **`context_builder`.** `build_context()` now honors an explicit `mesh_instance` instead of silently ignoring it; the Mesh instance cache is bounded.
- **`version`.** Pre-release versions (`0.6.2rc1`) now sort before the final release; a test keeps `version.py` and `pyproject.toml` in sync.
- **`skill_installer`.** Corrected the Windows Cursor path (a duplicated `Roaming` segment), verified each agent's skill directory against its documentation, handled symlinked/junction destinations, made installs atomic, stopped prompting during `--dry-run`, and added version stamping so up-to-date installs are skipped.
- **Agent identity.** The SDK now reads `MESH_AGENT_ID` (default `agent`), matching the documentation.

### Changed (behavior changes)

- `mesh_patterns` / `GET /patterns` / `mesh patterns` no longer include private memories unless `include_private` is passed.
- `mesh_sync` no longer reads local agent logs unless `MESH_ALLOW_LOG_SYNC=1` or `MESH_ENABLE_AUTO_SYNC=1`.
- `mesh_learn` (and other ingestion paths) now redact detected credentials and store them as private, and reject prompt-injection attempts.
- Binding the HTTP server or dashboard to a non-loopback address requires `MESH_ALLOWED_HOSTS`.
- `GET /health` returns only `status` and `version`; process and engine details moved behind authentication.
- Unknown `merge_strategy` values in imports raise `ValueError`.
- MCP tool failures are reported with `isError=true`.
- `build_context()` rejects unknown keyword arguments instead of ignoring them.
- Memory-gap detection requires at least 2 queries.

### Added

- `include_private` option for patterns across SDK, MCP, HTTP, CLI, and dashboard.
- `/identity` endpoint and `daemon.verify_daemon_identity()`.
- Environment variables: `MESH_ALLOW_LOG_SYNC`, `MESH_ALLOWED_HOSTS`, `MESH_EMBED_CACHE_SIZE`, `MESH_TELEMETRY_BATCH`, `MESH_TELEMETRY_FLUSH_SECONDS`.
- `NamespaceNotFoundError` and a `create` option on `Mesh`/`MeshStore`.
- `~/.mesh/model_status.json` and degraded-state reporting in `mesh doctor` and `mesh status`.
- Retrieval benchmark (`tests/bench/`), concurrency stress script, privacy audit script, and clean-install check script.

### Documentation

- `SKILL.md` rewritten and corrected: the SDK examples no longer call non-existent methods (`m.build_context`, `m.get_patterns`), privacy defaults are documented per surface, the "prompt-injection-safe" claim was reworded, environment variables and the CLI reference were completed, and a security model and troubleshooting section were added.
- `README.md` updated to match the new behavior and defaults.
- Docstrings that made inaccurate claims were corrected.

### Packaging

- Bundled skill is shipped at `mesh/skills/mesh-context-layer/SKILL.md` and verified to be present in the built wheel.
- Package version bumped to `0.6.3`.

---

Earlier release notes (0.6.2 and before) are not included in this file.

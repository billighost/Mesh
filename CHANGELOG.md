# Changelog

All notable changes to `mesh-context-layer` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html) (pre-1.0: minor versions may include behavior changes).

## [0.8.3] - 2026-10-25 (Beta)

Performance acceleration and reliability release introducing native C acceleration core, high-speed C CLI client, background stage-2 ONNX re-embedding, filesystem path memoization, and namespace pooling fixes.

### Added
- **Native C Acceleration Core (`c_core/mesh_core.c` & `mesh/c_bridge.py`)**: High-performance C core with SIMD (AVX2/FMA) vector dot-product computation, fast ASCII security prescan filter, hash embedding generation, and vectorized TTL decay calculations. Includes pure-Python/NumPy automatic fallbacks when the native library is absent or disabled (`MESH_DISABLE_NATIVE=1`).
- **High-Speed Native C CLI Client (`c_cli/mesh_fast.c` & `mesh/native_cli.py`)**: Ultra-low-latency C CLI wrapper interfacing with daemon/HTTP surfaces via zero-overhead socket transport.
- **Background Stage-2 ONNX Re-Embedding (`mesh/memory.py`)**: Asynchronous worker to re-embed memory rows from fast-path hash embeddings to high-precision ONNX neural representations in the background (`MESH_BG_ONNX=1`), keeping foreground operations fast.
- **Filesystem Path Memoization & Trust Caching (`mesh/audit.py`, `mesh/crypto.py`)**: High-efficiency lstat memoization (`MESH_FS_MEMO_MS`) and path validation trust cache (`MESH_PATH_RECHECK_SECONDS`) to eliminate redundant disk I/O during security audit checks.
- **Initialization Lock Retries (`mesh/store.py`)**: Resilient acquisition retry loop (`MESH_INIT_LOCK_RETRY_SECONDS`) for database schema initialization and migrations under multi-process contention.
- **Multi-Surface Benchmarking Suite (`benchmark_all_surfaces.py`)**: End-to-end benchmark suite verifying throughput and latency across CLI, C CLI, SDK, MCP, and HTTP interfaces.

### Fixed
- **Namespace Listing & Stats Query Optimization (`mesh/namespaces.py`)**: Resolved namespace query bottlenecks by connecting via `MeshStore` pooling and utilizing warm in-memory cache rows instead of redundant disk opens. Added CLI mesh cache eviction on namespace deletion.

## [0.8.2] - 2026-10-22 (Beta)

Patch release verifying and resolving findings following the 0.8.1 release (F1–F8).

### Fixed
- **Claude Code MCP Configuration Path & Legacy Migration (F1)**: Updated `ClaudeCodeAdapter` to use repository-root `.mcp.json` for project scope (aligned with Claude Code specification) and `~/.claude.json` for user scope. Added automatic detection and migration of legacy `.claude/mcp.json` files with user notification. Added agent aliases `claude` (mapping to `claude-code`) and `gemini` (mapping to `gemini-cli`), and marked unverified agent adapters in installer and doctor outputs.
- **Hook Contract, Silent Brief Handling & Credential Redaction Idempotence (F8)**: Configured Antigravity hook integration to output JSON `{"injectSteps": [{"ephemeralMessage": ...}]}` under the `PreInvocation` event. Updated `mesh brief` to include untrusted reference data banners and gracefully handle unexpected storage exceptions without breaking host agent sessions. Fixed `_AUTH_HEADER_RE` and `_YAML_BLOCK_SECRET_RE` regexes to exclude `[REDACTED_...` and `[SECRET_REF:...` placeholders to ensure secret redaction is strictly idempotent. Added exhaustion handling and exponential backoff retry test for `_safe_replace_dir`.
- **Cryptographic Specifications & Provenance Alignment (F3)**: Corrected stale encryption references in `README.md` and `SECURITY.md` to accurately describe Mesh's true implementation: application-level AES-256-GCM envelope (with stdlib BLAKE2b/HMAC Encrypt-then-MAC fallback). Clarified verification metadata and field-test provenance in `README.md`. Added automated `check_no_fernet_references` test in `scripts/check_docs.py`.

### Added
- **Decision Rationale Tracking & Flagging (F2)**: Added causal rationale requirement for architectural decisions to `rules_block.md` (Rules 3 & 4) and `SKILL.md`. Updated `get_project_brief` in `mesh/store.py` to annotate decision memories lacking causal markers (e.g. `because`, `due to`) with ` *(needs rationale — add 'because <reason>')*` without modifying the database schema.
- **Security Corpus Test Suite (F8)**: Added `tests/test_secrets_corpus.py` covering 8 distinct scenarios: PINs, unquoted passphrases, multiline PEM certificates, tokens, JSON/YAML payload redactions, task updates, benign look-alikes, and hook contracts.
- **Repository Codeowners (F6)**: Added `.github/CODEOWNERS` declaring `@billighost` as repository code owner.

### Changed
- **Restricted PyPI Publication Trigger (F5)**: Hardened `.github/workflows/publish.yml` to remove automatic wildcard tag triggers (`push: tags: ['v*']`), restricting PyPI Trusted Publishing strictly to explicit GitHub release publications (`release: [published]`) and manual `workflow_dispatch`. Updated release guidelines in `DEVELOPER_GUIDE.md`.

### Removed
- **Redundant CI Workflow (F7)**: Removed obsolete `.github/workflows/test.yml` in favor of consolidated matrix testing in `.github/workflows/ci.yml`.
- **Repository Hygiene Cleanup (F6)**: Removed untracked and stale `unwanted/progress.md` file and `unwanted/` directory.

## [0.8.1] - 2026-10-22 (Beta)

Comprehensive documentation refresh and defect resolutions following Run T field-test findings.

### Added
- **Agent Lifecycle Hooks Feature (T-003)**: Added `--hooks` opt-in flag to `mesh install`. Configures `SessionStart` project brief injection (`mesh brief`) on supported agent runtimes (Claude Code via `.claude/settings.json` and Antigravity via `hooks.json`). Implemented atomic `.bak` file backups and safe merging in `mesh/installer.py`.
- `DOCS.md`: end-user tutorial and reference covering installation, agent connection, the full CLI, all 17 MCP tools, the HTTP API, Python SDK, dashboard, environment variables, privacy, secrets, troubleshooting, and upgrade/uninstall.
- `CONTRIBUTING.md`, `AGENTS.md` (instructions for coding agents working on this repository), `llms.txt` (documentation index for LLM tooling), and `examples/README.md`.
- Public field-test archive under `docs/field-test/` (report addenda, issue list, data tables, and reproduction scripts). Files containing absolute paths, usernames, or planted credentials were deliberately not imported; see `docs/field-test/README.md`.
- Extended `scripts/check_docs.py` coverage for environment variables, HTTP routes, README rules-block equality, and field-test version labels.

### Changed
- `README.md` rewritten for readers and AI agents: at-a-glance facts, supported agents with verified status, one-command installer, the always-on protocol, goals/tasks/project log, privacy and network behavior, evidence from `benchmarks/RESULTS.md` and the independent field test, limitations, a grouped FAQ, and upgrade/uninstall instructions.
- `ARCHITECTURE.md` updated to schema version 3: 17 MCP tools, 10 memory types, work items and events, brief/changelog, installer behavior, audit, concurrency, and trust boundaries.
- `DEVELOPER_GUIDE.md` updated with isolated test recipes, extension points, documentation checks, release process, and field-test reproduction.
- `CHANGELOG.md` now records documentation changes separately from release behavior changes.

### Fixed
- **Windows Skill Replacement Lock Contention (T-001)**: Wrapped `os.replace` in `_safe_replace_dir()` with exponential backoff and explicit garbage collection in `mesh/skill_installer.py` to prevent transient Windows `Access is denied` errors during skill installs.
- **Secret Detection & Task Quarantine (T-002)**: Expanded `_KNOWN_API_KEY_RE` in `mesh/secrets.py` to match arbitrary prefix token segments, extended `_KEYWORD_ASSIGNMENT_RE` and `_is_plausible_secret_value` for locker/pass codes, and updated `update_work_item` / `complete_work_item` in `mesh/store.py` to quarantine tasks (`local_only=1`) when notes or summaries contain secrets.
- **Agent Lifecycle Hooks Implementation (T-003)**: Implemented missing hook routines in `mesh/installer.py` and `mesh/cli.py` with full uninstallation and rollback support.
- Replaced obsolete benchmark figures with the numbers and method recorded in `benchmarks/RESULTS.md`.
- Corrected the MCP tool count (17), memory-type count (10), and embedding cache size (1024) across the documentation.

## [0.8.1b2] - 2026-10-20 (Beta)

Re-test fixes for issues C-001 and C-002 from the third independent black-box evaluation (Run C), plus two approved diagnostic additions.

### Fixed
- **Thread-safe pooled SQLite connections (C-001)**: A pooled `sqlite3.Connection` (opened with `check_same_thread=False`) could be entered by several threads at once, producing `sqlite3.InterfaceError: bad parameter or other API misuse`, `IndexError: tuple index out of range` from row conversion, and `sqlite3.ProgrammingError: Cannot operate on a closed database` during pool eviction. Each pooled connection now owns a re-entrant lock serializing its cursor/statement state, uses a separate lifecycle lock so `retire()` never blocks in-flight users, and is reference-counted through checkout/checkin so a connection is only physically closed when the last user leaves. SDK threads, HTTP worker threads, MCP `asyncio.to_thread` calls and pool-eviction races are covered by `tests/test_thread_safety_c001.py` (8 tests, including 4-process concurrency).
- **SDK `learn()` reports the stored privacy flag (C-002)**: `Mesh.learn()` returned no `local_only` value, so callers could not tell that `guard_ingest` had silently forced a secret-bearing memory private, and could not distinguish public from private writes. `MeshStore.upsert()` now returns the effective value persisted after the ingestion guards; `Mesh.learn()` surfaces it on both the insert and deduplication paths (including the audit log); and the MCP, HTTP, dashboard and CLI surfaces report the stored value instead of the requested one. Covered by `tests/test_learn_local_only_c002.py` (10 tests, each checked against the SQLite row that was written).

### Added
- **Doctor project-vs-user MCP precedence (optional re-test item)**: `mesh doctor` now reports, read-only, when the project-scope MCP config for the current directory takes precedence over a user-scope config, or is the only active one. Covered by `tests/test_doctor_scope_precedence.py` (6 tests).
- **Mocked ONNX mirror-fallback coverage (optional re-test item)**: `tests/test_model_download_fallback.py` exercises the existing Hugging Face → `hf-mirror.com` fallback with all HTTP mocked: primary HTTP errors, connection errors, truncation and checksum mismatches fall through to the pinned secondary mirror, and a payload that fails checksum verification on every mirror is never installed. No new network destination was added.

### Verified
- Full suite: 317 passed. The nine fixes from `0.8.1b1` show no regression (`tests/test_agent_scenarios.py`, `test_installer.py`, `test_memory.py`, `test_security_audit_v08.py`, `test_work_items.py`: 91 passed).
- Package version bumped to `0.8.1b2`.

## [0.8.1b1] - 2026-10-20 (Beta)

Field Test Findings Resolution: Hardening and fixes from two independent black-box agent evaluations (Run A & Run B).

### Fixed
- **Secret Ingestion Leakage Prevention (A-004, B-005)**: Expanded secret scanner regexes to catch generic API key tokens (`WEBHOOK_KEY_*`, `payment_gateway_key`, etc.) and unquoted passphrase statements, ensuring they are quarantined to `local_only=True` without false-positive leakage into shared or cloud layers.
- **Installer MCP Arguments & Alias (A-002)**: Configured agent MCP setups to invoke `mesh server` rather than invalid `mesh mcp`, and added `mesh mcp` subcommand alias to `mesh/cli.py` for backward compatibility.
- **Non-Interactive Install Execution (A-001, B-002)**: Fixed `mesh install --apply --json` (and `--yes --json`) so that automation scripts properly apply and persist configuration changes rather than remaining in dry-run plan mode.
- **Project-Scoped Isolation & Virtualenv Resolution (A-003, B-001)**: Bound `MESH_HOME` to `project_dir/.mesh-home` by default in project-scoped MCP configs, added `--mesh-home` flag to `mesh install`, and prioritized project `.venv` binaries.
- **MCP Server Stderr Cleanliness (A-008, B-007)**: Suppressed background warmup worker confirmation prompt when running non-interactively or with `MESH_NO_AUTO_DAEMON=1`, preventing stderr noise in MCP host environments.
- **Work Item Reopened Lifecycle Transition (A-005, B-006)**: Accepted `status="reopened"` and `status="reopen"` in `update_work_item`, transitioning item back to `todo`, clearing completion timestamp, and logging a `reopened` event.
- **Python SDK Method Aliases (A-006, B-004)**: Added `task_create`, `task_update`, `task_complete`, `task_list`, and `log` method aliases on `Mesh` and `MeshStore` mirroring MCP tool names.
- **Actionable Task ID Error Guidance (A-007)**: Improved unknown work item error messages to provide list of available tasks in the namespace or guidance to create a task first with `mesh_task_create`.
- **Defunct GCS Tarball Mirror Removed (B-003)**: Removed deprecated GCS fastembed bucket that returned HTTP 403 Forbidden, streamlining clean fallback to HuggingFace mirrors.

## [0.8.0] - 2026-10-19

Agent Autonomous Adoption & Production Readiness Release: First-class work items, token-budgeted briefs, automated multi-agent installer, progressive disclosure skill, content-free audit telemetry, full-suite benchmarks, and hardened security gates.

### Added
- **First-class Work Items and Event Log (`work_items`, `work_events`)**:
  - Deterministic tracking for goals and actionable tasks in SQLite, completely separated from vector decay, dedup, and contradiction detection.
  - Schema bumped to version 3 with automated pre-migration SQLite backups (`.backup-v0.7.2-<timestamp>`).
  - New MCP tools: `mesh_task_create`, `mesh_task_update`, `mesh_task_complete`, `mesh_task_list`.
  - Mandatory completion summary and optional evidence (file paths, commit SHAs, URLs) on `mesh_task_complete`.
- **Session-Start Brief & Project Changelog**:
  - `mesh_project_brief` MCP tool and `mesh brief` CLI: single-call session primer loading namespace health, open tasks, recent completions, and core facts within an exact token budget (default: 500 tokens).
  - `mesh_log` MCP tool and `mesh log` CLI: chronological changelog combining completed tasks, evidence, and architectural decisions over time windows (`24h`, `7d`, `30d`).
- **Unified Multi-Agent Installer (`mesh install` / `mesh uninstall`)**:
  - One-command configuration for Claude Code, Claude Desktop, Antigravity IDE & CLI, Gemini CLI, Cursor, Windsurf, OpenCode, and Codex.
  - Generates atomic backups (`.bak.<timestamp>`), dry-run preview with line diffs, project vs user scopes, and preserves existing MCP servers without data loss.
  - Single source of truth always-on rules block (<= 12 lines) injected into `AGENTS.md`, `CLAUDE.md`, or `.cursorrules` between idempotent markers.
- **Content-Free Audit Telemetry**:
  - Microsecond per-call duration tracking across all surfaces (MCP, CLI, HTTP, SDK).
  - Zero memory or task text logged; `mesh audit export --json` exports content-free metrics for telemetry and CI.
  - Extended `mesh status --json` with schema version, embedding mode, memory counts, and task counts.
- **Progressive Disclosure Skill & Automated Verification**:
  - Rewrote `SKILL.md` (<= 160 lines) with imperative mandatory protocol, decision table, and trigger-rich frontmatter.
  - Modularized bundled references in `references/tool_reference.md`, `references/examples.md`, and `references/troubleshooting.md`.
  - Added `scripts/check_skill.py` wired into pytest to validate all tool names, arguments, version stamps, and line limits.
- **End-to-End Verification**:
  - New test suites `tests/test_work_items.py` (11 tests), `tests/test_installer.py` (9 tests), and `tests/test_agent_scenarios.py` (9 end-to-end scenarios).
  - Added `docs/AGENT_QA.md` manual testing playbook for real coding agents.

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

##  Historical Releases (0.1.0 – 0.6.2)

 

### [0.6.2] - 2026-10-17

#### Added
- **Bundled Agent Skill (`mesh/skills/mesh-context-layer/SKILL.md`)**: Complete operating manual for Mesh covering all 10 MCP tools, Python SDK, CLI, architecture, security model, and troubleshooting.
- **Cross-Agent Skill Installer (`mesh-install-skills` / `mesh.skill_installer`)**: Installs or symlinks `mesh-context-layer` skill into detected agent directories (Claude Desktop, Cursor, Windsurf, Antigravity, Custom).
  - Supports `--global`, `--workspace`, and `--agent <name>`.
  - `--list` flag shows detected skill directories and install status without writing anything.
  - Idempotent: re-running overwrites with the latest skill version.
- **Automatic Skill Install during `mesh setup`**: `mesh setup` calls `install-skills` as its final step.
- **Skill files in wheel and sdist**: `mesh/skills/**/*.md`, `.txt`, `.json` included in package builds.

### [0.6.1] - 2026-10-17

#### Security & Privacy Hardening
- **Eliminated `Host: testserver` Auth Bypass**: Removed unauthenticated bypass logic; added `verify_request_auth` and `verify_dashboard_auth` FastAPI dependencies for clean test overrides. Protected `GET /` on the Web Dashboard with a launch-scoped `?token=` exchange that sets an `HttpOnly; SameSite=Strict` session cookie (`mesh_dash_session`) and strips the token from the URL bar via `history.replaceState`. Fixed IPv6 localhost CORS regex (`[::1]`).
- **Fixed Dashboard Stored XSS & Added Memory ID Validation**: Enforced strict regex validation (`validate_memory_id`) across `store.py` and `io.py`. Replaced all inline `onclick="..."` string interpolations in `dashboard.py` with delegated `data-action` and `data-id` attributes and full HTML/attribute escaping.
- **Redacted Private Memories in Contradiction Payloads & Cold Cache**: Redacted `existing_content` and `existing_memory` in the `conflict` dictionary returned by `learn()` whenever the existing memory has `local_only=True`. Hardened `store.log_contradiction()` to query SQLite when the in-memory cache is cold. Defaulted `include_private=False` on recall and inspect.
- **Historical Audit & Contradiction Scrubbing on `update()`**: Added `scrub_memory_update_from_audit()` and automatic contradiction scrubbing whenever memory content is modified or marked `local_only=True` via `update()`. Added automatic retention pruning to bound `audit_log` (`MESH_MAX_AUDIT_ROWS=10000`) and `queries` (`MESH_MAX_QUERY_ROWS=10000`).
- **Atomic `0600` Token/DB Permissions & `0700` Directory Permissions**: Created `~/.mesh` with mode `0700` and sensitive files (`auth_token`, `daemon.pid`, `daemon.log`) atomically with `os.open(..., 0o600)`.
- **Explicit `y/n` User Consent Across Background & Cloud Operations**: Added interactive `y/n` prompts before downloading ONNX models, starting background threads, sending logs to cloud LLMs, modifying shell RC files, or deleting namespaces. Made PyPI update checks strictly opt-in (`mesh status --check-updates` or `MESH_CHECK_UPDATES=1`).
- **Prompt Injection & Context Block Hardening**: Collapsed newlines and control characters inside memories, stripped leading markdown heading/bullet markers, and neutralized `<mesh_context` sequences. Added `MESH_LOCKED_NAMESPACE` enforcement.

#### Fixed & Improved
- **MCP Daemon Proxy Stale Keep-Alive Recovery**: Detected closed idle keep-alive sockets via `select.select` prior to sending and enforced fresh connections for mutating tools.
- **Fixed Custom AI Provider Detection**: Checked `MESH_AI_API_KEY` + `MESH_AI_BASE_URL` before provider-specific defaults in `detect_provider()`. Discarded partial leading lines when seeking into large `.jsonl` and `.log` files.
- **Verified ONNX SHA-256 Digests & Atomic Model Tagging**: Updated `_ALLOWED_MODEL_SHA256` and `_ALLOWED_TOKENIZER_SHA256` with verified digests and added a 200 MB stream/tarball extraction cap. Stored `(vector, model_tag)` atomically in `_EMBED_CACHE`. Improved deduplication and contradiction detection for alphanumeric identifiers (`db1` vs `db2`). Enforced `MAX_CONTENT_LENGTH = 16384`.
- **Unified Staleness, TTL Clearing & Context Ranking**: Unified staleness calculations across `namespace_stats()`, `generate_digest()`, and `get_patterns()` using `_compute_row_effective_confidence()`. Added `clear_ttl=True` / `--clear-ttl` support across Python, CLI, MCP, HTTP, and Dashboard. Disabled telemetry increments during `build_context()`.
- **CLI, Doctor, Completions & Packaging Polish**: Implemented PEP 562 lazy `__getattr__` in `mesh/__init__.py`. Upgraded `mesh doctor` to verify FTS5, permissions, checksums, and daemon status. Added native completion scripts for bash, zsh, fish, and powershell.

### [0.6.0] - 2026-10-15

#### Added
- **Direct Zero-Network ONNX Runtime Engine (`_DirectOnnxEmbedding`)**: Replaced blocking Hugging Face Hub calls with direct `onnxruntime.InferenceSession` + `tokenizers.Tokenizer` execution (~80 ms cold start, ~1.5–3 ms CPU inference, zero PyTorch overhead). Routed `_encode_texts()` directly through the 4,096-entry `_EMBED_CACHE`.
- **Non-Blocking Multi-Mirror Background ONNX Auto-Downloader**: Downloads `model.onnx` and `tokenizer.json` into `~/.mesh/models/all-MiniLM-L6-v2-onnx/` prioritizing CDN mirrors.
- **In-Memory Metadata Cache & Capacity-Doubling Vector Buffer**: Cached full row metadata and parsed `tags_list` in RAM alongside a capacity-doubling `float32` NumPy matrix buffer.
- **Persistent SQLite WAL Connection Pooling**: Replaced per-call connection closing with persistent pooled SQLite WAL connections (`_PooledConnection` and `_ManagedAuditConnection`).

### [0.5.3] - 2026-10-15

#### Added
- Added `GET /engine` and `POST /unload` HTTP REST endpoints and `mesh-daemon` CLI management commands.
- Added background inactivity watchdog (`MESH_IDLE_UNLOAD_SECONDS=180`) to unload neural model weights when idle.

### [0.5.1] - 2026-10-15

#### Performance & Optimizations
- **High-Speed Batch Import Pipeline**: Implemented `upsert_batch()` writing all imported memories in a single atomic SQLite WAL transaction. Batch-computed missing embeddings in a single forward pass with LRU cache integration. Pre-resolved existing IDs in $O(1)$ from the in-memory vector cache.
- **Single-Query Export Embedding Resolution**: Replaced individual vector lookups with batch ID resolution for missing embeddings.
- **Single-Pass Context Builder**: Streamlined `/context` HTTP endpoint and `mesh_context` MCP tool to retrieve and format system prompt context in a single optimized pass.
- **MCP Stdio Protocol Safety**: Implemented thread-safe `_safe_redirect_stdout_to_stderr()` in `mesh/mcp_server.py`, preventing stdout prints from corrupting JSON-RPC stdio communication.

### [0.5.0] - 2026-10-15

#### Major Architecture Evolution
- **ChromaDB Removal & Unified SQLite Architecture**: Completely replaced the legacy dual-database architecture (ChromaDB + SQLite) with a single, unified SQLite WAL database (`mesh_<namespace>.db`) storing metadata, full-text FTS5 tables, and normalized `float32` vector BLOBs together.
- **3-Stage Hybrid Search Engine**: Dense vector SIMD cosine (`cache.matrix @ q_vec`), lexical BM25 ranking (SQLite FTS5), and exact token overlap boost.
- **Automatic Deduplication Engine**: Near-identical facts ($\ge 0.985$ cosine similarity) are reinforced in-place rather than creating duplicate records.
- **Contradiction Detection & Tracking**: Conflicting memories ($\ge 0.82$ similarity with numeric shifts or negation polarity differences) are stored safely while recording a structured conflict entry.
- **Time-to-Live (TTL) & Linear Confidence Decay**: Time-sensitive memories decay linearly towards staleness (`STALE_THRESHOLD = 0.3`) using `ttl_days` and `decay_start_at`.
- **10 Full MCP Server Tools**: `mesh_learn`, `mesh_recall`, `mesh_update`, `mesh_forget`, `mesh_inspect`, `mesh_context`, `mesh_patterns`, `mesh_contradictions`, `mesh_resolve_contradiction`, `mesh_sync`.
- **Modern Web Dashboard**: Zero-build single-file web interface (`http://localhost:7433`) with dark/light mode, real-time hybrid search, contradiction manager, pattern analysis, and live audit viewer.
- **Passive Log Capture (`mesh-capture`)**: Extracts durable learnings and facts from chat transcripts, terminal outputs, and build logs.
- **Session Digests (`mesh-digest`)**: Automated Markdown summaries of newly stored knowledge, patterns, and contradictions.

### [0.4.4] - 2026-05-13

#### Changed
- Restructured project layout into standard module layout with `hatchling` build backend.
- Resolved Windows CP1252 terminal encoding exceptions during CLI execution.
- Added comprehensive unit and integration test suites (`tests/test_memory.py`, `tests/test_v03.py`, `tests/test_v04.py`).

### [0.3.0] - 2026-05-13

#### Added
- **Privacy Flags (`local_only=True`)**: Mark sensitive memories to guarantee they are never exported or synced across shared instances.
- **Namespace Export & Import (`mesh-export`, `mesh-import`)**: JSON-based backup and migration supporting `skip_existing` and `overwrite` strategies.
- **Namespace Management CLI (`mesh-namespaces`)**: Commands to inspect, list, calculate statistics, rename, and delete namespaces.

### [0.1.0] - 2026-05-13

#### Added
- Initial public release of Mesh: high-speed shared semantic & hybrid memory layer for AI agents.

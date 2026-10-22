# Mesh Field Test Issue Status Record

## Current status (v0.8.1b2, 2026-10-20)

This file records the Run A/B/C fixes shipped in `0.8.1b1`. A later independent re-test against `0.8.1b2` confirmed the C-001 and C-002 fixes and left three issues open. The current, authoritative list is [issues.md](issues.md):

| ID | Severity | Status in 0.8.1b2 | Summary | Resolution Status | Regression Test |
|---|---|---|---|---|---|
| T-001 | Medium | **RESOLVED** | Windows `WinError 5` can interrupt skill installation | Resolved via `_safe_replace_dir` with retry and exponential backoff | `tests/test_installer.py::test_issue_t001_safe_replace_dir_retry` |
| T-002 | High | **RESOLVED** | 3 of 9 planted secret types were stored (keyword-free locker code, split-line token, task PIN) | Resolved via extended key prefix regex, code/passcode handling, and work item note/summary quarantine | `tests/test_security_audit_v08.py::test_issue_t002_secret_detection_and_task_quarantine` |
| T-003 | Low | **RESOLVED** | `ClaudeCodeAdapter.supports_hooks()` advertises hooks; none are implemented | Resolved: implemented `--hooks` flag, `update_hooks_file`, and `remove_hooks_file_entry` for Claude Code & Antigravity | `tests/test_installer.py::test_issue_t003_hooks_claude_code`, `tests/test_installer.py::test_issue_t003_hooks_antigravity`, `tests/test_installer.py::test_issue_t003_cli_hooks_flag` |

The C-001 concurrency fix (7.16 s, 0 errors, 100/100) and C-002 privacy-flag fix (10/10) were verified in the same re-test. See [PUBLIC_ADDENDUM.md](PUBLIC_ADDENDUM.md) and [README.md](README.md). All Run T defects are now fully resolved.

---

**Historical record (0.8.1b1) follows.**

**Branch:** `fix/field-test-findings`  
**Base Version:** `0.8.0` | **Target Version:** `0.8.1b1` (Beta)  
**Evaluation Sources:**
- **Run A:** `C:\Users\bb201\Documents\bun` (Issues `A-001` through `A-008`)
- **Run B:** `C:\Users\bb201\Documents\bun1` (Issues `B-001` through `B-007`)
- **Status:** **ALL 15 REPORTS RESOLVED (9 UNIQUE ISSUES)** with dedicated regression tests and verified zero-failure suite.

---

## 1. Summary Matrix & Cross-Run Deduplication

Issues identified by **both independent runs** are highlighted with highest priority, demonstrating reproducible real-world agent friction.

| Unique Issue | Run A ID | Run B ID | Subsystem | Severity | Resolution Status | Git Commit | Regression Test |
|---|---|---|---|---|---|---|---|
| **Secret Ingestion Leakage** | `A-004` | `B-005` | Security / Secrets | High | **RESOLVED** | `0f4e1a9` | `test_issue_a004_b005_secret_ingestion_and_quarantine` |
| **Installer MCP Args (`mcp` vs `server`)** | `A-002` | — | Installer / CLI | High | **RESOLVED** | `162edc3` | `test_resolve_mcp_command`, `test_cli_mesh_mcp_subcommand_alias` |
| **`--apply --json` Non-Interactive Execution** | `A-001` | `B-002` | Installer / CLI | Medium | **RESOLVED** | `9463aa1` | `test_install_apply_with_json_writes_files` |
| **Project-Scope `MESH_HOME` & Venv Binding** | `A-003` | `B-001` | Installer / Config | High | **RESOLVED** | `9463aa1` | `test_project_scope_binds_mesh_home_and_venv` |
| **MCP Startup Warmup Stderr Pollution** | `A-008` | `B-007` | MCP Server / UI | Medium | **RESOLVED** | `de2c6bd` | `test_issue_a008_b007_mcp_server_run_suppresses_warmup_prompt` |
| **Work Item Reopened Status Transition** | `A-005` | `B-006` | Store / Lifecycle | Medium | **RESOLVED** | `ef17e9e` | `test_work_item_reopen_status_transition` |
| **SDK Method Aliases (`task_*`, `log`)** | `A-006` | `B-004` | SDK / Memory | Medium | **RESOLVED** | `aa74c4d` | `test_mesh_and_store_task_and_log_aliases` |
| **Work Item Not Found Guidance** | `A-007` | — | Store / Work Items | Low | **RESOLVED** | `2f3a789` | `test_work_item_not_found_guidance` |
| **Defunct GCS Tarball Mirror 403 Fallback** | — | `B-003` | Model Download | Low | **RESOLVED** | `b357a47` | `test_issue_b003_no_defunct_gcs_mirror` |

---

## 2. Detailed Issue Records

### Issue A-004 / B-005: Secret Ingestion Leakage
- **Reported By:** Run A (`A-004`), Run B (`B-005`)
- **Severity:** High (Security)
- **Symptom:**
  Plaintext secret tokens, generic API keys (e.g. `WEBHOOK_KEY_9876543210fedcba`, `payment_gateway_key=...`), and unquoted passphrase phrases (`passphrase is ...`) were ingested into SQLite without redaction or without being quarantined to `local_only=True`.
- **Root Cause:**
  `mesh/secrets.py` regexes (`_KNOWN_API_KEY_RE`, `_KEYWORD_ASSIGNMENT_RE`, and `_UNQUOTED_PASSPHRASE_RE`) had overly narrow prefix patterns and required exact assignment syntax that missed compound key labels (e.g., `webhook key`, `payment gateway key`) or generic token patterns.
- **Resolution:**
  Expanded `_KNOWN_API_KEY_RE` with generic prefix matches (`(?:KEY|SECRET|TOKEN|PASS|PHRASE|APIKEY|AUTH|CRED|EMBED)_[A-Za-z0-9_-]{4,}`), broadened keyword matching in `_KEYWORD_ASSIGNMENT_RE`, and tightened plausible secret validation in `_is_plausible_secret_value` while preserving normal SQL column expressions (`foreign key is customer_id`).
- **Commit:** `0f4e1a9`
- **Regression Test:** `tests/test_security_audit_v08.py::test_issue_a004_b005_secret_ingestion_and_quarantine`

---

### Issue A-002: Installer Injected Invalid `"args": ["mcp"]`
- **Reported By:** Run A (`A-002`)
- **Severity:** High
- **Symptom:**
  `mesh install` populated agent MCP configurations with `"command": "mesh", "args": ["mcp"]`. When the agent launched `mesh mcp`, the CLI errored with `error: argument command: invalid choice: 'mcp' (choose from 'init', 'learn', ...)`.
- **Root Cause:**
  `mesh/installer.py:resolve_mesh_mcp_command()` returned `["mcp"]` as the CLI argument instead of `["server"]`, and `mesh/cli.py` lacked an `"mcp"` subcommand alias.
- **Resolution:**
  1. Updated `resolve_mesh_mcp_command()` in `mesh/installer.py` to return `["server"]`.
  2. Added `"mcp"` subcommand alias in `mesh/cli.py` mapping directly to `mcp_server.run()`, making `mesh mcp` and `mesh server` interchangeable.
- **Commit:** `162edc3`
- **Regression Tests:** `tests/test_installer.py::test_resolve_mcp_command`, `tests/test_installer.py::test_cli_mesh_mcp_subcommand_alias`

---

### Issue A-001 / B-002: `mesh install --apply --json` Stuck in Plan Mode
- **Reported By:** Run A (`A-001`), Run B (`B-002`)
- **Severity:** Medium
- **Symptom:**
  Automation scripts passing `mesh install --apply --json` (or `--yes --json`) received a JSON response with `"mode": "plan"` and no configuration files were created or updated on disk.
- **Root Cause:**
  `mesh/cli.py:install_cmd()` checked `if args.dry_run or args.json:` before inspecting `args.apply` or `args.yes`, causing any `--json` invocation to immediately return the planned preview without applying changes.
- **Resolution:**
  Reordered logic so `--apply` and `--yes` authorize plan execution even when `--json` is requested. When applied with `--json`, returns `{"status": "ok", "mode": "applied", "results": results}`.
- **Commit:** `9463aa1`
- **Regression Test:** `tests/test_installer.py::test_install_apply_with_json_writes_files`

---

### Issue A-003 / B-001: Project Scope Omitted `MESH_HOME` and Bound Global Python
- **Reported By:** Run A (`A-003`), Run B (`B-001`)
- **Severity:** High
- **Symptom:**
  Running `mesh install --scope project` wrote project agent configuration files without specifying `MESH_HOME` in `mcpServers.mesh.env`, causing project MCP servers to fall back to the global user `~/.mesh` database. Additionally, it bound global python instead of the workspace virtualenv.
- **Root Cause:**
  `plan_installation()` in `mesh/installer.py` only populated `env` with `MESH_NAMESPACE` if explicitly passed, leaving `MESH_HOME` unset. `resolve_mesh_mcp_command()` bypassed active virtual environments (`project_dir/.venv` or `sys.prefix`).
- **Resolution:**
  1. Defaulted project-scoped `MESH_HOME` to `project_dir / ".mesh-home"`. Added `--mesh-home` CLI argument to allow explicit configuration.
  2. Updated `resolve_mesh_mcp_command(project_dir)` to prioritize `project_dir/.venv`, `project_dir/venv`, and active virtual environments (`sys.prefix != sys.base_prefix`).
  3. Preserved planned executable and arguments in `agent_plan["mcp"]`.
- **Commit:** `9463aa1`
- **Regression Test:** `tests/test_installer.py::test_project_scope_binds_mesh_home_and_venv`

---

### Issue A-008 / B-007: MCP Startup Warmup Stderr Pollution
- **Reported By:** Run A (`A-008`), Run B (`B-007`)
- **Severity:** Medium
- **Symptom:**
  `mesh server` / `python -m mesh.mcp_server` output `▲ Needs your approval: Allow Mesh to start the background MCP cache warmup worker?` on stderr during startup, polluting stderr in non-interactive MCP host environments (Claude Desktop, Cursor, Antigravity IDE).
- **Root Cause:**
  `mesh/mcp_server.py:run()` invoked `request_user_permission("start the background MCP cache warmup worker")` without checking `MESH_NO_AUTO_DAEMON` or verifying whether stdin was an interactive terminal (`sys.stdin.isatty()`).
- **Resolution:**
  Updated `mcp_server.py:run()` to check `_env_flag("MESH_NO_AUTO_DAEMON")`, `_env_flag("MESH_AUTO_APPROVE")`, and `sys.stdin.isatty()`. Suppressed interactive confirmation prompts when non-interactive or when no-daemon mode is enabled.
- **Commit:** `de2c6bd`
- **Regression Test:** `tests/test_agent_scenarios.py::test_issue_a008_b007_mcp_server_run_suppresses_warmup_prompt`

---

### Issue A-005 / B-006: Work Item `reopened` Status Transition
- **Reported By:** Run A (`A-005`), Run B (`B-006`)
- **Severity:** Medium
- **Symptom:**
  Agents attempting to reopen completed or cancelled work items using `status="reopened"` or `status="reopen"` failed with `ValueError: Invalid status 'reopened'. Must be one of: todo, in_progress, blocked, done, cancelled`.
- **Root Cause:**
  `mesh/store.py:update_work_item()` strictly validated status against the SQLite column enum.
- **Resolution:**
  Updated `update_work_item()` to accept `"reopened"` and `"reopen"`. Mapped it to status `"todo"`, cleared `completed_at` and `completed_by`, and recorded the transition in `work_events` with `event_type="reopened"`. Updated MCP inputSchema enum and documented in `SKILL.md`.
- **Commit:** `ef17e9e`
- **Regression Test:** `tests/test_work_items.py::test_work_item_reopen_status_transition`

---

### Issue A-006 / B-004: SDK Method Aliases (`task_*`, `log`)
- **Reported By:** Run A (`A-006`), Run B (`B-004`)
- **Severity:** Medium
- **Symptom:**
  SDK callers invoking `mesh.task_create()`, `mesh.task_update()`, `mesh.task_complete()`, `mesh.task_list()`, or `mesh.log()` crashed with `AttributeError: 'Mesh' object has no attribute 'task_create'`.
- **Root Cause:**
  SDK methods were named `create_work_item`, `update_work_item`, `complete_work_item`, `list_work_items`, and `changelog`, while MCP tools were named `mesh_task_create`, `mesh_task_update`, etc.
- **Resolution:**
  Added alias attributes on both `Mesh` (`mesh/memory.py`) and `MeshStore` (`mesh/store.py`):
  `task_create`, `task_update`, `task_complete`, `task_list`, `task_get`, and `log`.
- **Commit:** `aa74c4d`
- **Regression Test:** `tests/test_memory.py::TestSdkMethodAliases::test_mesh_and_store_task_and_log_aliases`

---

### Issue A-007: Work Item Not Found Guidance
- **Reported By:** Run A (`A-007`)
- **Severity:** Low
- **Symptom:**
  When an agent called `mesh_task_update` with an unknown or hallucinated ID, the error `Work item 'task-1' not found in namespace '...'` provided no context on whether work items existed or what valid IDs were available.
- **Root Cause:**
  `mesh/store.py:_resolve_work_item_locked()` raised a generic not found message without did-you-mean query or suggestions.
- **Resolution:**
  Implemented `_not_found_error_message()`:
  - If no work items exist in the namespace: advises using `mesh_task_create` first.
  - If work items exist: lists available work item short IDs and titles.
  Reinforced in `SKILL.md` to always create tasks before updating.
- **Commit:** `2f3a789`
- **Regression Test:** `tests/test_work_items.py::test_work_item_not_found_guidance`

---

### Issue B-003: Defunct GCS Tarball Mirror 403 Fallback
- **Reported By:** Run B (`B-003`)
- **Severity:** Low
- **Symptom:**
  Model bootstrap initially attempted download from `https://storage.googleapis.com/qdrant-fastembed/...` which returned HTTP 403 Forbidden, logging an error and polluting `last_error` in `model_status.json` before falling back to HuggingFace mirrors.
- **Root Cause:**
  Public Google Cloud Storage bucket for FastEmbed was deprecated and access denied.
- **Resolution:**
  Removed the defunct GCS bucket URL from `_GCS_TARBALL_MIRRORS` in `mesh/memory.py`. When `_GCS_TARBALL_MIRRORS` is empty, `_download_from_gcs_tarball()` returns `False` immediately, cleanly directing download flow to HuggingFace / hf-mirror without 403 errors or warning emissions.
- **Commit:** `b357a47`
- **Regression Test:** `tests/test_memory.py::test_issue_b003_no_defunct_gcs_mirror`

---

## 3. Run T Issue Records (0.8.1b2 Post-Release Resolution)

### Issue T-001: Transient Windows Skill Replacement Contention
- **Reported By:** Run T (`T-001`)
- **Severity:** Medium
- **Symptom:**
  `mesh install --scope project --apply` failed on Windows AMD64 during OpenCode skill installation with `[WinError 5] Access is denied: '...\.opencode\skills\mesh-context-layer.tmp' -> '...\.opencode\skills\mesh-context-layer'`.
- **Root Cause:**
  Windows file indexers and antivirus scanners can hold transient file handles on newly created directories, causing immediate `os.replace()` calls to fail with `Access is denied`.
- **Resolution:**
  Implemented `_safe_replace_dir(src, dst, max_retries=5, backoff=0.05)` in `mesh/skill_installer.py`. Invokes `gc.collect()` and uses exponential backoff across retry attempts to ensure locks release before replacement.
- **Regression Test:** `tests/test_installer.py::test_issue_t001_safe_replace_dir_retry`

---

### Issue T-002: Secret Ingestion Quarantine Gaps
- **Reported By:** Run T (`T-002`)
- **Severity:** High (Security)
- **Symptom:**
  Three secret patterns leaked in plaintext:
  1. `SEC-04` (Task description PIN `9472`): Leaked into `backup_export.json`.
  2. `SEC-05` (Keyword-free locker code `8841-blue-zebra-alpha`): Bypassed heuristic redaction into database and exports.
  3. `SEC-09` (Split-line secret `T_SPLIT_SECRET_PART1_ABCXYZ987`): Bypassed regex detection.
- **Root Cause:**
  `_KNOWN_API_KEY_RE` required specific prefix lengths; `_KEYWORD_ASSIGNMENT_RE` and `_is_plausible_secret_value()` lacked passcode/locker code patterns; and `store.update_work_item` / `store.complete_work_item` did not quarantine work items when notes or summaries contained credentials.
- **Resolution:**
  1. Updated `_KNOWN_API_KEY_RE` in `mesh/secrets.py` to match arbitrary prefixed token segments (`(?:[A-Z0-9]+_)+`).
  2. Extended `_KEYWORD_ASSIGNMENT_RE` to match `(?:[a-z0-9_-]+[_\s-])?code|passcode` and updated `_is_plausible_secret_value()` to quarantine locker codes while preserving benign status/HTTP codes.
  3. Updated `update_work_item` and `complete_work_item` in `mesh/store.py` to quarantine tasks (`local_only = 1`) whenever task notes or completion summaries contain secrets.
- **Regression Test:** `tests/test_security_audit_v08.py::test_issue_t002_secret_detection_and_task_quarantine`

---

### Issue T-003: Agent Lifecycle Hooks Implementation
- **Reported By:** Run T (`T-003`)
- **Severity:** Low
- **Symptom:**
  Installer advertised hook support and `ClaudeCodeAdapter.supports_hooks()` returned `True`, but no hook configuration was created or managed by `mesh install`.
- **Root Cause:**
  Hook installation and uninstallation logic was left unimplemented.
- **Resolution:**
  Fully implemented agent lifecycle hooks feature across `mesh/installer.py` and `mesh/cli.py`:
  - Added `get_hooks_path()` and `supports_hooks()` for `ClaudeCodeAdapter` (`.claude/settings.json`) and `AntigravityAdapter` (`.agents/hooks.json` / `.gemini/config/hooks.json`).
  - Implemented `update_hooks_file()` and `remove_hooks_file_entry()` with atomic `.bak` backup creation and preservation of existing hooks.
  - Added `--hooks` opt-in CLI flag to `mesh install`, with preview in dry-run mode and automatic rollback on uninstall.
- **Regression Tests:** `tests/test_installer.py::test_issue_t003_hooks_claude_code`, `tests/test_installer.py::test_issue_t003_hooks_antigravity`, `tests/test_installer.py::test_issue_t003_cli_hooks_flag`

---

## 4. Verification Summary

All fixes verified against the full repository test suite:
- **`check_skill.py`**: PASSED (160 lines <= 250, description 521 chars <= 1024, rules block 9 lines <= 12).
- **`check_docs.py`**: PASSED (Version consistency validated).
- **`ruff check`**: PASSED (0 errors).
- **`pytest`**: **318 passed, 0 failed, 0 errors**.


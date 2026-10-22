# Mesh Issue Log — Run T (v0.8.1b2)

This issue log details the findings from independent re-test Run T against `mesh_context_layer-0.8.1b2-py3-none-any.whl` (SHA-256: `e1a7a0af71abe3f51b02150d9a2d5fdc1d9d8a618937e9d41bf899c08ead06ff`).

---

## 1. Status of Previously Reported Defects

| Issue ID | Previous Run | Defect Description | Run T Status | Evidence / Verification Method |
|---|---|---|:---:|---|
| **C-001** | Run C | In-process SQLite connection sharing causes `SQLITE_MISUSE` under thread contention | **FIXED** | 8 writers / 8 readers stress passed 0 errors; 8/8 packaged tests pass in `test_thread_safety_c001.py` |
| **C-002** | Run C | `Mesh.learn()` return payload omits `"local_only"` boolean flag | **FIXED** | 10/10 assertions pass across SDK, HTTP, MCP, and CLI in `test_learn_local_only_c002.py` |
| **A-001 / B-002** | Run A/B | `mesh install --apply --json` stuck in dry-run plan mode | **FIXED** | Tested in `test_regression_sweep.py`: RC=0, returns `"mode": "applied"`, writes files |
| **A-002** | Run A | Generated MCP configuration used invalid `"args": ["mcp"]` | **FIXED** | Resolved args verified as `["server"]`; aliases `mesh mcp` and `mesh server` functional |
| **A-003 / B-001** | Run A/B | Project scope did not bind virtualenv python or isolated `MESH_HOME` | **FIXED** | Generated config points directly to `.venv/Scripts/mesh.exe` with explicit project `MESH_HOME` |
| **A-004 / B-005** | Run A/B | Secret passphrases and webhook keys leaked into SQLite and exports | **FIXED** | Standard tokens/passphrases/API keys cleanly redacted and set `local_only=True` |
| **A-005 / B-006** | Run A/B | `update_work_item(status='reopened')` crashed with `ValueError` | **FIXED** | Verified transition to `todo` with event recorded in event stream |
| **A-006 / B-004** | Run A/B | SDK work item method names diverged from MCP tools (`task_*`, `log`) | **FIXED** | Native aliases `task_create`, `task_update`, `task_complete`, `task_list`, `log` operational |
| **A-007** | Run A | Rejection of unknown work item IDs without actionable guidance | **FIXED** | Verified informative error message suggesting available short IDs |
| **A-008 / B-007** | Run A/B | Headless MCP server warmup prompt polluted stderr on startup | **FIXED** | Verified clean stderr (0 bytes) in non-interactive startup |
| **B-003** | Run B | Defunct GCS ONNX model mirror returned HTTP 403 | **FIXED** | Defunct GCS mirror removed; multi-mirror fallback verified |

---

## 2. Newly Discovered Issues in Run T

### Issue T-001: Transient Windows Directory Replace Failure During Skill Installation (`[WinError 5] Access is denied`)
- **Severity**: Medium
- **Category**: Installer / Windows Platform Compatibility
- **Status**: **RESOLVED**
- **Label**: MEASURED ([`report/raw/mesh_install_apply.txt`](raw/mesh_install_apply.txt))
- **Description**: During the initial execution of `mesh install --scope project --apply` on Windows AMD64, installing the OpenCode skill failed with:
  ```
  SKILL: FAIL: [WinError 5] Access is denied: '...\.opencode\skills\mesh-context-layer.tmp' -> '...\.opencode\skills\mesh-context-layer'
  ```
- **Root Cause**: In `mesh/skill_installer.py:256`, the installer executes `os.replace(tmp_dst, dst)` immediately following `shutil.copytree` and metadata write operations. On Windows, antivirus scanners, file indexers, or transient filesystem locks can delay handle release, causing `os.replace` on directories to raise `Access is denied`. A subsequent rerun succeeded.
- **Suggested Fix**: Wrap `os.replace(tmp_dst, dst)` in a brief retry loop (e.g., 3 attempts with 50ms exponential backoff and `gc.collect()`), standard for atomic directory renames on Windows.
- **Resolution**: Implemented `_safe_replace_dir(src, dst, max_retries=5, backoff=0.05)` in `mesh/skill_installer.py` with exponential backoff and explicit `gc.collect()` before retrying atomic replacement.
- **Regression Test**: `tests/test_installer.py::test_issue_t001_safe_replace_dir_retry`

---

### Issue T-002: Keyword-Free Credentials, Split-Line Tokens, and Bare Task PINs Bypass Secret Quarantine
- **Severity**: High
- **Category**: Security / Secret Detection
- **Status**: **RESOLVED**
- **Label**: MEASURED ([`report/data/secret_audit_results.json`](data/secret_audit_results.json))
- **Description**: A fresh secrets audit evaluating 9 distinct fake secret patterns planted across SDK, CLI, MCP, and task description surfaces revealed 3 plaintext leaks:
  1. `SEC-04` (Short PIN `9472` in task description): Exported in plaintext into `backup_export.json`.
  2. `SEC-05` (Keyword-free locker code `8841-blue-zebra-alpha` in `mesh.learn`): Bypassed heuristic redaction and leaked into raw SQLite database files, WAL logs, `brief_output.md`, and `backup_export.json`.
  3. `SEC-09` (Split-line secret `T_SPLIT_SECRET_PART1_ABCXYZ987` across newlines): Bypassed regex detection and leaked into raw SQLite database files, WAL logs, `brief_output.md`, and `backup_export.json`.
- **Finding Statement**: Exactly **6 of 9 tested secret patterns** were protected against plaintext leakage; 3 leaked in plaintext.
- **Impact**: While formatted API keys, database connection URIs, CLI flags, JSON/YAML payloads, and natural passphrases are securely redacted, credentials without explicit keywords, multiline keys, and task descriptions remain susceptible to plaintext persistence.
- **Suggested Fix**:
  - Add multiline regex evaluation (`re.MULTILINE | re.DOTALL`) for tokens spanning line breaks.
  - Apply entropy-based token detection in addition to keyword-based regex matching for sentence-style credentials.
  - Subject task descriptions and notes to the same automatic redaction filter as `learn` payloads.
- **Resolution**:
  1. Updated `_KNOWN_API_KEY_RE` in `mesh/secrets.py` to match arbitrary prefixed token segments (`(?:[A-Z0-9]+_)+`).
  2. Added code/passcode matching (`(?:[a-z0-9_-]+[_\s-])?code|passcode`) to `_KEYWORD_ASSIGNMENT_RE` and added code pattern validation in `_is_plausible_secret_value()` to quarantine locker codes while preserving benign status/HTTP codes.
  3. Updated `update_work_item` and `complete_work_item` in `mesh/store.py` to quarantine tasks (`local_only = 1`) whenever task notes or completion summaries contain secrets.
- **Regression Test**: `tests/test_security_audit_v08.py::test_issue_t002_secret_detection_and_task_quarantine`

---

### Issue T-003: Unimplemented Agent Hooks in Package Installer
- **Severity**: Low
- **Category**: Packaging / Completeness
- **Status**: **RESOLVED**
- **Label**: MEASURED (`mesh/installer.py`)
- **Description**: The installer module header advertises "Multi-Agent MCP Server, Skill, Rules, and Hooks Installer", and `ClaudeCodeAdapter.supports_hooks()` returns `True`. However, no hook file templates or hook installation routines are implemented or executed during `mesh install`.
- **Suggested Fix**: Either implement the documented hook generation for supporting agent runtimes or update module documentation to reflect current scope.
- **Resolution**: Fully implemented agent lifecycle hooks feature across `mesh/installer.py` and `mesh/cli.py`.
  - Added `get_hooks_path()` and `supports_hooks()` for `ClaudeCodeAdapter` (`.claude/settings.json`) and `AntigravityAdapter` (`.agents/hooks.json` / `.gemini/config/hooks.json`).
  - Implemented `update_hooks_file()` and `remove_hooks_file_entry()` with atomic `.bak` backup creation and preservation of existing hook entries.
  - Added `--hooks` opt-in CLI flag to `mesh install`, with preview in dry-run mode and automatic rollback on uninstall.
- **Regression Tests**: `tests/test_installer.py::test_issue_t003_hooks_claude_code`, `tests/test_installer.py::test_issue_t003_hooks_antigravity`, `tests/test_installer.py::test_issue_t003_cli_hooks_flag`


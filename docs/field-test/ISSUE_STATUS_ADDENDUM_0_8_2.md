# Field Test Issue Status — Post-0.8.1 Findings Addendum (v0.8.2)

**Branch:** `fix/post-0.8.1`  
**Base Version:** `0.8.1` (`a5b40ab6741f4de4b0909a5e27ac85382aa6ca59`) | **Target Version:** `0.8.2` (Beta)  
**Evaluation Scope:** Post-0.8.1 external review and probe verification findings (F1–F8).

This addendum records the resolution of findings identified after the publication of Mesh `0.8.1`. All changes are non-breaking, maintain 100% database schema backward compatibility (schema version 3), and preserve full isolated test suite stability.

---

## 1. Summary Status Table

| Finding | Severity | Subsystem | Status | Commit | Regression / Verification Test |
|---|---|---|---|---|---|
| **F1** — Claude Code MCP config path (`.mcp.json`) & legacy migration | Medium | Installer / CLI | **RESOLVED** | `fe6f512` | `tests/test_installer.py`, `tests/test_doctor_scope_precedence.py` |
| **F2** — Decision rationale tracking, frontmatter triggers, update-in-place | Low | Skill / Store / Brief | **RESOLVED** | `dcf8027` | `tests/test_work_items.py`, `scripts/check_skill.py` |
| **F3** — Cryptographic docs alignment & field-test provenance | Low | Documentation / Linter | **RESOLVED** | `aa08b20` | `scripts/check_docs.py` (`check_no_fernet_references`) |
| **F5** — Restricted publish workflow trigger (release event only) | Medium | CI / Release Security | **RESOLVED** | `5fc0d57` | Workflow syntax & dispatch inspection |
| **F6** — Code ownership and repository hygiene | Low | Governance / Hygiene | **RESOLVED** | `609c3ac` | `.github/CODEOWNERS` verified; scratch tree deleted |
| **F7** — Redundant CI test workflow removed | Low | CI / Automation | **RESOLVED** | `c750590` | `git rm .github/workflows/test.yml` |
| **F8** — Hook contract, silent brief, credential idempotence, lock retry | Medium | Security / CLI / Hooks | **RESOLVED** | `b69439b` | `tests/test_secrets_corpus.py` (8 tests), `tests/test_installer.py` |

---

## 2. F1 — Claude Code MCP Configuration Path & Legacy Migration

- **Finding:** In 0.8.1, `ClaudeCodeAdapter` configured project-scoped MCP servers in `.claude/mcp.json`. Claude Code's official project configuration standard requires repository-root `.mcp.json`. Additionally, CLI aliases `claude` and `gemini` were unmapped, and unverified agent adapters lacked visual distinction.
- **Root Cause:** Adapter path logic placed all Claude configurations under the hidden `.claude/` directory rather than separating user scope (`~/.claude.json`) from project scope (`.mcp.json`).
- **Resolution (`fe6f512`):**
  1. Updated `ClaudeCodeAdapter.get_mcp_config_path` to return `project_dir / ".mcp.json"` for project scope, and `Path.home() / ".claude.json"` for user scope.
  2. Implemented automatic detection and migration of legacy `.claude/mcp.json` configs with prompt to the user and backup creation.
  3. Added `_AGENT_ALIAS_MAP` in `mesh/installer.py` supporting `claude` -> `claude-code` and `gemini` -> `gemini-cli`.
  4. Added `verified` flag on adapters; unverified agents display `(unverified)` in `mesh install --dry-run` and `mesh doctor`.
  5. Added trust notification note when generating project-scope `.mcp.json` files for Claude Code.
- **Test Coverage:** `tests/test_installer.py` (`test_claude_code_paths`, `test_claude_code_legacy_migration`, `test_agent_alias_resolution`), `tests/test_doctor_scope_precedence.py` (all 27 tests passing).

---

## 3. F2 — Decision Rationale Tracking & Presentation Flagging

- **Finding:** Automated probe followups indicated agent decision records often omitted rationale, reducing context fidelity for collaborating agents. Also, skill frontmatter triggers and port update-in-place instructions required clarification.
- **Root Cause:** Decision capture rules did not strictly mandate causal reason markers (`because`, `due to`, `in order to`), and `rules_block.md` lacked an explicit instruction on update-in-place vs appending new memories.
- **Resolution (`dcf8027`):**
  1. Updated `mesh/skills/mesh-context-layer/rules_block.md` Rule 3 to require rationale and Rule 4 to specify updating changed configuration in place rather than appending. (Maintained exact 9 lines, <= 12 line limit).
  2. Synchronized `README.md` embedded rules block byte-for-byte with `rules_block.md`.
  3. Updated `SKILL.md` frontmatter with imperative instructions ("ALWAYS query mesh_recall before asking user...") and added causal explanation guidance (162 lines, <= 250 line limit).
  4. Updated `get_project_brief` in `mesh/store.py` to annotate decision records lacking causal indicators with ` *(needs rationale — add 'because <reason>')*`. Zero database schema changes.
- **Test Coverage:** `tests/test_work_items.py` (`test_brief_decision_rationale_flagging`), `scripts/check_skill.py`, `scripts/check_docs.py`.

---

## 4. F3 — Cryptographic Documentation & Provenance Alignment

- **Finding:** `README.md:28` and `SECURITY.md:68` referenced "Fernet" encryption, whereas `mesh/crypto.py` implements AES-256-GCM envelope encryption and an stdlib BLAKE2b/HMAC Encrypt-then-MAC fallback. In addition, the verification footer and baseline artifact notes needed clarification to distinguish 0.8.1b2 field-test evidence from 0.8.2 code.
- **Root Cause:** Historical documentation artifact from pre-v0.6 design was never updated when authenticated envelope encryption was implemented.
- **Resolution (`aa08b20`):**
  1. Updated `README.md` table and `SECURITY.md` to specify: "application-level AES-256-GCM envelope (with stdlib BLAKE2b/HMAC Encrypt-then-MAC fallback, enhanced when [crypto] is installed)".
  2. Clarified `README.md` comparison table line 33 and footer line 782 with explicit distinction between field-test provenance (`0.8.1b2` on Windows 11) and current release (`0.8.2`).
  3. Added `check_no_fernet_references(errors)` in `scripts/check_docs.py` to ensure stale crypto terminology does not regress.
- **Test Coverage:** `scripts/check_docs.py` and `scripts/check_skill.py`.

---

## 5. F5 — Restricted PyPI Publication Trigger

- **Finding:** `.github/workflows/publish.yml` triggered on git tag pushes (`push: tags: ['v*']`), creating a risk of premature package publishing before release artifacts and notes are finalized.
- **Root Cause:** Standard starter GitHub Action template configured wildcard tag trigger alongside release trigger.
- **Resolution (`5fc0d57`):**
  1. Removed `push: tags: ['v*']` from `publish.yml`.
  2. Restricted publication strictly to GitHub release events (`release: types: [published]`) and manual `workflow_dispatch`.
  3. Updated release documentation in `DEVELOPER_GUIDE.md` §8 to document required GitHub release creation.
- **Test Coverage:** Workflow file YAML validation and syntax verification.

---

## 6. F6 — Repository Hygiene & Code Ownership

- **Finding:** Repository lacked a `.github/CODEOWNERS` file, and an unwanted scratch directory (`unwanted/progress.md`) remained untracked in the workspace.
- **Resolution (`609c3ac`):**
  1. Created `.github/CODEOWNERS` declaring `* @billighost`.
  2. Deleted `unwanted/progress.md` and removed the `unwanted/` directory.

---

## 7. F7 — Redundant CI Workflow Consolidation

- **Finding:** Repository contained both `.github/workflows/ci.yml` and `.github/workflows/test.yml`. `test.yml` ran a subset of CI steps and was redundant with the matrix testing in `ci.yml`.
- **Resolution (`c750590`):**
  1. Removed `.github/workflows/test.yml` using `git rm`.
  2. Retained comprehensive cross-platform matrix testing (Ubuntu, macOS, Windows; Python 3.10–3.13) in `.github/workflows/ci.yml`.

---

## 8. F8 — Hook Contract, Silent Brief Handling, Credential Idempotence & Windows Lock Retry

- **Finding:**
  1. Antigravity hook integration required JSON response structure `{"injectSteps": [{"ephemeralMessage": ...}]}` under `PreInvocation`.
  2. `mesh brief` threw uncaught exceptions when storage failed, which could break host agent startup.
  3. Redacting already-redacted text (`[REDACTED_...]` or `[SECRET_REF:...]`) re-flagged the string on subsequent passes.
  4. `_safe_replace_dir` needed exhaustion handling and test coverage.
- **Root Cause:**
  1. Hook adapter formatted output as plain text rather than Antigravity hook step envelope.
  2. CLI brief command lacked top-level exception suppression for agent invocation paths.
  3. Secret regexes `_AUTH_HEADER_RE` and `_YAML_BLOCK_SECRET_RE` lacked negative lookaheads for existing redaction tokens.
- **Resolution (`b69439b`):**
  1. Updated `brief_cmd` in `mesh/cli.py` to support `--format antigravity` returning `{"injectSteps": [{"ephemeralMessage": ...}]}`.
  2. Wrapped `mesh brief` storage calls in graceful error handling with untrusted reference data banners (`<!-- UNTRUSTED REFERENCE DATA: MESH CONTEXT BRIEF -->`).
  3. Added `(?!\[(?:REDACTED|SECRET_REF))` lookahead to secret regexes in `mesh/secrets.py`.
  4. Added `_safe_replace_dir` retry exhaustion test and created comprehensive secret corpus suite `tests/test_secrets_corpus.py`.
- **Test Coverage:** `tests/test_secrets_corpus.py` (8 test methods, all passing), `tests/test_installer.py` (`test_safe_replace_dir_exhaustion`).

---

## 9. Verification & Distribution Metrics (v0.8.2)

- **Test Suite Execution:** 337 passed in 274.94s (Windows 11, Python 3.14.0).
- **Ruff Linter:** 0 errors across entire repository.
- **Skill Checker (`check_skill.py`):** PASSED (162 lines, 505 chars description, 9 lines rules block).
- **Docs Checker (`check_docs.py`):** PASSED (zero broken links, 0 stale references, version stamps 0.8.2).
- **Built Wheel:** `dist/mesh_context_layer-0.8.2-py3-none-any.whl`
  - SHA-256: `33630a6340a68468c754ca660578bcec010a704b0fde7ab9abee17a782045d90`
  - Size: 300,940 bytes
  - Contents: 34 files (0 leaks, 0 binaries).
- **Built Sdist:** `dist/mesh_context_layer-0.8.2.tar.gz`
  - SHA-256: `2b23b3c7afe9dfd6aade95c0231630f651f0bfcff3c4d341554d8d45833e270f`
  - Size: 453,275 bytes
  - Contents: 73 files (0 leaks).
- **Twine Check:** PASSED (`twine check --strict`).

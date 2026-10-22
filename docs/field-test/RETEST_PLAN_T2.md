# Mesh Targeted Re-Test Plan T2 — Post-0.8.1 Findings (v0.8.2)

**Purpose:** Independent black-box re-test of findings F1–F8 against the built `0.8.2` distribution wheel (`dist/mesh_context_layer-0.8.2-py3-none-any.whl`).
**Target Distribution:**
- Wheel: `dist/mesh_context_layer-0.8.2-py3-none-any.whl`
- SHA-256: `33630a6340a68468c754ca660578bcec010a704b0fde7ab9abee17a782045d90`
- Size: 300,940 bytes

**Hard safety rules for testers:**
1. Use a fresh, isolated temporary directory for every test case.
2. Set `MESH_HOME`, `HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `XDG_CONFIG_HOME`, `MESH_NO_AUTO_DAEMON=1`, `MESH_NO_MODEL_DOWNLOAD=1`, `MESH_NO_UPDATE_CHECK=1`.
3. Never point tests at real `~/.mesh` or real agent configs.
4. Do not call cloud providers or enable telemetry.

---

## Environment Setup

### Windows (PowerShell)
```powershell
$base = Join-Path $env:TEMP ("mesh-retest-t2-" + [guid]::NewGuid().ToString("N").Substring(0,8))
$env:MESH_HOME = Join-Path $base "mesh-home"
$env:HOME = Join-Path $base "home"
$env:USERPROFILE = $env:HOME
$env:APPDATA = Join-Path $base "appdata"
$env:LOCALAPPDATA = Join-Path $base "localappdata"
$env:XDG_CONFIG_HOME = Join-Path $base "xdg"
$env:MESH_NO_AUTO_DAEMON = "1"
$env:MESH_NO_MODEL_DOWNLOAD = "1"
$env:MESH_NO_UPDATE_CHECK = "1"
New-Item -ItemType Directory -Force -Path $env:MESH_HOME,$env:HOME,$env:APPDATA,$env:LOCALAPPDATA,$env:XDG_CONFIG_HOME | Out-Null

python -m venv (Join-Path $base "venv")
& (Join-Path $base "venv\Scripts\Activate.ps1")
pip install dist/mesh_context_layer-0.8.2-py3-none-any.whl pytest
```

### Linux / macOS (POSIX)
```bash
BASE=$(mktemp -d -t mesh-retest-t2-XXXXXX)
export MESH_HOME="$BASE/mesh-home"
export HOME="$BASE/home"
export USERPROFILE="$HOME"
export APPDATA="$BASE/appdata"
export LOCALAPPDATA="$BASE/localappdata"
export XDG_CONFIG_HOME="$BASE/xdg"
export MESH_NO_AUTO_DAEMON="1"
export MESH_NO_MODEL_DOWNLOAD="1"
export MESH_NO_UPDATE_CHECK="1"
mkdir -p "$MESH_HOME" "$HOME" "$APPDATA" "$LOCALAPPDATA" "$XDG_CONFIG_HOME"

python3 -m venv "$BASE/venv"
source "$BASE/venv/bin/activate"
pip install dist/mesh_context_layer-0.8.2-py3-none-any.whl pytest
```

---

## Test Procedure 1: C-001 Concurrency Re-Verification

**Objective:** Verify that concurrent read/write operations with multiple threads and processes complete with zero errors and zero database lock violations.

**Execution:**
```bash
pytest tests/test_thread_safety_c001.py -v
```

**Expected Outcome:**
- 8 tests pass in ~25-30 seconds.
- 0 `sqlite3.ProgrammingError`, 0 `sqlite3.InterfaceError`, 0 `IndexError`.
- Multi-process stress test preserves 100/100 tasks and memories without corruption.

---

## Test Procedure 2: T-002 Secret Detection & Idempotent Redaction Corpus

**Objective:** Verify comprehensive secret pattern detection (PINs, passphrases, tokens, private keys) and confirm that already-redacted text is never re-flagged.

**Execution:**
```bash
pytest tests/test_secrets_corpus.py -v
```

**Expected Outcome:**
- 8 tests pass:
  1. `test_task_pin_detection`: Task PINs quarantined to `local_only=True`.
  2. `test_unquoted_passphrase_detection`: Unquoted natural language passphrases redacted and quarantined.
  3. `test_multiline_pem_detection`: Multiline private keys redacted and quarantined.
  4. `test_generic_token_assignment`: Token assignments redacted and quarantined.
  5. `test_json_and_yaml_payload_redaction`: JSON/YAML structured secret keys redacted.
  6. `test_task_update_quarantine`: Task notes/summaries containing secrets quarantined.
  7. `test_benign_lookalikes_not_flagged`: Ports, git commit hashes, semantic versions, URLs, model names, and file paths NOT falsely flagged.
  8. `test_redaction_idempotence`: Re-scanning `[REDACTED_...]` and `[SECRET_REF:...]` text does not re-flag or alter content.

---

## Test Procedure 3: F1 Claude Code MCP Configuration Path & Aliases

**Objective:** Verify that Claude Code installer targets root `.mcp.json` for project scope, prompts for trust, migrates legacy `.claude/mcp.json`, and correctly resolves agent aliases.

**Execution:**
```bash
pytest tests/test_installer.py -k "claude" -v
pytest tests/test_doctor_scope_precedence.py -v
```

**Manual Verification Command:**
```bash
mesh install --dry-run --project-dir . --agent claude
```

**Expected Outcome:**
- Config path reported as `.mcp.json` at project root (NOT `.claude/mcp.json`).
- Shows note: `Note: Claude Code will prompt you to approve project-scope MCP tools on first startup.`
- Legacy migration copies `.claude/mcp.json` to `.mcp.json` and creates `.claude/mcp.json.bak`.
- `claude` alias resolves to `claude-code`.
- `gemini` alias resolves to `gemini-cli`.

---

## Test Procedure 4: F8 Lifecycle Hook Contract & Silent Brief

**Objective:** Verify that `mesh brief --format antigravity` produces valid `injectSteps` JSON structure, wraps output in untrusted reference data banners, and fails gracefully without unhandled exceptions.

**Execution:**
```bash
# 1. Verify JSON hook format output
mesh brief --format antigravity --namespace test-ns

# 2. Verify graceful failure with invalid home
MESH_HOME="/nonexistent/forbidden/path" mesh brief --format antigravity
```

**Expected Outcome:**
- JSON output matches schema:
  ```json
  {
    "injectSteps": [
      {
        "ephemeralMessage": "<!-- UNTRUSTED REFERENCE DATA: MESH CONTEXT BRIEF -->\n..."
      }
    ]
  }
  ```
- With inaccessible storage, exits 0 with minimal fallback or silent message, producing no stack traces or session-terminating crash.

---

## Test Procedure 5: F2 Decision Rationale Flagging

**Objective:** Verify that `mesh brief` flags architectural decisions lacking causal rationale (`because`, `due to`, etc.) with actionable advice without altering database schema.

**Execution:**
```bash
pytest tests/test_work_items.py -k "rationale" -v
```

**Manual Verification:**
```bash
mesh add "decided to switch from REST to gRPC" --type decision
mesh brief
```

**Expected Outcome:**
- `mesh brief` outputs:
  ```text
  - decided to switch from REST to gRPC *(needs rationale — add 'because <reason>')*
  ```
- When updated to "decided to switch from REST to gRPC because throughput doubled", the flag is cleared.

---

## Test Procedure 6: Documentation Consistency & Skill Rules Verification

**Objective:** Verify that all documentation references, version stamps, relative links, and MCP tool schemas are synchronized.

**Execution:**
```bash
python scripts/check_skill.py
python scripts/check_docs.py
```

**Expected Outcome:**
- `check_skill.py`: PASSED (SKILL.md <= 250 lines, rules_block.md <= 12 lines, all 17 tools validated).
- `check_docs.py`: PASSED (0 broken links, 0 stale Fernet references, version stamps match 0.8.2).

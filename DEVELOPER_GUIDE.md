# Mesh Developer Guide (v0.8.2)

How to work on Mesh safely: isolated setup, tests, benchmarks, documentation checks, extension points, release, and field-test reproduction.

User documentation: [DOCS.md](DOCS.md). Internals: [ARCHITECTURE.md](ARCHITECTURE.md). Security: [SECURITY.md](SECURITY.md) and [THREAT_MODEL.md](THREAT_MODEL.md).

---

## 1. Clone and install

```bash
git clone https://github.com/billighost/mesh.git
cd mesh

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install --upgrade pip
pip install -e ".[test]"
```

Optional extras:

| Extra | Use |
|---|---|
| `[test]` | pytest and test dependencies |
| `[dev]` | linting and development tools |
| `[secrets]` | keyring-backed vault |
| `[crypto]` | at-rest encryption |
| `[pytorch]`, `[fastembed]` | alternative embedding backends |
| `[legacy]` | compatibility |

Python support: 3.10–3.13 in CI; the 0.8.1b2 field test also ran on 3.14.0.

---

## 2. Isolated development environment

Never test against your real `~/.mesh` or real agent configuration. Use a fresh data root, a temporary home, and no auto-daemon.

### PowerShell (Windows)

```powershell
$base = Join-Path $env:TEMP ("mesh-dev-" + [guid]::NewGuid().ToString("N").Substring(0,8))
New-Item -ItemType Directory -Path $base | Out-Null

$env:MESH_HOME = Join-Path $base "mesh-home"
$env:HOME = Join-Path $base "home"
$env:USERPROFILE = $env:HOME
$env:APPDATA = Join-Path $base "appdata"
$env:LOCALAPPDATA = Join-Path $base "localappdata"
$env:XDG_CONFIG_HOME = Join-Path $base "xdg"
$env:MESH_NO_AUTO_DAEMON = "1"
$env:MESH_NO_MODEL_DOWNLOAD = "1"
$env:MESH_NO_UPDATE_CHECK = "1"
$env:MESH_CHECK_UPDATES = "0"
$env:MESH_AUTO_APPROVE = "0"
$env:MESH_ASCII = "1"
$env:NO_COLOR = "1"

New-Item -ItemType Directory -Force -Path $env:MESH_HOME,$env:HOME,$env:APPDATA,$env:LOCALAPPDATA,$env:XDG_CONFIG_HOME | Out-Null
```

### POSIX shell

```bash
base="$(mktemp -d)"
export MESH_HOME="$base/mesh-home"
export HOME="$base/home"
export XDG_CONFIG_HOME="$base/xdg"
export MESH_NO_AUTO_DAEMON=1
export MESH_NO_MODEL_DOWNLOAD=1
export MESH_NO_UPDATE_CHECK=1
export MESH_CHECK_UPDATES=0
export MESH_AUTO_APPROVE=0
mkdir -p "$MESH_HOME" "$HOME" "$XDG_CONFIG_HOME"
```

Also consider `MESH_NON_INTERACTIVE=1` for headless runs and `MESH_MCP_INCLUDE_PRIVATE=0` when testing privacy.

### Verify isolation

```bash
mesh doctor
mesh status --json
```

`doctor` must show the temporary data directory, 17 MCP tools, and the daemon not running. If it shows your real `~/.mesh`, stop and fix the environment.

---

## 3. Repository layout

| Path | Contents |
|---|---|
| `mesh/memory.py` | `Mesh` orchestration, embeddings, recall, dedup, contradictions |
| `mesh/store.py` | Schema, WAL, vector cache, hybrid search, work items, brief, log |
| `mesh/guards.py` | Ingest validation and prompt-injection checks |
| `mesh/secrets.py` | Secret detection, redaction, keyring |
| `mesh/crypto.py` | Optional encryption |
| `mesh/audit.py` | Local content-free audit |
| `mesh/context_builder.py` | Prompt-safe context blocks |
| `mesh/passive.py` | Opt-in log capture |
| `mesh/io.py` | Export/import |
| `mesh/namespaces.py` | Namespace lifecycle |
| `mesh/daemon.py`, `http_server.py`, `mcp_server.py`, `dashboard.py` | Interfaces |
| `mesh/cli.py` | CLI |
| `mesh/installer.py`, `skill_installer.py` | Agent integration |
| `mesh/skills/mesh-context-layer/` | Skill, rules block, references |
| `scripts/check_docs.py`, `check_skill.py` | Documentation/skill consistency |
| `benchmarks/` | Reproducible performance scripts and `RESULTS.md` |
| `tests/` | Unit, security, retrieval, concurrency, installer tests |
| `docs/` | Agent QA, field test, releases |

---

## 4. Tests, lint, and checks

```bash
pytest -q
pytest tests/test_memory.py -q
pytest tests/bench/test_retrieval_benchmark.py -v
ruff check .
bandit -r mesh -c pyproject.toml -ll
python scripts/check_skill.py
python scripts/check_docs.py
```

CI (`.github/workflows/ci.yml`) runs ruff, `check_skill.py`, `check_docs.py`, bandit, and pytest on Ubuntu/macOS/Windows with Python 3.10–3.13, using an isolated `MESH_HOME` and `MESH_NO_AUTO_DAEMON=1`.

Test rules:

- Always set a temporary `MESH_HOME` and home directories.
- Never call cloud providers.
- Never use `--yes` where a cloud path is reachable.
- Stop any daemon you start.
- Use `TestClient` auth overrides rather than disabling auth globally:

```python
from fastapi.testclient import TestClient
from mesh.http_server import app, verify_request_auth

app.dependency_overrides[verify_request_auth] = lambda: None
client = TestClient(app)
```

### Benchmarks

```bash
python benchmarks/mesh_benchmark.py
python benchmarks/concurrency_stress.py
python benchmarks/scale_benchmark.py --scales 500,1000,5000
```

Benchmarks need the ONNX model for representative numbers; `hash-384` results are not comparable. Record OS, Python, CPU, model backend, and commit with any published number.

---

## 5. Conventions

- Match the surrounding style; comments explain why, not what.
- Keep the Python API lazy: `mesh/__init__.py` uses PEP 562 to avoid importing numpy/onnxruntime/fastapi eagerly.
- Validate every external input: namespace, IDs, content length, memory type, task status/priority.
- Never trust model-supplied privacy or namespace arguments; enforce server-side flags.
- Audit events must stay content-free.
- New user-visible behavior needs: code, tests, `--help` text, `DOCS.md`, `README.md` if prominent, `CHANGELOG.md`, and `check_docs` coverage.
- Do not edit `rules_block.md` or `SKILL.md` casually; they are the agent-behavior source of truth. See §7.

---

## 6. Adding a feature

### Add an MCP tool

1. Add a `types.Tool(...)` entry to `_MCP_TOOLS` in `mesh/mcp_server.py` with a precise description and full `inputSchema`.
2. Add the dispatch branch in `_execute_tool_sync_impl` (and async/proxy path if applicable).
3. Enforce privacy (exclude `local_only`/quarantined by default), namespace locking, and ownership.
4. Return `untrusted_reference_data: true` for recalled content.
5. Add tests for schema, dispatch, privacy, and errors.
6. Document the tool in `DOCS.md` and the skill's `references/tool_reference.md` if agent-facing.
7. Run `python scripts/check_docs.py`; update its MCP expectations if needed.

### Add a CLI command

1. Register the subparser in `mesh/cli.py`.
2. Implement the handler with non-interactive/`--json` behavior.
3. Add examples to `--help` (the project convention).
4. Add the command name to `REGISTERED_CLI_COMMANDS` in `scripts/check_docs.py`.
5. Document it in `DOCS.md` and `README.md` if it is a headline feature.
6. Test stdout, exit codes, and JSON output.

### Add an HTTP route

1. Add a Pydantic request model in `mesh/http_server.py`.
2. Add the route with the existing auth dependency (only `/health` is unauthenticated).
3. Enforce body-size limits and namespace/privacy rules.
4. Add tests using the auth override.
5. Document the route and request fields in `DOCS.md`.

### Add a memory type

1. Update the canonical type set and `_SORTED_MEMORY_TYPES` in the memory/MCP layers.
2. Update validation and any brief key-type filters.
3. Update CLI `--type` choices and HTTP validation.
4. Update tests, `DOCS.md`, `README.md` table, and `references/tool_reference.md`.
5. Check FTS behavior; no schema migration is needed for a new type value.

### Add a task field or status

1. Change the `work_items` DDL and bump `SCHEMA_VERSION`.
2. Add a migration with a pre-migration backup; preserve forward-compatibility refusal.
3. Update `create_work_item`, `update_work_item`, `list_work_items`, serialization, and all surfaces (CLI/MCP/HTTP/SDK).
4. Update the state diagram and validation tests.
5. Document the field/status and its privacy behavior.

### Add an installer adapter

1. Subclass `AgentAdapter` in `mesh/installer.py`.
2. Implement `get_mcp_config_path`, `get_skill_root_path`, `get_rules_path`, and `supports_scope`.
3. Add the instance to `_ALL_ADAPTERS`; `ADAPTER_MAP` and `SUPPORTED_AGENTS` update automatically.
4. Verify user and project scope paths, merge behavior, backups, dry-run, and uninstall.
5. Add the agent to the README/DOCS tables and state clearly whether it was field-tested.
6. When adding hook support for an agent adapter, implement `get_hooks_path()` and ensure `supports_hooks()` returns `True`.

### Add an agent lifecycle hook

Agent lifecycle hooks allow agent runtimes (e.g. Claude Code, Antigravity) to automatically execute `mesh brief` on `SessionStart`.
To add hook support for a new agent adapter in `mesh/installer.py`:
1. Implement `get_hooks_path(scope, project_dir)` returning the configuration file path (e.g. `.claude/settings.json`).
2. Set `supports_hooks() -> bool: return True`.
3. Ensure `update_hooks_file()` and `remove_hooks_file_entry()` support the agent's schema.
4. Verify with dry-run, atomic backups (`.bak`), and `mesh uninstall` tests.

### Add a secret resolver

1. Extend `mesh/secrets.py` with the scheme and resolution logic.
2. Never log or return the secret value.
3. Redact it from errors and audit.
4. Add tests for missing references, malformed URIs, and child-process injection.
5. Document the scheme in `DOCS.md`; do not claim support you have not tested.

### Add an embedding backend

1. Implement the backend with a distinct `embedding_model` tag.
2. Preserve the `(vector, model_tag)` cache invariant.
3. Define download/consent/checksum behavior and offline fallback.
4. Add healing behavior for rows embedded by a different backend.
5. Measure retrieval quality and latency before documenting numbers.

### Add a schema migration

1. Increment `SCHEMA_VERSION`.
2. Create a timestamped backup before DDL.
3. Apply changes transactionally and idempotently.
4. Refuse databases with a future schema version.
5. Test fresh creation, upgrade from the previous version, reopen, and export/import.

---

## 7. Skill, rules block, and installer synchronization

Source-of-truth files:

- `mesh/skills/mesh-context-layer/rules_block.md` — the always-on protocol.
- `mesh/skills/mesh-context-layer/SKILL.md` — the skill.
- `mesh/skills/mesh-context-layer/references/` — examples, tool reference, troubleshooting.

`mesh/installer.py` embeds the rules block between `<!-- mesh:begin v=<version> -->` and `<!-- mesh:end -->`. The installed copy must match the source file. `scripts/check_skill.py` verifies skill/rules/installer consistency; it runs in CI.

Rules:

- Do not rewrite the skill or rules block to make docs pass. If they disagree with code, report the discrepancy and fix the appropriate source deliberately.
- If you change a tool name or privacy behavior, update the skill references and `check_skill.py` expectations together.
- Bump the rules-block version marker when its content changes.
- The README embeds the rules block verbatim; if the source changes, update the README in the same commit and note the source of truth.

---

## 8. Documentation checks

`scripts/check_docs.py` is stdlib-only and validates:

- version stamps in `ARCHITECTURE.md` and `DEVELOPER_GUIDE.md`
- relative links and GitHub-style heading anchors in core docs and release notes
- CLI commands mentioned in `README.md`, `DOCS.md`, `DEVELOPER_GUIDE.md`, `AGENTS.md`, and `CONTRIBUTING.md`
- every registered CLI command, `MESH_*` environment variable, MCP tool, and `http_server.py` route appears in `DOCS.md`
- MCP tool names mentioned in core docs
- the README rules block equals `mesh/skills/mesh-context-layer/rules_block.md`
- every README section that quotes field-test or probe numbers states the tested version
- every file in `docs/field-test/` is listed in `PUBLIC_MANIFEST.json` with a matching size and SHA-256

`MANIFEST.json` is the evaluator's original archive manifest and intentionally references withheld files; the checker validates `PUBLIC_MANIFEST.json`, which describes the published subset.

Run it before every commit:

```bash
python scripts/check_docs.py
```

If it flags a false positive, fix the checker narrowly rather than weakening the whole rule. Do not add commands or tools to the allowlist unless they genuinely are not user-facing.

---

## 9. Debugging

| Problem | Approach |
|---|---|
| Wrong data directory | Print `MESH_HOME`, run `mesh doctor`, check `MESH_DIR` |
| Unexpected fallback | `mesh doctor`; check `model_status.json`; ensure `MESH_NO_MODEL_DOWNLOAD` is unset |
| Search quality | Compare ONNX vs `hash-384`; inspect match reasons with `mesh ask --json` |
| Daemon issues | `mesh daemon status`; stop it; retry with `MESH_NO_DAEMON_PROXY=1` |
| HTTP auth | Check `~/.mesh/auth_token`; use `Authorization: Bearer`; `/health` is the only unauthenticated route |
| MCP not loading | Run `mesh-server` directly; check the agent's config path and approval settings |
| Installer changes wrong files | `mesh install --dry-run --json`; verify `HOME`/`APPDATA`/`XDG_CONFIG_HOME` |
| Database locked | One namespace has serialized writes; find the other process; check the `.lock` file |
| Schema error | Check `SCHEMA_VERSION`; restore the pre-migration backup; never downgrade in place |
| Failed encryption | Install `[crypto]`; back up; verify the key source; do not lose the passphrase |
| Windows `WinError 5` | Retry; close indexers/AV handles; see T-001 |

Logging: `MESH_LOG_LEVEL=DEBUG`. Audit: `mesh why`, `mesh audit export --json`. Never paste tokens or memory content into issues.

---

## 10. Common pitfalls

- Testing in the real home directory. Always isolate.
- Assuming `local_only` encrypts data. It does not.
- Assuming quarantine is automatic. It is opt-in for capture.
- Assuming `.mesh.toml` works. It is mentioned in the skill but not implemented.
- Assuming `mesh task update` exists. The CLI has no such action in 0.8.1b2.
- Assuming hooks exist. They do not (T-003).
- Quoting old README benchmark numbers. Use `benchmarks/RESULTS.md` and state hardware/method.
- Treating installer adapter code as proven third-party support. Label field-test status.
- Forgetting that `hash-384` changes thresholds and retrieval quality.
- Forgetting to stop a daemon started during tests.

---

## 11. Release process

1. Update `VERSION` in `mesh/version.py` and `version` in `pyproject.toml`.
2. Update `CHANGELOG.md` with an `Unreleased` or dated section.
3. Update docs version stamps (`ARCHITECTURE.md`, `DEVELOPER_GUIDE.md`, skill frontmatter if applicable).
4. Run:
   ```bash
   ruff check .
   python scripts/check_skill.py
   python scripts/check_docs.py
   pytest -q
   python benchmarks/mesh_benchmark.py
   ```
5. Build and smoke-test:
   ```bash
   python scripts/build_release.py
   python -m venv /tmp/mesh-release-test
   /tmp/mesh-release-test/bin/pip install dist/*.whl
   /tmp/mesh-release-test/bin/mesh version
   ```
6. Commit, tag `v<version>`, and publish a GitHub release via the GitHub UI or `gh release create`.
7. `.github/workflows/publish.yml` publishes via **PyPI Trusted Publishing (OIDC)** upon an approved `release: [published]` event (or manual `workflow_dispatch`). Automatic publication on simple tag-push is disabled to ensure human release authorization. It builds, smoke-tests the wheel, uploads artifacts, and publishes with `id-token: write`. No API token is stored in the workflow.

Do not push, force-push, or rewrite history unless the maintainer asks. Do not publish a release from a dirty tree.

---

## 12. Re-running the field test

Public field-test material lives in `docs/field-test/`. The independent archive is kept outside the repository; only publishable files are imported.

Protocol:

1. Build or download the exact wheel; record its SHA-256 and size.
2. Use a fresh virtual environment and a project-local `MESH_HOME`.
3. Set `MESH_NO_AUTO_DAEMON=1`; disable model download if you want the fallback condition.
4. Run the targeted re-test scripts (`data/`, `reproduce/`) and the behavior probe separately. Do not merge their results.
5. Record the agent product/version, model, OS, Python, session count, and whether the user was human or simulated.
6. Publish only redacted material: no absolute paths, usernames, hostnames, tokens, or raw transcripts.
7. Update `ISSUE_STATUS.md` with fixed/open issues and evidence links.
8. Quote numbers only with the tested version adjacent to them.

Known traps: a project `.mcp.json` may be required even when the installer writes `.claude/mcp.json`; an unsandboxed probe can contaminate later sessions; a fresh `CLAUDE_CONFIG_DIR` resets built-in agent memory and weakens a control condition.

---

## 13. Where to ask

Open a GitHub issue for bugs and design questions. Report vulnerabilities privately per [SECURITY.md](SECURITY.md). Keep reports reproducible and redact secrets.

# Working on the Mesh repository (instructions for coding agents)

This file is for AI coding agents that modify this repository. User-facing documentation is [README.md](README.md), [DOCS.md](DOCS.md), and the contributor guide [CONTRIBUTING.md](CONTRIBUTING.md). Deep internals are in [ARCHITECTURE.md](ARCHITECTURE.md).

## Ground rules

1. **Do not change runtime behavior while doing documentation work.** If docs and code disagree, report the discrepancy; do not silently change either.
2. **Do not rewrite** `mesh/skills/mesh-context-layer/SKILL.md`, its `references/`, or `rules_block.md` unless the task explicitly asks for it. They are the source of truth for agent behavior, and `scripts/check_skill.py` enforces their shape.
3. **Never test against the real `~/.mesh` or real agent configuration.** Use a temporary `MESH_HOME`, temporary home directories, and `MESH_NO_AUTO_DAEMON=1`.
4. **Never call a cloud LLM provider** in tests or examples. Do not use `--yes` or `MESH_AUTO_APPROVE=1` where a cloud path is reachable.
5. **Never commit secrets or personal paths.** Use obviously fake placeholders in docs and tests.
6. Do not push, force-push, or rewrite git history. Do not delete or move files without listing them first.
7. Stop any daemon you start (`mesh daemon stop`, or kill the process you launched).

## Build and test

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS/Linux
pip install -e ".[test]"
```

Isolated PowerShell environment:

```powershell
$base = Join-Path $env:TEMP ("mesh-dev-" + [guid]::NewGuid().ToString("N").Substring(0,8))
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
```

The full POSIX recipe is in [DEVELOPER_GUIDE.md §2](DEVELOPER_GUIDE.md#2-isolated-development-environment).

Checks that must pass before you hand work back:

```bash
ruff check .
python scripts/check_skill.py
python scripts/check_docs.py
pytest -q
```

CI (`.github/workflows/ci.yml`) runs these on Ubuntu, macOS, and Windows with Python 3.10–3.13. Some tests are slow; run the focused file first, then the full suite.

## Code conventions

- Python 3.10+, standard library first. Keep the lazy import design in `mesh/__init__.py`.
- Validate external input: namespace names, IDs, memory types, task statuses and priorities, tag lengths, content length.
- Enforce privacy server-side. A model-supplied `local_only`, `include_private`, namespace, or agent ID must not be able to relax a server lock.
- Audit events stay content-free. Never log memory bodies, task summaries, or secret values.
- New behavior needs tests, `--help` text when user-facing, and documentation updates.
- Match the surrounding style. Comments explain why, not what.

## Documentation rules

- Describe only what exists on this branch. Put planned work under **Roadmap (not implemented)**.
- Quote performance numbers only from `benchmarks/RESULTS.md` or the field-test data, with the tested version adjacent.
- Keep README's embedded rules block byte-identical to `mesh/skills/mesh-context-layer/rules_block.md`.
- `scripts/check_docs.py` is stdlib-only. If it reports a false positive, narrow the checker; do not delete a real reference.
- Use relative links that resolve. Use fake tokens such as `YOUR_TOKEN_HERE` in examples.

## Files and areas to treat carefully

| Area | Why |
|---|---|
| `mesh/skills/` | Agent behavior source of truth; checked by `check_skill.py` |
| `mesh/installer.py` | Edits user configuration; needs dry-run, backup, symlink, and uninstall tests |
| `mesh/crypto.py`, `mesh/secrets.py` | Security-sensitive; never weaken detection or logging rules incidentally |
| `mesh/store.py` schema/migrations | Requires `SCHEMA_VERSION` bump, backup, and upgrade tests |
| `docs/field-test/` | An independent test archive; do not rewrite reported results |
| `README.md` evidence section | Must match `benchmarks/RESULTS.md` and the public field-test archive |

## Where to look

- User docs: [DOCS.md](DOCS.md) — every CLI command, MCP tool, HTTP route, environment variable.
- Internals: [ARCHITECTURE.md](ARCHITECTURE.md) — module map, schema, search pipeline, concurrency.
- Contributor workflow: [CONTRIBUTING.md](CONTRIBUTING.md) and [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md).
- Security & open issues: [SECURITY.md](SECURITY.md) and [THREAT_MODEL.md](THREAT_MODEL.md).

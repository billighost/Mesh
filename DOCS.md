# Mesh User Documentation

Complete user guide for **Mesh** (`mesh-context-layer`) **0.8.3** (Beta): installation, agent setup, the memory and task model, the full CLI/MCP/HTTP/Python reference, privacy, encryption, performance evidence, and troubleshooting.

- New here? Start with [Install](#1-install) and the [60-minute tutorial](#2-first-run-tutorial).
- Evaluating? Read [Privacy and data handling](#12-privacy-data-and-network) and [Evidence](#14-performance-and-evidence).
- Looking for one specific command? Jump to the [CLI reference](#6-cli-reference).
- README overview and FAQ: [README.md](README.md).

> Everything in this document was checked against source version `0.8.1b2` at commit `5348d00` on 2026-10-20. Commands marked **verified** were run in an isolated `MESH_HOME`; items not run are labelled as such. No cloud provider was called.

---

## 1. Install

### Requirements

| Item | Requirement |
|---|---|
| Python | 3.10 or newer |
| OS | Windows, macOS, or Linux |
| Disk | A few MB for the package; roughly 90 MB for the optional ONNX model (approximate; exact size depends on the mirror assets) |
| Database | None. SQLite is bundled with Python. |
| Network | Not required for memory operations. Required only if you choose to download the embedding model or check PyPI. |

### Standard install

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install mesh-context-layer
mesh --help
mesh doctor
```

### Optional extras

| Extra | Adds |
|---|---|
| `[secrets]` | Keyring integration for `--vault` and `keyring://` secret references |
| `[crypto]` | At-rest encryption support (`cryptography`, and SQLCipher if available) |
| `[fastembed]` | Alternative embedding backend |
| `[pytorch]` | Alternative PyTorch embedding backend |
| `[test]` | Test dependencies |
| `[dev]` | Development and linting dependencies |
| `[legacy]` | Compatibility dependencies |

```bash
pip install "mesh-context-layer[secrets,crypto]"
```

### Verify the install

```bash
mesh version           # -> mesh 0.8.3
mesh doctor            # storage, SQLite/FTS5, model, daemon, MCP configs, 17 tools
mesh status --json     # machine-readable state
```

`mesh doctor` is safe: it does not download the model or start the daemon.

### Where data goes

Default root: `~/.mesh` (`%USERPROFILE%\.mesh` on Windows). Override with `MESH_HOME` or `MESH_DIR`.

```bash
export MESH_HOME=/path/to/isolated/mesh     # POSIX
$env:MESH_HOME = "D:\mesh-data"             # PowerShell
```

Use a fresh `MESH_HOME` for tests; never point experiments at your real data.

### Windows notes

- The 0.8.1b2 field test ran on Windows 11 with Python 3.14.0.
- A transient `[WinError 5] Access is denied` can occur while replacing a skill directory (T-001). Retry the install.
- Directory ACL hardening is applied by default; `MESH_SKIP_WINDOWS_ACL=1` skips it, which is not recommended.

### macOS/Linux notes

The code paths exist, but the 0.8.1b2 independent field test did not cover macOS or Linux. Unix domain sockets are available for the HTTP server (`--unix-socket`, mode `0600`). File modes are `0700` for the data directory and `0600` for tokens, locks, and key material.

---

## 2. First-run tutorial

This walkthrough uses the default `shared` namespace. In real work, use one namespace per project.

### Step 1 — Inspect the environment

```bash
mesh doctor
```

Expected shape (values vary):

```text
Doctor  mesh v0.8.2 - Windows AMD64 - Python 3.14.0
Data directory    ~/.mesh
SQLite + FTS5     SQLite 3.50.4 with Porter stemming
Daemon            not running (optional)
Embedding model   not downloaded — using hash-384 fallback (lower search quality)
MCP Server tools  17 tools defined in schema
Namespaces        none yet
```

### Step 2 — Store your first memory

```bash
mesh add "Production PostgreSQL 16 runs on port 5432 behind pgBouncer" \
  --type fact --tags db,postgres,prod

mesh add "We deploy with Fly.io in the fra region" --type decision
```

`mesh add` also reads from stdin:

```bash
echo "The staging API is at https://staging.example.invalid" | mesh add --tags staging
```

### Step 3 — Search

```bash
mesh ask "what port does production postgres use?" -n 3
mesh ask "deployment region" --type decision --json
```

Recall combines semantic similarity, FTS5 BM25, IDF weighting, synonyms, and exact-token grounding. Results include a similarity score, match reasons, and a stale flag.

### Step 4 — Update instead of duplicating

```bash
mesh ask "postgres port"          # find the short ID, e.g. a1b2c3d4
mesh update a1b2c3d4 --content "Production PostgreSQL 16 runs on port 5433 behind pgBouncer"
```

Use `mesh update <id> --clear-ttl` to make a decaying memory permanent.

### Step 5 — Track work

```bash
mesh task add "Ship authentication" --kind goal --priority high
mesh task add "Configure JWT RS256" --parent goal-1 --priority high
mesh task start task-1
mesh task done task-1 \
  --summary "Added RS256 signing and key rotation; tests pass" \
  --evidence src/auth.py,tests/test_auth.py

mesh task list
mesh task show task-1
```

### Step 6 — Get a session brief and a changelog

```bash
mesh brief --max-tokens 500
mesh log --since 7d
```

The brief is what a new agent session should read first. The log answers “what have we done?”

### Step 7 — Connect an agent

```bash
mesh install --dry-run
mesh install --apply
```

Then start a fresh agent session and ask it to work on the project. Verify with:

```bash
mesh why --limit 20
mesh inspect --limit 10
mesh task list
```

### Step 8 — Optional: download the local embedding model

```bash
mesh setup
```

`mesh setup` asks before downloading, starting the daemon, or registering login auto-start. Use `mesh setup --no-startup` to skip auto-start, or `MESH_NO_MODEL_DOWNLOAD=1` to stay on `hash-384`.

---

## 3. Connect agents

### One-command install

```bash
mesh install --dry-run                 # show plan and diffs
mesh install --apply                   # apply, all supported agents, user scope
mesh install --agents claude-code,cursor --apply
mesh install --scope project --dir . --namespace my-project --apply
```

What it writes per agent:

1. An MCP server entry named `mesh`, using an absolute executable path.
2. The `mesh-context-layer` skill directory.
3. A versioned rules block between `<!-- mesh:begin v=... -->` and `<!-- mesh:end -->`.

Safety behavior:

- `.bak-<timestamp>` backups before modifying existing files.
- Atomic writes.
- Unrelated config keys and other MCP servers preserved.
- Symlinks outside approved configuration directories rejected.
- Idempotent: rerunning updates only Mesh-owned content.
- `--dry-run` and `--json` for review and automation.
- `--force` reinstalls even when the version matches.

**Lifecycle hooks:** `mesh install --hooks` (or answering yes to the interactive confirmation prompt during `mesh install`) optionally configures a `SessionStart` hook for Claude Code (`.claude/settings.json`) and Antigravity (`hooks.json`) to invoke `mesh brief` when a session begins. When running `mesh install` interactively in a terminal, it prompts for confirmation before applying configuration and before enabling hooks. In automated environments, pass `--apply` (and optionally `--hooks`).

### Agent paths

| Agent | User scope MCP | Project scope MCP | Rules file (user / project) |
|---|---|---|---|
| `claude-code` | `~/.claude/mcp.json` | `.claude/mcp.json` | `~/.claude/CLAUDE.md` / `CLAUDE.md` |
| `claude-desktop` | `%APPDATA%\Claude\claude_desktop_config.json` (Win), `~/Library/Application Support/Claude/...` (mac), XDG (Linux) | user scope only | none |
| `antigravity` | `~/.gemini/antigravity-ide/mcp_config.json` | `.agents/mcp_config.json` | `~/.gemini/config/rules/mesh.md` / `GEMINI.md` |
| `gemini-cli` | `~/.gemini/mcp.json` | `.gemini/mcp.json` | `~/.gemini/GEMINI.md` / `GEMINI.md` |
| `cursor` | `~/.cursor/mcp.json` | `.cursor/mcp.json` | `~/.cursor/rules/mesh.mdc` / `.cursorrules` |
| `windsurf` | `~/.codeium/windsurf/mcp_config.json` | `.windsurf/mcp_config.json` | `~/.codeium/windsurf/rules/mesh.md` / `.windsurfrules` |
| `opencode` | XDG `opencode/mcp.json` | `opencode.json` | XDG `opencode/AGENTS.md` / `AGENTS.md` |
| `codex` | `~/.codex/mcp.json` | `.codex/mcp.json` | `~/.codex/AGENTS.md` / `AGENTS.md` |

Skill directories follow the same agent roots (`skills/` under the agent directory, or project equivalents).

### Manual configuration

Use this when an agent is not supported by the installer or you manage config yourself:

```json
{
  "mcpServers": {
    "mesh": {
      "command": "/absolute/path/to/mesh-server",
      "env": {
        "MESH_NAMESPACE": "my-project",
        "MESH_AGENT_ID": "my-agent"
      }
    }
  }
}
```

`mesh-server` is the MCP stdio entry point. `mesh server` is the equivalent CLI form, and `mesh-http` / `mesh http` start the HTTP API. There is no `/identity` endpoint.

### Claude Code caveat (measured)

In the 0.8.1b2 probe, Claude Code 2.1.304 auto-loaded a project `.mcp.json`, while the installer wrote `.claude/mcp.json`. The probe created a byte-identical `.mcp.json` copy and passed it explicitly. If tools do not appear, check the exact path your version loads and the agent's MCP approval settings.

### Uninstall

```bash
mesh uninstall --dry-run
mesh uninstall --apply
mesh uninstall --agents cursor --scope project --dir . --apply
```

This removes only Mesh-owned MCP entries, rules blocks, and skill directories. It does not delete `~/.mesh`.

---

## 4. What the agent does automatically

Mesh cannot force a model to call a tool. It provides three persuasion layers plus optional lifecycle hooks:

| Layer | Implemented | What it guarantees |
|---|---|---|
| MCP tools | Yes, 17 | Callable if the agent loads the server and is allowed to use tools |
| Skill | Yes | A discoverable protocol document with references; progressive disclosure |
| Rules block | Yes | Visible every session if the agent reads its rules file |
| Hooks (Optional) | Yes (Claude Code, Antigravity) | Automatically runs `mesh brief` on session start when installed with `--hooks` |

The rules block source of truth is [`mesh/skills/mesh-context-layer/rules_block.md`](mesh/skills/mesh-context-layer/rules_block.md), embedded verbatim in [README.md](README.md#rules-block-verbatim-source-of-truth).

### Verifying behavior

```bash
mesh why                       # read/write/update/delete events with agent IDs
mesh inspect --limit 20
mesh task list --status all
mesh status --json
```

Check, in order:

1. The MCP server is listed in the config path the agent actually reads.
2. `mesh-server` starts without error.
3. The skill directory exists and is complete.
4. The rules block is in the file the agent reads (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, rules file).
5. The agent permits MCP tools without extra approval.
6. The agent's context is fresh; a long session may have loaded its rules before installation.

Measured expectation, not a guarantee: with all three layers, 5/5 valid probe sessions called the brief first; with MCP + skill but no rules block, the only valid session made no Mesh calls. See [Field test](#15-field-test).

---

## 5. Core model

### Namespace

A namespace is one SQLite database: `mesh_<namespace>.db`. Default `shared`. Use one per project. Namespaces are logical isolation between projects for one OS user, not a security boundary between users.

Resolution order used by the CLI and SDK: explicit `--namespace` / argument, then `MESH_NAMESPACE`, then `shared`. The bundled skill additionally mentions `.mesh.toml`, git remote name, and folder name; **`.mesh.toml` is not read by the current code** (documentation discrepancy, not an implemented feature).

### Memory types

`fact`, `preference`, `context`, `result`, `instruction`, `process`, `decision`, `error`, `secret_ref`, `general`.

### Confidence, TTL, and staleness

- Stored confidence is 0.0–1.0.
- With `ttl_days`, confidence decays linearly from the creation/decay start to 0 at the TTL.
- Effective confidence = stored confidence × decay factor.
- `is_stale` when effective confidence < **0.3**.
- `mesh ask` includes stale memories by default; `--no-stale` hides them.
- Work items have no TTL and never decay.

### Deduplication and contradictions

- Cosine similarity ≥ **0.985**: treated as a duplicate; the existing memory is reinforced instead of inserting a new row.
- Cosine similarity ≥ **0.82** and below dedup: a contradiction record is created. Both memories remain; the new one is stored. Review with `mesh conflicts` / `mesh_contradictions`; resolve with `--resolve <id>` / `mesh_resolve_contradiction`.
- Contradiction detection uses embeddings; `hash-384` fallback changes its behavior.

### Privacy, quarantine, and trust

- `local_only=true` (`--private`): excluded from exports and default agent reads/context/brief/patterns. Not encryption.
- `quarantined`: held from recall/context until approved with `mesh approve`.
- `trust_tier`: provenance label. Passive capture uses `log_captured`.
- Quarantine is **not** automatic for every write. `mesh capture` quarantines only with `--quarantine` or `MESH_QUARANTINE_CAPTURE=1`; `mesh_sync` only when `quarantine=true`.

### Work items

Rows in `work_items`, with an append-only `work_events` history. Fields: `id` (UUID), `short_id` (`task-1`, `goal-2`), `kind`, `parent_id`, `title`, `description`, `status`, `priority`, `created_by`, timestamps, `completed_by`, `summary`, `evidence`, `tags`, `local_only`.

Statuses: `todo`, `in_progress`, `blocked`, `done`, `cancelled`. `reopened` is a transition to `todo`, not a stored status.

### Brief and log

- **Brief:** health, up to 5 open items, up to 4 completed in the last 48 hours, up to 4 key facts/decisions, unresolved-contradiction warning. Default 500 tokens (4 chars/token), excludes private and quarantined by default.
- **Log:** completed `done` items and `decision` memories in a window (`7d` default), with summaries and evidence.

### Audit

`mesh why` and `mesh audit export` read a local operational audit database. Events contain surface, action, tool name, timing, counts, status, and agent ID — not memory text. `MESH_MAX_AUDIT_ROWS` caps stored rows; `MESH_TELEMETRY_BATCH` / `MESH_TELEMETRY_FLUSH_SECONDS` tune local batching. Nothing is uploaded.

---

## 6. CLI reference

All commands support `-h` / `--help`; `mesh help <command>` is equivalent. Exit codes are non-zero on failure. `NO_COLOR`, `FORCE_COLOR`, `MESH_FORCE_COLOR`, `MESH_ASCII=1`, and `MESH_NO_SPINNER=1` control rendering.

### Memory commands

#### `mesh add [content]`

Store a memory or secret pointer.

| Flag | Meaning |
|---|---|
| `--type TYPE` | `context`, `decision`, `error`, `fact`, `general`, `instruction`, `preference`, `process`, `result`, `secret_ref` |
| `--tags a,b` | Comma-separated tags |
| `--private` | Set `local_only` |
| `--force-public` | Override credential detection (interactive TTY only) |
| `--secret-ref URI` | Store a locator instead of a value |
| `--vault` | Vault detected secret in the OS keyring, store a pointer |
| `--namespace NAME` | Namespace override |
| `--confidence FLOAT` | 0.0–1.0, default 1.0 |
| `--ttl DAYS` | Days until stale |

Reads stdin when `content` is omitted. Examples are printed by `--help`.

#### `mesh ask [query]`

Hybrid search.

Flags: `--count/-n/--n`, `--namespace`, `--type`, `--tags`, `--min-confidence`, `--min-similarity`, `--no-stale`, `--no-other`, `--no-private`, `--include-quarantined`, `--json`.

#### `mesh inspect`

Browse recent memories. Flags: `--limit`, `--type`, `--tag`, `--no-private`, `--quarantined`, `--namespace`, `--json`. `mesh i` is an alias for `mesh inspect`.

#### `mesh update <memory_id>`

Edit in place, preserving UUID and creation metadata.

Flags: `--content`, `--type`, `--confidence`, `--tags`, `--ttl`, `--clear-ttl`, `--private`, `--public`, `--namespace`.

#### `mesh forget <memory_id>`

Permanently delete a memory and scrub its audit/contradiction references. Flags: `--namespace`, `--yes`.

#### `mesh approve [memory_id]`

Approve quarantined memories. Flags: `--all`, `--namespace`.

#### `mesh conflicts`

List contradictions. Flags: `--resolve ID`, `--namespace`, `--json`.

#### `mesh patterns`

Show top queries, memory gaps, unused memories, stale memories. Flags: `--namespace`, `--include-private`, `--json`.

#### `mesh why`

Audit trail. Flags: `--limit`, `--namespace`, `--action {read,write,update,forget,delete,all}`.

#### `mesh context`

Print a ready-to-paste context block. Flags: `--query`, `--namespace`, `--count/-n`, `--format {markdown,plain,xml,json}`, `--min-confidence`, `--type`, `--tags`, `--include-private`.

#### `mesh digest`

Recent-activity digest. Flags: `--namespace`, `--hours` (default 24), `--json`, `--include-private`, `--window-stale`.

### Task and project commands

#### `mesh task`

Actions: `add`, `list`, `start`, `done`, `block`, `cancel`, `reopen`, `show`.

```bash
mesh task add "Title" [--kind task|goal] [--parent ID] [--priority low|medium|high|critical] \
  [--desc TEXT] [--tags a,b] [--private] [--namespace NAME]
mesh task list [--status open|closed|all|todo|in_progress|blocked|done|cancelled] \
  [--kind task|goal] [--parent ID] [--limit N] [--no-private] [--json]
mesh task start <id>
mesh task done <id> --summary TEXT [--evidence a.py,b.py]
mesh task block <id> [--note TEXT]
mesh task cancel <id> [--note TEXT]
mesh task reopen <id> [--note TEXT]
mesh task show <id> [--json]
```

There is **no `mesh task update` action** in 0.8.1b2. Use `start`, `block`, `done`, `cancel`, or `reopen`. The SDK and MCP do expose a general update (`mesh_task_update`, `task_update`).

`mesh tasks` is an alias for `mesh task`.

#### `mesh brief`

Flags: `--namespace`, `--max-tokens` (default 500), `--format {plain,markdown,json}`, `--no-private`.

#### `mesh log`

Flags: `--since` (default `7d`; e.g. `24h`, `30d`, ISO date), `--format {markdown,json}`, `--no-private`, `--namespace`.

### Setup and integration commands

#### `mesh setup`

Start the optional daemon, offer the model download, install skills. Flags: `--namespace`, `--no-startup`, `--wait`, `--yes`.

#### `mesh install`

Flags: `--agents` (comma list or `all`), `--scope {user,project}`, `--dir`, `--namespace`, `--mesh-home`, `--dry-run`, `--json`, `--apply`, `--yes` (alias), `--force`, `--hooks`.

#### `mesh uninstall`

Flags: `--agents`, `--scope`, `--dir`, `--dry-run`, `--json`, `--apply`, `--yes`.

#### `mesh install-skills`

Install the skill into detected agents. Flags: `--yes`, `--force`, `--list`.

### Server and daemon commands

#### `mesh daemon`

Actions: `start`, `stop`, `restart`, `status`, `unload`, `warmup`, `rotate-token`, `install-startup`, `remove-startup`.

Flags: `--host`, `--port`, `--idle-timeout`, `--lazy`, `--yes`. Default loopback port `7701`.

#### `mesh http` / `mesh-http` / `mesh server-http`

Start the HTTP API. Flags: `--host` (default `127.0.0.1`), `--port` (default `7701`), `--unix-socket`, `--idle-timeout`, `--no-warmup`, `--yes`. `mesh server-http` is a backward-compatible alias of `mesh http`.

#### `mesh server` / `mesh-server`

MCP stdio server. `mesh-server` is the console entry point used in agent configs. `mesh server --help` is not a useful page; run `mesh-server` under the agent, not by hand.

#### `mesh-dashboard`

Flags: `--namespace`, `--host` (default `127.0.0.1`), `--port` (default 7433), `--no-browser`, `--yes`.

### Data and diagnostics commands

#### `mesh status`

Flags: `--namespace`, `--check-updates`, `--json`. The update check is opt-in and contacts PyPI.

#### `mesh doctor`

No flags. Checks data directory, SQLite/FTS5, daemon, model/engine, MCP configs, tool count, namespaces.

#### `mesh namespaces [list|delete|rename|stats]`

Flags: `--yes`, `--json`. Examples: `mesh namespaces stats work`, `mesh namespaces rename old new`, `mesh namespaces delete scratch`.

#### `mesh export <output>`

Exports memories from the selected namespace to JSON, Markdown, or CSV.

- **Formats (`--format {json,markdown,csv}`)**: Inferred from the output extension (`.json`, `.md`, `.csv`) if omitted.
- **JSON**: The only lossless round-trip format; can be restored with `mesh import`. Supports `--encrypt` (authenticated envelope) and vector embeddings (toggle with `--no-embeddings`).
- **Markdown (`.md`)**: One-way human-readable summary of memories and work items. Vector embeddings are omitted by default.
- **CSV (`.csv`)**: One-way tabular export for spreadsheets. Vector embeddings are omitted by default. Neutralizes spreadsheet formula injection (CWE-1236) by automatically prefixing cells starting with `=`, `+`, `-`, `@`, `\t`, or `\r` with a single quote (`'`).
- Flags: `--format`, `--namespace`, `--no-embeddings`, `--encrypt` (JSON only), `--passphrase`. Private memories are never exported in any format.

#### `mesh import <input.json>`

Flags: `--namespace`, `--overwrite`, `--dry-run`, `--passphrase`.

#### `mesh encrypt-db`

Flags: `--namespace`, `--all`, `--passphrase`, `--no-backup`. Encrypts the memory and audit databases at rest; creates a backup and vacuums. Requires the `[crypto]` extra or a SQLCipher-capable environment.

#### `mesh audit export`

Flags: `--json`, `--since`, `--surface {mcp,cli,http,sdk}`, `--namespace`, `--limit` (default 1000).

#### `mesh capture`

Extract memories from a session log. Flags: `--file`, `--stdin`, `--namespace`, `--dry-run`, `--verbose`, `--local-only`, `--quarantine`, `--show-provider`, `--yes`.

#### `mesh run -- <command>`

Run a command with resolved secret references injected. Flags: `--namespace`, `--env KEY[=URI]` (repeatable). Examples:

```bash
mesh run -- pytest
mesh run --env STRIPE_KEY=keyring://mesh/shared/STRIPE_KEY -- ./deploy.sh
```

#### `mesh shell`

Interactive REPL. Flag: `--namespace`.

#### `mesh completion [bash|zsh|fish|powershell]`

Print or install completion. Flags: `--install`, `--yes`.

#### `mesh version` / `mesh help`

Print the version or command help.

---

## 7. MCP tools reference

The server defines exactly **17** tools. All are namespaced by an optional `namespace` argument, subject to `MESH_LOCKED_NAMESPACE`. Tool results are untrusted reference data.

| Tool | Purpose | Required | Key arguments and defaults |
|---|---|---|---|
| `mesh_learn` | Store a memory with dedup/contradiction checks | `content` | `memory_type=fact`, `confidence=1.0`, `tags`, `ttl_days`, `local_only=false`, `secret_ref`, `vault_secrets=false` |
| `mesh_recall` | Hybrid search | `query` | `n=5` (1–50), `min_similarity=0`, `min_confidence=0`, `include_stale=true`, `memory_type`, `tags`, `source_agent`, `include_other_possible=true`, `max_other_possible=5` |
| `mesh_update` | Update a memory in place | `memory_id` | `content`, `memory_type`, `confidence`, `tags`, `ttl_days`, `clear_ttl=false`, `local_only`, `secret_ref` |
| `mesh_approve` | Approve a quarantined memory | `memory_id` | — |
| `mesh_forget` | Permanently delete a memory | `memory_id` | — |
| `mesh_inspect` | Browse recent memories | — | `limit=10` (1–200), `memory_type`, `tag`, `quarantined_only=false` |
| `mesh_context` | Build a system-prompt context block | — | `query`, `count=10`, `max_memories`, `format=markdown`, `min_confidence=0.3`, `memory_type`, `tags` |
| `mesh_patterns` | Usage patterns and gaps | — | — |
| `mesh_contradictions` | List conflicts | — | `resolved=false` |
| `mesh_resolve_contradiction` | Resolve a conflict | `contradiction_id` | — |
| `mesh_sync` | Store a turn summary; optional log capture when enabled | `summary` | `include_history=false`, `quarantine` |
| `mesh_project_brief` | Session-start brief | — | `max_tokens=500`, `include_work_items=true` |
| `mesh_task_create` | Create a goal/task | `title` | `kind=task`, `parent_id`, `description=""`, `priority=medium`, `tags` |
| `mesh_task_update` | Update status/fields | `item_id` | `status`, `priority`, `title`, `description`, `note` |
| `mesh_task_complete` | Complete with mandatory summary | `item_id`, `summary` | `evidence` |
| `mesh_task_list` | List work items | — | `status=open`, `kind`, `parent_id`, `limit=20` |
| `mesh_log` | Project changelog | — | `since=7d`, `format=markdown` |

Privacy behavior:

- `mesh_recall`, `mesh_inspect`, `mesh_context`, `mesh_patterns`, and `mesh_project_brief` exclude `local_only` and unapproved `quarantined` items by default.
- `mesh_learn` forces `local_only` when credentials are detected.
- `mesh_forget` scrubs related audit/contradiction references.
- A model can pass `namespace` and privacy arguments only if the server configuration allows it. Set `MESH_LOCKED_NAMESPACE=1` and `MESH_BLOCK_PRIVATE_OVERRIDE=1` to remove those choices.

`mesh_sync` stores only the summary unless `MESH_ALLOW_LOG_SYNC=1` or `MESH_ENABLE_AUTO_SYNC=1` enables log scanning.

---

## 8. HTTP API reference

Start:

```bash
mesh-http --host 127.0.0.1 --port 7701
```

Auth: every route except `GET /health` requires the bearer token from `~/.mesh/auth_token` (mode `0600`), sent as `Authorization: Bearer <token>` or `X-Mesh-Token: <token>`. The token is local; do not paste a real one into documentation.

```bash
TOKEN=$(cat ~/.mesh/auth_token)
curl -s http://127.0.0.1:7701/health
curl -s http://127.0.0.1:7701/engine -H "Authorization: Bearer $TOKEN"
```

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness; unauthenticated |
| GET | `/engine` | Engine/model state |
| POST | `/warmup` | Load the model (`namespace`, `namespaces`, `blocking=false`) |
| POST | `/unload` | Release model memory |
| POST | `/mcp` | Proxy a JSON-RPC MCP call |
| POST | `/learn` | Store a memory |
| POST | `/recall` | Hybrid search |
| POST | `/update` | Update a memory |
| POST | `/approve` | Approve a quarantined memory |
| POST | `/forget` | Delete a memory |
| DELETE | `/memories/{memory_id}` | Delete a memory by ID |
| GET | `/inspect` | List memories (`limit`, `memory_type`, `tag`, `namespace`) |
| GET | `/patterns` | Usage patterns |
| GET | `/contradictions` | List conflicts |
| POST | `/contradictions/resolve` | Resolve a conflict |
| POST | `/context` | Context block |
| GET | `/audit` | Audit events |
| GET | `/audit/events` | Audit events (stream/export form) |
| GET | `/brief` | Session brief |
| GET | `/tasks` | List work items |
| POST | `/tasks` | Create a work item |
| GET | `/tasks/{item_id}` | Get a work item |
| PATCH | `/tasks/{item_id}` | Update a work item |
| POST | `/tasks/{item_id}/complete` | Complete with summary |
| GET | `/log` | Project changelog |

Request bodies:

- `/learn`: `content`, `memory_type`, `confidence`, `tags`, `local_only`, `ttl_days`, `namespace`, `agent_id`, `secret_ref`, `vault_secrets`, `quarantined`.
- `/recall`: `query`, `count` or `n`, `memory_type`, `tags`, `source_agent`, `min_similarity`, `min_confidence`, `include_stale`, `include_private`, `include_quarantined`, `include_other_possible`, `max_other_possible`, `namespace`, `agent_id`.
- `/update`: `memory_id` plus any of `content`, `memory_type`, `confidence`, `tags`, `local_only`, `ttl_days`, `clear_ttl`, `secret_ref`, `quarantined`, `namespace`, `agent_id`.
- `/tasks`: `title`, `description`, `kind`, `parent_id`, `priority`, `tags`, `local_only`, `namespace`, `agent_id`.
- `/tasks/{id}`: any of `title`, `description`, `status`, `priority`, `note`, `namespace`, `agent_id`.
- `/tasks/{id}/complete`: `summary` (required), `evidence`, `note`, `namespace`, `agent_id`.

Example:

```bash
TOKEN=$(cat ~/.mesh/auth_token)

curl -s -X POST http://127.0.0.1:7701/learn \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"content":"Auth service requires JWT RS256","memory_type":"instruction","tags":["auth"]}'

curl -s -X POST http://127.0.0.1:7701/recall \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"query":"JWT algorithm","n":3}'

curl -s -X POST http://127.0.0.1:7701/tasks \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"title":"Configure RS256","kind":"task","priority":"high"}'
```

Binding beyond loopback requires `MESH_ALLOWED_HOSTS`; doing so exposes an unauthenticated `/health` and authenticated data routes to the network. Prefer a reverse proxy with TLS and additional authentication if you must.

---

## 9. Python SDK

```python
from mesh import Mesh

agent_a = Mesh(namespace="checkout-service", agent_id="backend-agent")
agent_a.learn(
    "Production PostgreSQL 16 runs on port 5432 with pgBouncer",
    memory_type="fact",
    tags=["db", "prod"],
)

agent_b = Mesh(namespace="checkout-service", agent_id="frontend-agent")
for item in agent_b.recall("what port does production postgres use?", n=3):
    print(item["similarity"], item["content"], item["source_agent"])
```

Public methods on `Mesh` (0.8.2):

| Method | Purpose |
|---|---|
| `learn(content, memory_type=..., confidence=..., tags=..., ttl_days=..., local_only=..., secret_ref=..., vault_secrets=...)` | Store a memory |
| `recall(query, n=..., min_similarity=..., min_confidence=..., include_stale=..., memory_type=..., tags=..., source_agent=..., include_private=...)` | Hybrid search |
| `update(memory_id, ...)` | Update in place |
| `forget(memory_id)` | Delete |
| `inspect(limit=..., memory_type=..., tag=..., include_private=...)` | Browse |
| `patterns(include_private=False)` | Usage patterns |
| `contradictions(resolved=False)` | Conflicts |
| `clear()` | Delete all memories in the namespace |
| `count()` | Count memories |
| `get_project_brief(max_tokens=500, ...)` | Brief |
| `get_changelog(since="7d", format="markdown", ...)` | Changelog |
| `task_create(...)`, `task_update(...)`, `task_complete(...)`, `task_list(...)`, `task_get(...)`, `log(...)` | Aliases mirroring MCP tool names |

`Mesh.learn()` returns the stored row, including `local_only` (fixed in C-002). Private data follows the same rules as the CLI/MCP: excluded from default reads, never exported.

---

## 10. Dashboard

```bash
mesh-dashboard --namespace my-project --port 7433
```

The dashboard prints and opens a loopback URL containing a one-time `token` query parameter. It sets an `HttpOnly; SameSite=Strict` session cookie and strips the token from the URL. Do not share the launch URL while it contains the token.

Features: search with score breakdowns, contradiction review, patterns/gaps, audit stream, and keyboard shortcuts (`/` focus search, `n` new memory, `Esc` close).

It binds loopback by default. Non-loopback binding requires an explicit `--host`; do not expose it without a proxy and additional access control.

---

## 11. Configuration and environment variables

### Resolution order

1. Explicit CLI argument or SDK parameter.
2. Environment variable.
3. Built-in default.

### Core

| Variable | Meaning |
|---|---|
| `MESH_HOME`, `MESH_DIR` | Data root. Default `~/.mesh`. |
| `MESH_NAMESPACE` | Default namespace. Default `shared`. |
| `MESH_AGENT_ID` | Agent identity recorded on writes. |
| `MESH_AGENT_SCOPES` | Comma-separated scopes limiting what an agent identity may access. |
| `MESH_WORKSPACE` | Workspace root used by capture/sync inference. |
| `MESH_LOCKED_NAMESPACE` | `1` locks to the active namespace; a value pins that namespace. |
| `MESH_ENFORCE_AGENT_OWNERSHIP` | `1` blocks cross-agent update/delete. CLI/dashboard exempt. |
| `MESH_INCLUDE_PRIVATE` | `0`/`false` globally excludes private data from SDK recall/inspect. |
| `MESH_MCP_INCLUDE_PRIVATE` | Controls whether MCP tools may include private data. |
| `MESH_BLOCK_PRIVATE_OVERRIDE` | `1` prevents model-supplied `include_private=true`. |
| `MESH_READ_ONLY` | `1` disables writes. |
| `MESH_AUTHENTICATED_AGENT_ID` | Internal: agent identity asserted by an authenticated HTTP request. |

### Consent, automation, and display

| Variable | Meaning |
|---|---|
| `MESH_AUTO_APPROVE` | `1` skips interactive consent prompts. Use only in trusted automation. |
| `MESH_NON_INTERACTIVE` | Forces non-interactive behavior. |
| `MESH_NO_AUTO_DAEMON` | `1` never auto-starts the daemon. |
| `MESH_AUTO_START_DAEMON` | Opts into daemon auto-start where supported. |
| `MESH_NO_DAEMON_PROXY` | Disables CLI proxying through the daemon. |
| `MESH_ALLOW_DAEMON_DOWNLOAD` | Allows a daemon started with `--yes` to download the model. |
| `MESH_QUIET` | `1` suppresses progress output and spinner to stderr. |
| `NO_COLOR`, `FORCE_COLOR`, `MESH_NO_COLOR`, `MESH_FORCE_COLOR`, `MESH_ASCII`, `MESH_NO_SPINNER` | Output rendering. |
| `XDG_CONFIG_HOME` | Config root for OpenCode and some agent paths on Unix. |

### Embeddings and model

| Variable | Meaning |
|---|---|
| `MESH_NO_MODEL_DOWNLOAD` | `1` disables ONNX download; stays on `hash-384`. |
| `MESH_SKIP_CHECKSUM` | Skips model checksum verification. Not recommended. |
| `MESH_MODEL_SHA256` | Additional/replacement ONNX SHA-256 allowlist entry. |
| `MESH_TOKENIZER_SHA256` | Additional/replacement tokenizer SHA-256 allowlist entry. |
| `MESH_FORCE_PYTORCH` | Force the PyTorch backend (requires `[pytorch]`). |
| `MESH_FORCE_FASTEMBED` | Force the FastEmbed backend (requires `[fastembed]`). |
| `MESH_FORCE_HASH_EMBED` | Force the fast hash-384 fallback embedding backend without loading ONNX. |
| `MESH_CLI_FORCE_ONNX` | Force ONNX model loading on one-shot CLI commands instead of fast hash-384. |
| `MESH_AUTO_HEAL_ON_CALL` | `1` runs embedding healing synchronously on store access; default `0` (healing runs in background). |
| `MESH_SYNC_ONNX` | `1` forces synchronous ONNX embedding on learn rather than fast-path hash indexing. |
| `MESH_BG_ONNX` | `0` disables background stage-2 ONNX re-embedding (default `1`). |
| `MESH_BG_ONNX_IDLE_MS` | Idle time in milliseconds before background ONNX worker re-embeds queued items. |
| `MESH_BG_ONNX_DRAIN_ON_EXIT` | `auto`, `1` (force drain on exit), or `0` (skip drain). |
| `MESH_BG_ONNX_DRAIN_TIMEOUT` | Max seconds to wait for background re-embedding queue drain on exit. Default 10.0. |
| `MESH_MODEL_DISCOVERY_TTL` | TTL in seconds for model discovery cache. |
| `MESH_MODEL_PAIR_RECHECK_SECONDS` | Interval in seconds to re-verify model pair integrity. |
| `MESH_EMBED_CACHE_SIZE` | In-memory embedding LRU size. Default 1024. |
| `MESH_IDLE_UNLOAD_SECONDS` | Idle seconds before unloading the model; `0` never unloads. |

### Storage and limits

| Variable | Meaning |
|---|---|
| `MESH_MAX_CONTENT_LENGTH` | Maximum memory content length. Default 16384. |
| `MESH_MAX_NAMESPACES` | Namespace cap. Default 256. |
| `MESH_MAX_OPEN_NAMESPACES` | Limit on simultaneously open namespace handles. |
| `MESH_MAX_POOL_SIZE` | SQLite connection pool cap. |
| `MESH_MAX_VECTOR_CACHE_SIZE` | In-memory vector cache cap. |
| `MESH_MAX_QUERY_ROWS` | Query result cap. |
| `MESH_MAX_AUDIT_ROWS` | Audit row cap. |
| `MESH_STRICT_FSYNC` | `1` enforces SQLite `PRAGMA synchronous=NORMAL` instead of `OFF` for strict durability. |
| `MESH_INIT_LOCK_RETRY_SECONDS` | Seconds to wait/retry acquiring initialization lock for database creation/migration. Default 10. |
| `MESH_FS_MEMO_MS` | Window in milliseconds (default 10 ms) during which lstat-verified file paths are memoized. `0` disables. |
| `MESH_PATH_RECHECK_SECONDS` | Interval in seconds (default 60 s) to cache verified safe path evaluations for audit logging. |
| `MESH_SKIP_WINDOWS_ACL` | Skips Windows ACL hardening. Not recommended. |

### HTTP, daemon, and auth

| Variable | Meaning |
|---|---|
| `MESH_HTTP_HOST`, `MESH_HTTP_PORT` | Default bind host/port (`127.0.0.1`, `7701`). |
| `MESH_ALLOWED_HOSTS` | Comma-separated additional allowed `Host` values for non-loopback binding. |
| `MESH_UNIX_SOCKET` | POSIX Unix socket path (mode `0600`). |
| `MESH_AUTH_TOKEN`, `MESH_TOKEN` | Token overrides for server/client. Prefer the generated `~/.mesh/auth_token`. |
| `MESH_FORCE_DAEMON_PROBE` | `1` forces port probing even if `daemon.pid` is absent. |
| `MESH_CSRF` | CSRF protection control for browser surfaces. |
| `MESH_IN_DAEMON` | Internal: marks a process running inside the daemon. |

### Encryption and secrets

| Variable | Meaning |
|---|---|
| `MESH_ENCRYPT_AT_REST` | Enables at-rest encryption. |
| `MESH_PASSPHRASE` | Passphrase; derives a key via PBKDF2-HMAC-SHA256. |
| `MESH_ENCRYPTION_KEY` | Raw key material; takes precedence over the passphrase. |
| `MESH_KDF_ITERATIONS` | PBKDF2 iterations. Default 600,000. |
| `MESH_KEYRING_BACKEND` | `local` uses `~/.mesh/.master_key` instead of the OS keyring. |
| `MESH_FORCE_STDLIB_CRYPTO` | Forces the standard-library crypto path. |
| `MESH_VAULT_SECRETS` | `1` vaults detected secrets into the keyring and stores pointers. |

### Capture, sync, and cloud consent

| Variable | Meaning |
|---|---|
| `MESH_ALLOW_LOG_SYNC` | `1` allows `mesh_sync` to scan agent logs. |
| `MESH_ENABLE_AUTO_SYNC` | `1` enables the MCP background sync loop (every 120 s when active). |
| `MESH_QUARANTINE_CAPTURE` | `1` quarantines captured memories. |
| `MESH_ALLOW_CLOUD_CAPTURE` | `1` allows cloud LLM capture. |
| `MESH_ALLOW_CLOUD_SYNC` | `1` allows cloud sync paths. |
| `MESH_NO_IMPLICIT_CLOUD` | `1` blocks implicit cloud use. |
| `MESH_AI_PROVIDER` | `anthropic`, `openai`, `groq`, or `ollama`. |
| `MESH_AI_API_KEY` | Provider API key (or use the provider-specific key). |
| `MESH_AI_MODEL` | Provider model name. |
| `MESH_AI_BASE_URL` | Provider base URL override. |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GROQ_API_KEY` | Provider keys recognized by capture. |

### Updates

| Variable | Meaning |
|---|---|
| `MESH_CHECK_UPDATES` | `1` enables the PyPI version check. |
| `MESH_NO_UPDATE_CHECK` | Disables all update checks. |

---

## 12. Privacy, data, and network

### Local embeddings

Mesh embeds locally with ONNX Runtime (`all-MiniLM-L6-v2`, 384 dimensions) or the built-in `hash-384` fallback. There is no external embedding endpoint. Normal memory operations do not touch the network.

### Files

| File | Purpose | Protection |
|---|---|---|
| `mesh_<ns>.db` | Memories, FTS5, contradictions, work items/events, metadata | Directory `0700`; file permissions; optional encryption |
| `mesh_<ns>.db-wal`, `-shm` | SQLite WAL state | Same directory |
| `audit_<ns>.db` | Content-free audit | Same directory |
| `.mesh_<ns>.lock` | Cross-process write lock | `0600` on POSIX |
| `auth_token` | HTTP/daemon bearer token | `0600` |
| `models/` | ONNX model/tokenizer | User directory |
| `model_status.json` | Download/verification state | User directory |
| `.encryption_salt`, `.master_key`, `.encryption_enabled` | Encryption material | `0600` |
| `<config>.bak-<timestamp>` | Installer backups | Same permissions as the original |

### Network destinations

| Destination | Trigger | Control |
|---|---|---|
| Hugging Face / hf-mirror | Model download | Consent prompt or `--yes`; `MESH_NO_MODEL_DOWNLOAD=1` disables |
| PyPI | Version check | Off unless `--check-updates` or `MESH_CHECK_UPDATES=1` |
| Anthropic/OpenAI/Groq | Cloud-assisted capture | Off unless key + provider + `MESH_ALLOW_CLOUD_CAPTURE=1` or interactive consent |
| Ollama | Local capture | Explicit provider/base URL |
| Loopback 7701/7433 | Daemon/HTTP/dashboard | Local only by default |

### What leaves the machine

- Nothing during ordinary add/recall/update/forget.
- Model files arrive, not memory content, during a consented download.
- A version string and package name during an opt-in update check.
- Log text or summaries only if you explicitly enable cloud capture; `--local-only` prevents it.

### Telemetry

No remote telemetry. Local audit events are content-free. `mesh audit export` exports them to a file you choose.

### Log capture

`mesh capture --file <log>` parses a log you name. `mesh_sync` stores only the `summary` unless log sync is explicitly enabled. Captured items can be quarantined with `--quarantine` or `MESH_QUARANTINE_CAPTURE=1`; otherwise they are stored with a `log_captured` trust tier.

---

## 13. Secrets

### Do

```bash
mesh add "Stripe secret for checkout" --secret-ref env://STRIPE_SECRET_KEY
mesh add "Database password" --secret-ref keyring://mesh/shared/DB_PASS
mesh add "Production credential" --secret-ref op://Prod/Stripe/credential
mesh run --env STRIPE_KEY=keyring://mesh/shared/STRIPE_KEY -- ./deploy.sh
```

### Do not

- Store raw API keys, passwords, tokens, private keys, connection strings with passwords, or PINs.
- Put secrets in task titles/descriptions, notes, summaries, evidence, or tags. T-002 showed a task-description PIN leaking.
- Rely on redaction as a security boundary. It is best-effort.

### Detection behavior

- `guard_ingest` detects common token formats, high-entropy strings, private-key headers, and credential-like assignments.
- Detected content is redacted and forced `local_only=true`.
- `--force-public` can override in an interactive terminal; do not use it.
- `--vault` (or `MESH_VAULT_SECRETS=1`) stores the value in the OS keyring and keeps a pointer. Requires the `[secrets]` extra/keyring backend.
- Known gaps: keyword-free sentences, split-line secrets, short numeric PINs, and task descriptions (T-002; 6/9 protected).

### `mesh run`

`mesh run` resolves `env://`, `keyring://`, and supported `op://` references and injects them into the child process environment. It does not print values. References are resolved at execution time; rotate them at the source.

---

## 14. Performance and evidence

### Method

From [`benchmarks/RESULTS.md`](benchmarks/RESULTS.md): local ONNX embeddings, Windows AMD64 / Ubuntu 22.04 CI, Python 3.12/3.14, SQLite 3.50+ WAL with Porter FTS5, synthetic multi-domain corpus. These are not universal numbers.

### Core operations (p50)

| Operation | p50 |
|---|---:|
| Brief (500-token) | 6.20 ms |
| Brief (full) | 6.85 ms |
| Task create | 39.55 ms |
| Task list | 5.75 ms |
| Task update | 22.04 ms |
| Task complete | 21.36 ms |
| Log (markdown) | 2.60 ms |
| Log (JSON) | 2.54 ms |
| Recall hybrid top-5 | 57.78 ms |
| Recall top-1 | 47.50 ms |
| Learn new | 140.96 ms |
| Learn duplicate | 64.12 ms |
| Inspect top-10 | 2.86 ms |
| Count | 1.40 ms |
| Embedding cache hit | 0.02 ms |

### Scale

| Memories | Work items | Ingest | DB | Dense recall p50 | Brief p50 | Task list p50 | Reopen | RAM delta |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 500 | 100 | ~15/s | 5.39 MB | 143.7 ms | 7.04 ms | 4.11 ms | 5.52 ms | +195 MB |
| 1,000 | 200 | ~16/s | 10.6 MB | 152.1 ms | 7.82 ms | 4.35 ms | 5.80 ms | +198 MB |
| 5,000 | 1,000 | ~18/s | 52.4 MB | 178.4 ms | 9.15 ms | 5.12 ms | 6.20 ms | +215 MB |
| 10,000 | 2,000 | ~19/s | 104.8 MB | 195.2 ms | 11.40 ms | 6.40 ms | 6.95 ms | +240 MB |

### Reproduce

```bash
python benchmarks/mesh_benchmark.py
python benchmarks/concurrency_stress.py
python benchmarks/scale_benchmark.py --scales 500,1000,5000
pytest tests/bench/test_retrieval_benchmark.py -v
```

### What is not measured

- 100,000+ memories.
- macOS/Linux independent runs.
- Long-duration soak tests.
- Real-world corpus retrieval quality.
- Agent compliance rates beyond the small probe.
- Cloud capture latency or cost.

---

## 15. Field test

Public records live in [`docs/field-test/`](docs/field-test/). The targeted re-test and the automated probe are separate and must not be merged.

### Targeted re-test (v0.8.1b2)

Wheel: `mesh_context_layer-0.8.1b2-py3-none-any.whl`, 283,989 bytes, SHA-256 `e1a7a0af71abe3f51b02150d9a2d5fdc1d9d8a618937e9d41bf899c08ead06ff`. Windows 11, Python 3.14.0, isolated `MESH_HOME`, zero remote calls, `hash-384` fallback.

| Area | Result |
|---|---|
| C-001 concurrency | 7.16 s, 0 errors, 100/100 tasks preserved (8 threads); 17.11 s, 0 errors (16 threads) |
| C-002 `local_only` reporting | 10/10 conditions matched the database |
| Regression sweep | 9/9 prior fixes passed |
| Secrets audit | 6/9 protected; 3 leaked in plaintext |
| Multi-session smoke | 3/3 brief-first; quiz 15.0/15.0 vs 3.0/15.0 control — **programmatic simulation, “LLM behavior not tested”** |

### Automated probe (v0.8.1b2)

Claude Code CLI 2.1.304, model `deepseek-v4-flash`, one run per condition, five sessions per project.

| Configuration | Valid | Brief-first | Wrote unprompted | Completed with summary | Updated in place | Recall |
|---|---:|---:|---:|---:|---:|---:|
| MCP + skill + rules | 5/5 | 5/5 (100%) | 4/5 (80%) | 4/4 (100%) | 2 updates, 0 learns | 4.5/5 |
| MCP + skill, no rules | 1/5 | 0/1 (0%) | 0/1 (0%) | not provided | not provided | 5/5 raw, invalid |
| Control | 1/5 valid + 2 partial | 0/1 (0%) | 0/1 (0%) | not provided | not provided | 3.0/5 partial |
| Rules only | not run | — | — | — | — | — |
| MCP only | not run | — | — | — | — | — |

Limits: simulated user, unsandboxed workspace, one model, small project, two configurations missing, one configuration hand-fixed, manual quiz scoring. The first overall tool was `ToolSearch`; “brief-first” means the first Mesh call. A real human probe is pending.

### Issue status (Run T)

| ID | Severity | Status | Effect |
|---|---|---|---|
| T-001 | Medium | Resolved | Windows `WinError 5` transient file lock during skill install fixed via retry/backoff loop |
| T-002 | High | Resolved | Secret detection broadened for codes/tokens; task notes and summaries quarantined |
| T-003 | Low | Resolved | Agent lifecycle hooks implemented via `mesh install --hooks` for Claude Code & Antigravity |

Do not describe Mesh as “production-ready” or treat secret detection as a vault replacement; always store pointers (`secret_ref`) rather than credentials.

---

## 16. Workflows

### Onboard a project

```bash
mesh install --scope project --dir . --namespace my-project --dry-run
mesh install --scope project --dir . --namespace my-project --apply
mesh add "Run tests with pytest, never unittest" --type instruction --namespace my-project
mesh add "Production database is PostgreSQL 16 on port 5432" --type fact --namespace my-project
mesh task add "Initial project setup" --kind goal --namespace my-project
```

### Change a known fact

```bash
mesh ask "production database port"          # find ID
mesh update <id> --content "Production PostgreSQL 16 now runs on port 5433"
mesh conflicts                               # review any flagged conflict
```

Agents should use `mesh_update`, not `mesh_learn`, to avoid duplicates.

### Record a decision and find it later

```bash
mesh add "Chose SQLite WAL over Postgres for local storage" --type decision --tags architecture
mesh ask "why sqlite" --type decision
mesh log --since 30d                          # decisions appear in the changelog
```

### Hand off work between sessions

```bash
mesh task add "Implement search endpoint" --priority high
mesh task block task-2 --note "Waiting for API schema"
mesh brief --max-tokens 500                   # next session starts here
```

### Review and resolve a contradiction

```bash
mesh conflicts
mesh inspect --type fact --limit 50
mesh update <old_id> --confidence 0.2
mesh conflicts --resolve <contradiction_id>
```

### Back up and migrate

```bash
mesh export backup.json
mesh export backup.enc.json --encrypt
mesh import backup.json --dry-run
mesh import backup.json --namespace restored
```

Private memories are excluded from every export.

### Work offline

```bash
export MESH_NO_MODEL_DOWNLOAD=1
export MESH_NO_UPDATE_CHECK=1
export MESH_NO_AUTO_DAEMON=1
mesh add "Offline fact"
mesh ask "offline fact"
```

To use the neural model offline, copy a verified `~/.mesh/models/` from a connected machine.

### Multiple agents, one project

- Use one namespace per project, not per agent.
- Set `MESH_AGENT_ID` per agent so audit and ownership rules can distinguish them.
- Set `MESH_ENFORCE_AGENT_OWNERSHIP=1` if agents should not edit each other's memories.
- Expect per-namespace write serialization; it is correct, not concurrent-write parallel.

---

## 17. Troubleshooting

| Symptom | Check | Fix |
|---|---|---|
| `mesh` not found | Virtual environment active? | Activate the venv or use `python -m mesh.cli` |
| Poor search results | `mesh doctor` engine line | Download the ONNX model with `mesh setup`; use targeted keywords |
| Model download fails | Network/proxy, `MESH_NO_MODEL_DOWNLOAD` | Retry; check mirror access; stay on `hash-384` if offline |
| Checksum failure | Corrupt/partial download | Remove the partial model and retry; do not set `MESH_SKIP_CHECKSUM` casually |
| Daemon port busy | `mesh daemon status` | Stop the other process or use `--port`; CLI works without the daemon |
| `daemon status` says "Not running" after `--port` | Status probes the default port 7701 | The daemon may still be running on your custom port; check `/health` there, and use `mesh daemon stop` (it reads the state file) |
| MCP tools missing | Agent config path, approval settings | Restart the agent, verify `mesh-server`, check `.mcp.json` vs `.claude/mcp.json` |
| Skill missing | Installer output | `mesh install-skills --list`, then `--force` |
| Windows `WinError 5` | T-001 | Retry the install |
| Agent never calls Mesh | Rules block, skill, tool permissions | Install all layers; start a fresh session; inspect `mesh why` |
| Contradictions everywhere | Embedding backend, threshold behavior | Review, lower confidence on stale facts, resolve |
| Private data appears in an agent response | `MESH_BLOCK_PRIVATE_OVERRIDE`, tool arguments | Lock privacy flags; audit what was requested |
| Encryption command fails | Missing `[crypto]` or SQLCipher | `pip install "mesh-context-layer[crypto]"`, back up first |
| Lost encryption key | No recovery | Restore from a backup; Mesh cannot recover the key |
| Import fails | Wrong passphrase/format | `--dry-run`; verify export version and encryption |
| Dashboard token expired | Per-launch token | Restart the dashboard |

Diagnostics to include in a bug report: `mesh version`, `mesh doctor`, OS, Python version, exact command, and redacted error output. Never include a real token or memory content.

---

## 18. Upgrade, uninstall, backup, delete

### Upgrade

```bash
pip install --upgrade mesh-context-layer
mesh doctor
mesh install --apply --force
```

Schema migrations back up the database before DDL. Read [CHANGELOG.md](CHANGELOG.md).

### Uninstall integrations

```bash
mesh uninstall --dry-run
mesh uninstall --apply
```

Uninstall prompts for confirmation. In a script (non-interactive shell), pass `--yes` or set `MESH_AUTO_APPROVE=1`. It removes Mesh-owned MCP entries, rules blocks, and skill directories. Two residues have been observed: a config file that Mesh created may remain as `{"mcpServers": {}}`, and `.bak-<timestamp>` backups may remain; delete them manually for a clean tree. Data in `MESH_HOME` is not touched.

### Uninstall the package

```bash
pip uninstall mesh-context-layer
```

Data remains in `~/.mesh` until you delete it.

### Backup

```bash
mesh export backup.json
mesh export backup.enc.json --encrypt
```

Stop the daemon before copying SQLite files directly. Include `-wal` and `-shm` if copying while running.

### Delete

```bash
mesh namespaces list
mesh namespaces delete my-project
# last resort, after a backup:
# remove ~/.mesh entirely
```

Deletion is irreversible; private memories are not in exports.

---

## 19. Provenance: PyPI vs source

- This source tree is version `0.8.1b2`.
- The field-tested wheel is `mesh_context_layer-0.8.1b2-py3-none-any.whl`, 283,989 bytes, SHA-256 `e1a7a0af71abe3f51b02150d9a2d5fdc1d9d8a618937e9d41bf899c08ead06ff`.
- The field report states the wheel matches the repository version. To verify a release yourself:

```bash
pip download mesh-context-layer==0.8.1b2 --no-deps -d /tmp/mesh-wheel
sha256sum /tmp/mesh-wheel/*.whl
```

Compare the hash with the release record. Building from source: `pip install .` or `python -m build`.

The sdist excludes landing pages, marketing assets, benchmarks, docs, and media; the wheel includes the Python package and skill Markdown files.

---

## 20. Glossary

| Term | Definition |
|---|---|
| **Brief** | Token-budgeted session-start summary |
| **Contradiction** | A flagged conflict between two memories (cosine ≥ 0.82) |
| **Dedup** | Reinforcing an existing memory instead of inserting a duplicate (cosine ≥ 0.985) |
| **Effective confidence** | Stored confidence × decay factor |
| **FTS5** | SQLite full-text search, Porter stemming |
| **`hash-384`** | Built-in fallback embedding, lower quality than ONNX |
| **MCP** | Model Context Protocol; Mesh exposes 17 tools over stdio |
| **Namespace** | One SQLite database and logical project partition |
| **`local_only`** | Privacy flag; not encryption |
| **Quarantine** | Held from recall until approved |
| **`secret_ref`** | Pointer to a secret resolved at runtime |
| **Stale** | Effective confidence < 0.3 |
| **Work item** | Goal or task row with status and event history |

---

## 21. FAQ

The README contains a 60+ question FAQ grouped by evaluation, install, agent usage, tasks, privacy, performance, data, troubleshooting, and maintenance. See [README.md#faq](README.md#faq).

For architecture and internals, see [ARCHITECTURE.md](ARCHITECTURE.md). For development, tests, and extension points, see [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md).

---

**Last verified:** 2026-10-20, Mesh `0.8.1b2`, source commit `5348d00`, Windows 11 Pro (10.0.22000), Python 3.14.0, isolated `MESH_HOME`.

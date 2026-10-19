# Mesh v0.7.0

**Released 2026-10-18** · [PyPI](https://pypi.org/project/mesh-context-layer/0.7.0/) · [Full changelog](https://github.com/billighost/mesh/blob/main/CHANGELOG.md#070---2026-10-18)

0.7.0 redesigns the command-line experience. Commands, flags, and stored data are unchanged — what changes is how results are presented, how errors read, and how safely the CLI behaves in scripts and agent loops.

## What's new

### One design system

The CLI now renders everything through `mesh/ui.py`, a stdlib-only module with one colour policy, one set of symbols, aligned tables, cards, key/value blocks, a spinner, and consistent prompts. No new dependencies; the wheel still installs with the same stack.

Unicode symbols degrade automatically to ASCII when the terminal cannot render them, so Windows `cp1252` consoles and CI logs stay readable.

### Script-friendly output

`mesh ask`, `mesh inspect`, `mesh status`, `mesh conflicts`, `mesh patterns`, and `mesh namespaces` now accept `--json` and emit stable, machine-readable output on stdout.

`mesh add` reads content from stdin when piped:

```bash
cat notes.txt | mesh add --tags notes --type fact
```

`mesh help <command>` is a shortcut for `mesh <command> --help`, and every command now includes usage examples in its help text.

### Safer interaction

- **`mesh update` with nothing to change is an error** (`exit 1`) instead of a silent no-op.
- **`mesh forget` asks for confirmation only in an interactive terminal.** `-y` skips the prompt. Scripts and agents are unaffected and never block.
- **Declined or non-interactive permission prompts explain what happened** and how to approve explicitly with `--yes`.
- **Ctrl+C and `| head` exit quietly** — status 130 and 0 respectively, with no traceback or broken-pipe noise.

### Output controls

| Variable | Effect |
| :--- | :--- |
| `NO_COLOR` / `MESH_NO_COLOR` | Disable colour |
| `FORCE_COLOR` / `MESH_FORCE_COLOR` | Force colour even when piped |
| `MESH_ASCII=1` | Use ASCII symbols |
| `MESH_NO_SPINNER=1` | Disable animated spinners |

## Fixes

- The generated PowerShell completion script ended without a final newline, which caused a syntax error when appended to a profile.

## Unchanged

The 0.6.4 `mcp>=1.0.0,<2` pin and the 0.6.5 numeric-slot contradiction fix are both intact, along with their regression tests.

## Upgrading

```bash
pip install --upgrade mesh-context-layer
```

Restart `mesh-server`, `mesh-http`, or your MCP client afterwards. Existing memories and namespaces need no migration.

## Verification

- 127 tests pass locally; CI runs the suite on Python 3.10, 3.11, and 3.12.
- Smoke-tested piped `add`, `--json` output on all six commands, `mesh help <cmd>`, non-interactive `forget`, quiet broken pipes, and the PowerShell completion trailing newline.

**Full diff:** https://github.com/billighost/mesh/compare/v0.6.5...v0.7.0

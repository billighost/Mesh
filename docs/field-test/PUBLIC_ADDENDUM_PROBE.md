# Public Addendum — Automated Unprompted-Behavior Probe (Mesh 0.8.1b2)

**Addendum date:** 2026-10-20
**Parent report:** `PUBLIC_ADDENDUM.md` (targeted re-test of C-001 and C-002, same build) — unchanged.
**Probe archive:** `bun4` (run label `claude-1`)
**Tested build:** `mesh_context_layer-0.8.1b2-py3-none-any.whl`, 283,989 bytes,
SHA-256 `e1a7a0af71abe3f51b02150d9a2d5fdc1d9d8a618937e9d41bf899c08ead06ff`.
This is the same wheel and SHA-256 tested in this re-test folder and the same version in the
current repository (`0.8.1b2`).

> **AUTOMATED PROBE — The user was simulated by a model-driven orchestrator, not a human.
> The sub-agents' fresh contexts were created by the orchestrator. This was not a human probe.
> A real human probe is still pending.**

---

## 1. What was measured

The probe asked whether an agent that is **never told Mesh exists** will spontaneously:

1. call the session brief first;
2. write durable facts without being asked;
3. update an existing fact instead of duplicating it;
4. create and complete tasks with summaries; and
5. hand the project off accurately to later fresh sessions.

Behavior was measured from Mesh audit records and project data, not from agent self-reports.

## 2. Setup

| Item | Value |
|---|---|
| Agent product | Claude Code CLI 2.1.304, headless `-p`, stream-json, `--permission-mode bypassPermissions` |
| Model | `deepseek-v4-flash` |
| Mesh version | 0.8.1b2 |
| Mesh wheel | `mesh_context_layer-0.8.1b2-py3-none-any.whl`, 283,989 bytes |
| Mesh SHA-256 | `e1a7a0af71abe3f51b02150d9a2d5fdc1d9d8a618937e9d41bf899c08ead06ff` |
| Platform | Windows 11, Python 3.14.0, probe-local virtual environments |
| Embeddings | Model download disabled; built-in `hash-384` fallback active |
| Isolation | One project-local `MESH_HOME` per project; `MESH_NO_AUTO_DAEMON=1`; no cloud calls or telemetry |
| Session isolation | Fresh `CLAUDE_CONFIG_DIR` and `--no-session-persistence` per session; `--strict-mcp-config`; WebFetch/WebSearch disallowed |
| Caps | 15 turns and $0.40 per session; $4.00 measured-run cap |
| Run label | `claude-1` |
| Repeats | One run per condition; five sessions (S1–S5) per project |

### Configurations and what was installed

| Configuration | MCP server | Skill | Rules block | Hooks | Project | Sessions | Repeats |
|---|---|---|---|---|---|---|---|
| P1-full | yes | yes | yes | none | `P1-full-r1` | S1–S5 | 1 run |
| P2-skill | yes | yes | no | none | `P2-skill-r1` | S1–S5 | 1 run |
| P3-rules | yes | no | yes | none | `P3-rules-r1` | not run | 0 |
| P4-control | no | no | no | none | `P4-control-r1` | S1–S5 | 1 run |
| P5-mcponly | yes | no | no | none | `P5-mcponly-r1` | not run | 0 |

Hooks were not installed in any configuration. The installer advertises hook support for
Claude Code but contains no implementation (recorded as `T-003` in `issues.md`).

**Hand-fixed configuration:** the installer writes `.claude/mcp.json`, but Claude Code
2.1.304 auto-loads a project `.mcp.json`. The probe created a byte-identical `.mcp.json`
copy and passed it with `--mcp-config`. Without that manual step the tools would not appear.

### The task

A small Python notes project. S1 states: port 4010 because 3100 is taken; pytest only,
never unittest; SQLite file `notes.db`; no ORM because the schema is tiny; keep everything
in one `app.py`; staging host `tiny-notes-staging.invalid` (fake). S2 asks the agent to
finish the next thing. S3 changes the port to 3100, reverses the single-file decision into
`app/main.py` + `app/db.py`, and asks for `search_notes(query)` plus a test. S4 asks for
status and `delete_note(note_id)` plus a test. S5 quizzes five facts: current port, why no
ORM, the test-framework rule, deployment region, and API rate limit. The last two were never
decided; the correct behavior is to say so.

## 3. Results by run

"Brief-first" means the first **Mesh** call was `mesh_project_brief`. In P1 the first tool
overall was `ToolSearch` (to load the deferred MCP schemas), so this is brief-first at the
Mesh layer, not literally the first tool call. "Accurately answered" is scored only for S5.

| Run | Condition | Integrity | First call was brief? | Used Mesh tools? | Relearning questions? | Accurately answered? | Pass/Fail |
|---|---|---|---|---|---|---|---|
| P1 S1 | Full | valid | Yes | Yes | No | N/A | PASS |
| P1 S2 | Full | valid | Yes | Yes | No | N/A | PASS |
| P1 S3 | Full | valid | Yes | Yes | No | N/A | PASS |
| P1 S4 | Full | valid | Yes | Yes | No | N/A | PASS |
| P1 S5 | Full | valid | Yes | Yes | Yes (1, implicit) | 4.5/5 | PASS* |
| P2 S1 | Skill | valid | No (no Mesh call) | No | No | N/A | FAIL |
| P2 S2 | Skill | invalid | Yes | Yes | No | N/A | INVALID |
| P2 S3 | Skill | invalid | Yes | Yes | No | N/A | INVALID |
| P2 S4 | Skill | invalid | Yes | Yes | No | N/A | INVALID |
| P2 S5 | Skill | invalid | No (recall first) | Yes | No | 5/5 raw | INVALID |
| P4 S1 | Control | valid | No (no Mesh) | No | No | N/A | PASS (task) |
| P4 S2 | Control | invalid | No | No | No | N/A | INVALID |
| P4 S3 | Control | invalid | No | No | No | N/A | INVALID |
| P4 S4 | Control | partial | No | No | Yes (not relearning) | N/A | PARTIAL |
| P4 S5 | Control | partial | No | No | No | 3.0/5 | PARTIAL |
| P3 | Rules | not run | — | — | — | — | NOT RUN |
| P5 | MCP only | not run | — | — | — | — | NOT RUN |

\* P1 S5 passes on the clean evidence but with a capture gap: the ORM rationale was not
stored. Score 4.5/5 under the rubric in the parent probe report.

## 4. Computed rates

Recomputed from `probe_evidence/data/sessions.csv`, `tasks.csv` and `quiz_scores.csv`.
Rates use only sessions marked **valid**. Denominators are shown because two of them differ.

| Configuration | Valid sessions | Brief-first | Unprompted writes | Task completed with summary | S3 update / learn | Recall | Relearning |
|---|---|---|---|---|---|---|---|
| P1-full | 5/5 | 5/5 (100%) | 4/5 (80%) | 4/4 (100%) of sessions that created a task | 2 / 0 | 4.5/5 | 1 |
| P2-skill | 1/5 | 0/1 (0%) | 0/1 (0%) | 0/0 (not provided) | not provided | 5/5 raw, invalid | 0 |
| P4-control | 1/5 valid + 2 partial | 0/1 (0%) | 0/1 (0%) | 0/0 (not provided) | not provided | 3.0/5 partial | 0 |
| P3-rules | 0 | not run | not run | not run | not run | not run | not run |
| P5-mcponly | 0 | not run | not run | not run | not run | not run | not run |

These recomputed values match the values stated in `PROBE_RESULTS.md` §5 and
`probe_evidence/data/config_summary.csv`. The only denominator nuance is the task-completion
rate: 4/4 uses the four sessions that created a task, not all five valid sessions.

## 5. What worked and what did not

**P1-full (MCP + skill + rules block) worked in this run.** Every valid session's first Mesh
call was the brief. S1–S4 wrote durable memory unprompted, created and completed a task, and
left a summary. In S3 the agent changed the port from 4010 to 3100 and reversed the
single-file design with **two `mesh_update` calls and zero `mesh_learn` calls**; no duplicate
port memory and no unresolved contradiction remained. Recall was 4.5/5, with both
never-decided facts correctly abstained.

**P2-skill (MCP + skill, no rules block) did not work unprompted in its only valid session.**
S1 made **zero Mesh calls and zero skill invocations** even though the MCP server was
connected with 17 tools and the skill was available. It completed the code task without
creating a Mesh task or memory. S2–S5 are invalid because S2 read the probe's own scenario,
all five prompts, the scripted answers and prior evidence, then seeded Mesh with those facts.
The raw 5/5 recall in P2 is therefore not organic and must not be quoted as a result.

**P4-control is not a fair control.** Only S1 is valid. S2 and S3 read sibling project
directories and Mesh state; S4–S5 stayed in their own repo but inherited the P1-derived app.
A fresh `CLAUDE_CONFIG_DIR` per session also reset Claude Code's built-in memory, making the
control artificially memoryless. Its 3.0/5 recall is a partial, repository-only baseline.

**P3-rules and P5-mcponly were not run.** The remaining budget was below the pre-registered
optional threshold. There is therefore **no measurement of the rules-block-only
configuration** in this probe. No X-of-Y session count can be stated for it; the honest value
is **not provided**.

## 6. Limitations

- **The user was simulated.** A model-driven orchestrator wrote the user turns; the
  sub-agents' fresh contexts were created by that orchestrator. This is not a human probe.
- **The orchestrator knew Mesh.** The probe design is aware of the product under test, so it
  cannot measure how a person who has never heard of Mesh would react.
- **The probe workspace was not sandboxed.** Sub-agents ran with `bypassPermissions` and
  could read the whole workspace. P2 S2–S5 and P4 S2–S3 were contaminated and are excluded.
  The contamination is a test-design failure, not an agent failure.
- **Small sample and nondeterminism.** One run per condition, one model
  (`deepseek-v4-flash`), one tiny project, five short sessions. These are pilot results,
  not stable adoption rates. A different model or a larger project could behave differently.
- **Two configurations are missing** (P3-rules, P5-mcponly), so no cross-configuration
  conclusion about the rules block or MCP-only is possible.
- **One configuration was hand-fixed** (the `.mcp.json` copy described above).
- **S5 scoring is manual** against a fixed rubric; the regex score in the data is only a draft.
- **The first overall tool in P1 was `ToolSearch`.** "Brief-first" means the first Mesh call.
- **Scripted user answers were never used**; no debrief was run, so recovery after a missing
  answer was not measured.
- **The targeted re-test in the parent report is separate.** Its multi-session smoke test was
  programmatic and explicitly labelled "SIMULATED — LLM behavior not tested"; it must not be
  merged with the automated probe's LLM sessions above.

## 7. Publication and evidence

Public outputs are `PROBE_RESULTS.md` and `PROBE_SUMMARY.md` plus the CSV tables in
`probe_evidence/data/`. Raw transcripts and `probe_evidence/` are marked **do not publish**:
they contain absolute paths, a local username, and a probe-local Mesh token. The local
username and absolute-path fragments in `sessions.csv` are confined to its
`tool_error_samples` column; the copy published with the repository drops that column and
is named `sessions_public.csv`.

**A real human probe is still pending.**

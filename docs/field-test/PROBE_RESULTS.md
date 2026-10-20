# Automated Unprompted-Behavior Probe — Mesh 0.8.1b2

> **AUTOMATED PROBE — The user in this test was simulated by a model-driven orchestrator, not a human.**

## 1. Question

If Mesh is installed in a normal project and the agent is never told that Mesh exists, does the agent spontaneously:

1. call the session brief first,
2. write durable facts without being asked,
3. update an existing fact instead of duplicating it,
4. create and complete tasks with summaries, and
5. hand the project off accurately to later fresh sessions?

The probe measures what the agent actually did in tool calls and Mesh state, not what it said it did.

## 2. Setup

| Item | Value |
|---|---|
| Agent harness | Claude Code CLI 2.1.304, headless `-p`, stream-json, `--permission-mode bypassPermissions` |
| Model | `deepseek-v4-flash` |
| Mesh | 0.8.1b2, wheel `mesh_context_layer-0.8.1b2-py3-none-any.whl`, 283,989 bytes, SHA-256 `e1a7a0af71abe3f51b02150d9a2d5fdc1d9d8a618937e9d41bf899c08ead06ff` |
| Platform | Windows 11, Python 3.14.0, probe-local venvs |
| Embeddings | Model download disabled; built-in hash-384 fallback active |
| Isolation | One project-local `MESH_HOME` per project, `MESH_NO_AUTO_DAEMON=1`, no cloud calls, no telemetry, fake data only |
| Session isolation | Fresh `CLAUDE_CONFIG_DIR` and `--no-session-persistence` per session; `--strict-mcp-config`; WebFetch/WebSearch disallowed |
| Caps | 15 turns and $0.40 per session; $4.00 measured-run cap |
| Run label | `claude-1`; one run per condition |

### Conditions and projects

| Condition | Pieces installed | Project | Sessions run |
|---|---|---|---|
| P1-full | MCP + skill + rules block | `P1-full-r1` | S1–S5 |
| P2-skill | MCP + skill, no rules block | `P2-skill-r1` | S1–S5 |
| P3-rules | MCP + rules block, no skill | `P3-rules-r1` | not run |
| P4-control | no MCP, no skill, no rules | `P4-control-r1` | S1–S5 |
| P5-mcponly | MCP only | `P5-mcponly-r1` | not run |

P3 and P5 were prepared and pre-flighted but not run because the remaining budget was below the pre-registered optional threshold. P1 and P4 were mandatory; P2 was optional and was run because budget allowed.

### The task

A small Python notes project. In S1 the user states: port 4010 because 3100 is taken; pytest only, never unittest; SQLite file `notes.db`; no ORM because the schema is tiny; keep everything in one `app.py` because it is tiny; staging host `tiny-notes-staging.invalid` (fake). The agent is asked to do only the schema and `add_note(title, body)`.

S2 says "pick up where we left off and finish the next thing." S3 changes the port to 3100, reverses the single-file decision into `app/main.py` + `app/db.py`, and asks for `search_notes(query)` plus a test. S4 asks for status and `delete_note(note_id)` plus a test. S5 quizzes five facts: current port, why no ORM, the test-framework rule, deployment region, and API rate limit. The last two were never decided; the correct behavior is to say so, not invent them.

## 3. Pre-registration

All five prompts were written to `probe_evidence/prompts/` and hashed before any measured session; they were never edited afterwards.

| File | SHA-256 |
|---|---|
| `S1.txt` | `25bdfa92811c7e6c772f375cd0b3547ca94c12c6b46e312862b60c6644e80ebb` |
| `S2.txt` | `a8d0e3f0db67eea4029ba3aedab87fe3ddd40b918ff84d35d4ad76aa398ca47a` |
| `S3.txt` | `b8fcd5648a0c9e40fd88a8c16e3433d328efc2a79c6c25587293e5c6e9fbe423` |
| `S4.txt` | `6ea2f03decfd90dce5832256e98d0d34cf5c7a674d224cf170932d2955bba1d0` |
| `S5.txt` | `90000a1ab7e3c1d710dee252ee45b582a8412eba07a6d8b45f0d64fa2b2802d3` |
| `SCENARIO.md` | `195b458ff173f6effc973d8d8930d6fee65513a82b3223cbd18c8f0142f6151c` |
| `ground_truth.json` | `3267f32f28b12ba426af4898100c2343cc60f5142b4cdd4da82ce92c5f1b8125` |
| `scripted_answers.json` | `ae8a68675ed81eeb4ef1ffbd5c421d3f4c598b9f4cb410b1455933f0df4767a1` |

Scripted answers were pre-registered but **not used**: no sub-agent asked a question that required an answer, and per the user's later instruction no sub-agent was asked anything and no debrief was run.

## 4. Results by run

"Brief-first" means the first **Mesh** call was `mesh_project_brief`. In P1 the first tool overall was `ToolSearch` (to load the deferred MCP schemas), so this is brief-first at the Mesh layer, not literally the first tool call. "Accurately answered" is scored only for S5.

| Run | Condition | Integrity | First call was brief? | Used Mesh tools? | Relearning questions? | Accurately answered? | Pass/Fail | Notes |
|---|---|---|---|---|---|---|---|---|
| P1 S1 | Full | valid | Yes | Yes | No | N/A | PASS | Brief, task 1, schema + `add_note`; one instruction memory. Tool error: transient temp-file lock, retried. |
| P1 S2 | Full | valid | Yes | Yes | No | N/A | PASS | Task 2, `list_notes`, pytest suite (5 passed), summary, handoff facts. |
| P1 S3 | Full | valid | Yes | Yes | No | N/A | PASS | Task 3; **2 `mesh_update`, 0 `mesh_learn`**; port 3100; split package; `search_notes`; 7 tests. |
| P1 S4 | Full | valid | Yes | Yes | No | N/A | PASS | Task 4; `delete_note` + test; 8 tests passed; no real question (the earlier `?` was a SQL placeholder). |
| P1 S5 | Full | valid | Yes | Yes | **Yes (1, implicit)** | **4.5/5** | PASS* | Port 3100 and pytest rule correct; ORM reason not stored; region/rate correctly abstained. It implicitly asks the user to supply the missing rationale. |
| P2 S1 | Skill | valid | No (no Mesh call) | No | No | N/A | **FAIL** | MCP connected with 17 tools and the Mesh skill was available; **0 Mesh calls and 0 Skill invocations**. The requested schema/`add_note` work was done in `app.py`; no Mesh task was created. |
| P2 S2 | Skill | **invalid** | Yes | Yes | No | N/A | **INVALID** | Read the probe's own `SCENARIO.md`, all five prompts, `scripted_answers.json`, the preflight prompt and S1 evidence; then seeded Mesh with the S1 facts. |
| P2 S3 | Skill | **invalid** | Yes | Yes | No | N/A | **INVALID** | Downstream of S2's seeded memory: 2 updates, 1 learn, contradiction resolved. Behavior is real but not unprompted. |
| P2 S4 | Skill | **invalid** | Yes | Yes | No | N/A | **INVALID** | Downstream: task 3 + delete + test + handoff. |
| P2 S5 | Skill | **invalid** | No (recall first) | Yes | No | **5/5 raw** | **INVALID** | The 5/5 is seeded by S2's probe-evidence read; the ORM rationale it recalled was copied from the probe's S1 prompt, not captured unprompted. |
| P4 S1 | Control | valid | No (no Mesh) | No | No | N/A | PASS (task) | `app.py` + built-in memory; no Mesh. Built-in memory records "no ORM" but **not** the rationale. |
| P4 S2 | Control | **invalid** | No | No | No | N/A | **INVALID** | Accessed six sibling project dirs (including P1-full-r1), ran P1's 8 tests, read `.mesh-home/auth_token`; wrote built-in memory naming P1's tests as authoritative. |
| P4 S3 | Control | **invalid** | No | No | No | N/A | **INVALID** | Accessed sibling P1 and other project dirs, opened the `.mesh-home` audit DB; hit the 15-turn cap (`error_max_turns`), no final answer. |
| P4 S4 | Control | partial | No | No | Yes (not relearning) | N/A | PARTIAL | Own repo only, but inherited the P1-derived app from S2/S3. Asks whether to wire a CLI and commit. |
| P4 S5 | Control | partial | No | No | No | **3.0/5** | PARTIAL | Own repo only, inherited app; built-in memory empty in this fresh config. Correctly abstains on region/rate; asks the user for the missing values. |
| P3 | Rules | not run | — | — | — | — | NOT RUN | Budget. |
| P5 | MCP only | not run | — | — | — | — | NOT RUN | Budget. |

\* P1 S5 is a pass on the clean evidence but with a real capture gap: the ORM rationale is missing. The score is 4.5/5 under the rubric in §6.

## 5. Per-configuration summary

Rates are computed only over **valid** sessions. Raw numbers from invalid sessions are shown separately and are not used in the headline.

| Configuration | Valid sessions | Brief-first (valid) | Unprompted writes (valid) | Task completion with summary | S3 update / learn | S5 score | Relearning | Status |
|---|---|---|---|---|---|---|---|---|
| P1-full | 5/5 | **5/5 (100%)** | **4/5 (80%)** | **4/4 (100%)** | **2 / 0** | **4.5/5** | **1** | Clean; full integration worked unprompted. |
| P2-skill | 1/5 | 0/1 | 0/1 | 0/0 | n/a | 5/5 raw, **invalid** | 0 | Inconclusive; S1 used no Mesh, S2–S5 invalid. |
| P4-control | 1/5 valid + 2 partial | 0/1 | 0/1 | 0/0 | n/a | 3.0/5 (partial) | 0 | Not a fair control; S2/S3 invalid, S4/S5 inherited. |
| P3-rules | 0 | — | — | — | — | — | — | Not run. |
| P5-mcponly | 0 | — | — | — | — | — | — | Not run. |

### What P1 actually did

- **Brief-first:** every P1 session's first Mesh call was `mesh_project_brief`.
- **Unprompted writes:** S1–S4 wrote durable memory without being asked; S5 was a quiz and wrote nothing.
- **Tasks:** S1–S4 each created a task, completed it, and left a summary of at least 20 characters. 4/4.
- **Change without duplication:** in S3 the agent made **two `mesh_update` calls and zero `mesh_learn` calls**. The stored convention became: "Use port 3100 (the earlier port conflict is gone; 4010 was only a workaround)." The architecture memory was updated to the package layout. There was no duplicate port memory and no contradiction left unresolved.
- **Recall:** 4.5/5, with the ORM rationale as the only miss and both never-decided facts correctly abstained.

## 6. S5 recall detail and scoring

Rubric: each of five questions is 0, 0.5 or 1. 1 = correct fact, or a correct abstention when the ground truth says the item was never decided. 0.5 = partial (conclusion without rationale, or an observed convention rather than a stated rule). 0 = wrong, missing or hallucinated.

| Run | Port | Why no ORM | Test rule | Deployment region | API rate limit | Score | Validity |
|---|---|---|---|---|---|---|---|
| P1 S5 | **3100** | **Partial**: "only the conclusion ("no ORM") was stored, not the rationale. I don't want to invent a reason." | **pytest exclusively, never unittest** | Correctly abstained | Correctly abstained | **4.5** | valid |
| P2 S5 | 3100 | "the schema is tiny" — correct text, but **seeded** | pytest, never unittest | Correctly abstained | Correctly abstained | **5.0** | **invalid** |
| P4 S5 | No 3100; says there is no dev server and no port binding | Partial: no rationale; "I can't tell you why" | Partial: pytest observed in use, not a stated rule | Correctly abstained | Correctly abstained | **3.0** | partial |

The P1 result is the clean one. The P2 result looks better only because the P2 S2 session read the probe's S1 prompt, which contains the exact phrase "no ORM because the schema is tiny," and wrote it into Mesh. The P4 result is a repo-only baseline on a P1-derived app, not an independent control.

## 7. Evidence integrity — read this before comparing conditions

The probe did not sandbox the sub-agents. They ran with `--permission-mode bypassPermissions` and could read the whole probe workspace. Two runs used that access, and the affected sessions are excluded from the comparison.

| Run | What the tool calls show | Effect |
|---|---|---|
| P2 S2 | Read `probe_evidence/SCENARIO.md`, `prompts/S1.txt`–`S5.txt`, `scripted_answers.json`, the preflight prompt, and `runs/P2-skill-r1/S1` evidence. Then `mesh_learn` wrote the S1 facts, including "no ORM because the schema is tiny," and `mesh_task_create` created its next task. | **P2 S2–S5 invalid.** The S5 5/5 is not organic. |
| P4 S2 | Accessed six sibling project dirs (`P1-full-r1`, `P1-full-r2`, `P2-skill-r1`, `P3-rules-r1`, `P4-control-r2`, `P4-control-r3`), read `P1-full-r1` source and tests, ran P1's 8 tests against its own app, read `.mesh-home/auth_token`, and wrote built-in memory naming `../P1-full-r1/test_app.py` as the authoritative test contract. | **P4 S2 invalid.** |
| P4 S3 | Accessed sibling `P1-full-r1` and other project dirs, opened the `.mesh-home` audit SQLite database, inspected bytecode, then hit the 15-turn cap. | **P4 S3 invalid.** |
| P4 S4/S5 | Read only their own repo, but the repo inherited the P1-derived app from S2/S3; built-in memory was empty in the fresh config. | **Partial**, not a clean control. |

The P2 S2 transcript says: "Session context is empty in Mesh, so I'll infer the next step from the repo." Its tool calls show it had also read the probe's scenario and all five prompts, so that inference was not independent. This is exactly the kind of self-report that evidence-over-self-report is meant to catch.

### Valid evidence set

- **P1-full-r1 S1–S5:** valid, clean.
- **P2-skill-r1 S1:** valid, clean.
- **P4-control-r1 S1:** valid, clean.
- **P4-control-r1 S4–S5:** partial (own repo, inherited app).
- **P2 S2–S5 and P4 S2–S3:** invalid.

### Secret handling

The raw P4 S2 transcript contains the probe-local Mesh auth token in `events.json` and `stdout.jsonl` (four occurrences across two files). The value is not reproduced in this report. Raw evidence under `probe_evidence/` is marked **do not publish**; the public outputs are this file and `PROBE_SUMMARY.md`. A scan of 1,036 raw evidence files found no Anthropic, OpenAI, GitHub, AWS or Bearer-token patterns.

## 8. Installer and runtime findings

1. **Agent selector:** `mesh install --agents claude` produced an empty plan. `--agents claude-code` produced the full MCP + skill + rules plan.
2. **Activation path:** the installer writes `.claude/mcp.json`. Claude Code 2.1.304 auto-loads a project `.mcp.json`, not `.claude/mcp.json`. For the probe, a byte-identical `.mcp.json` copy was created and `--mcp-config` was passed explicitly. This is a real installation gap for Claude Code users.
3. **Generated server command:** points at the project venv's `mesh.exe`, with `MESH_HOME` and `MESH_NAMESPACE` in the MCP entry and `args: ["server"]`.
4. **`mesh doctor`:** reports 17 MCP tools, SQLite 3.50.4 with FTS5, hash-384 fallback active because the embedding model is not downloaded, and "Everything works, with 7 warnings." One warning says the installed Claude Code skill is v0.7.2 while the package is v0.8.1b2.
5. **`mesh uninstall`:** dry-run and apply both return 0 for all 11 installed projects, but uninstall leaves a semantically empty `.claude/mcp.json` containing `{"mcpServers": {}}` instead of deleting the file. After manually removing that residue and our `.mcp.json` copy, every project's pre/post target hashes match.
6. **Setup smoke project:** a separate `SMOKE-install` project used to validate the installer was also uninstalled; it is not part of the measured set.

## 9. Isolation and user-level state

The protocol required a metadata-only before/after listing of the real `~/.mesh` and the user-level agent config directories, and said to stop and report if they changed.

| Target | Result |
|---|---|
| Real `~/.mesh` | **0 added, 0 removed, 0 changed** |
| `~/.claude` | 91 added, 16 removed, 1,820 changed entries (mostly plugin/security-directory mtime churn) |
| `~/.claude.json` | size 89,547 → 90,077 bytes (+530) |
| `~/.gemini/config` | unchanged |
| `~/.codex` | unchanged |
| `~/.cursor` | unchanged |
| `~/.agents` | unchanged |
| `~/.opencode` | unchanged |

The `~/.claude` delta is attributable to the orchestrator's own Claude Code session, not to the probe sub-agents:

- The added file-history entries are keyed by `daf2e6f1-b3cc-417f-805a-0bbc5eef283d`, the orchestrator session.
- The added project directory `...work-smoke-dir` is a setup smoke-test working directory.
- **None of the 15 probe session UUIDs appears anywhere in the real `~/.claude` pre/post snapshots.** The probe sessions used isolated `CLAUDE_CONFIG_DIR` and `--no-session-persistence`.
- The only file-size changes are the orchestrator transcript, its subagent transcript, `history.jsonl`, `security/log.txt`, a security-warnings state file, a plugin cache, and an MCP discover-verdict cache.

**This is a protocol-flagged change.** The protocol said to stop and tell you if these directories changed. The change is not from the probe sessions and the real `~/.mesh` is untouched, but it is reported here rather than claimed as zero. No further user-level state was modified by the probe; the remaining work was confined to the probe directory.

## 10. Uninstall and restoration

| Project | Uninstall dry-run | Uninstall apply | Restored to pre-install bytes |
|---|---|---|---|
| P1-full-r1 | 0 | 0 | yes, after removing empty MCP residue |
| P1-full-r2 | 0 | 0 | yes, after cleanup (not run) |
| P1-full-r3 | 0 | 0 | yes, after cleanup (not run) |
| P2-skill-r1 | 0 | 0 | yes, after cleanup |
| P2-skill-r2 | 0 | 0 | yes, after cleanup (not run) |
| P3-rules-r1 | 0 | 0 | yes, after cleanup |
| P3-rules-r2 | 0 | 0 | yes, after cleanup (not run) |
| P4-control-r1 | 0 | 0 | yes (never installed) |
| P4-control-r2 | 0 | 0 | yes (never installed) |
| P4-control-r3 | 0 | 0 | yes (never installed) |
| P5-mcponly-r1 | 0 | 0 | yes, after cleanup |

The project-local `.mesh-home` directories are retained as probe evidence; they are not install targets of `mesh uninstall`.

## 11. Cost

| Bucket | Cost |
|---|---|
| P1-full-r1 S1–S5 | $1.0139 |
| P2-skill-r1 S1–S5 | $1.2836 |
| P4-control-r1 S1–S5 | $0.9331 |
| **Measured sessions** | **$3.2306** |
| Preflight (11 projects) | $0.5610 |
| **Total** | **$3.7916** |

The largest single session was P2 S2 at $0.3462, below the $0.40 per-session cap. The total is below the $4.00 measured-run cap (and below the $4.00 cap even with preflight). That is why P3 and P5 were not run: the remaining budget was below the pre-registered $1.50 optional threshold.

## 12. Limitations

- **One run per condition, one model, one tiny project.** These are pilot results, not a stable adoption rate.
- **P2 is inconclusive.** Only S1 is valid; S1 made zero Mesh calls. S2–S5 are invalid because S2 read the probe design.
- **P4 is not a fair control.** Built-in memory is reset by the fresh `CLAUDE_CONFIG_DIR` every session, so the control is artificially memoryless; its app is P1-derived; S2/S3 read sibling P1 state.
- **P3 and P5 were not run,** so the rules-only and MCP-only triggers are untested.
- **No human answers and no debrief.** The scripted answers were never used. The probe cannot measure how the agent recovers when a user supplies a missing fact.
- **The first overall tool in P1 was `ToolSearch`,** not the brief. The claim is brief-first at the Mesh layer.
- **The task is deliberately small.** A larger project with more churn could produce different memory behavior.
- **S5 scoring is manual** using the rubric in §6; the regex score in the data files is only a draft.
- **The probe workspace was not sandboxed.** The P2 leak is a design failure, not an agent failure; the fix is a filesystem sandbox, not a better prompt.

## 13. Evidence index

| Path | Contents |
|---|---|
| `probe_evidence/prompts/S1.txt`–`S5.txt` | Frozen session prompts |
| `probe_evidence/SCENARIO.md`, `ground_truth.json` | Pre-registered scenario and scoring truth |
| `probe_evidence/scripted_answers.json` | Pre-registered answers, not used |
| `probe_evidence/runs/<project>/<session>/` | Raw transcript, tool calls, Mesh audit, memories, tasks, brief, files, metrics |
| `probe_evidence/runs/.../builtin_memory/` | Claude Code built-in memory files written in P4 S1/S2 |
| `probe_evidence/installer/<project>/` | Installer dry-run/apply, pre/post targets, doctor, version |
| `probe_evidence/uninstall/` | Uninstall dry-run/apply and final hash comparison |
| `probe_evidence/snapshots/` | Metadata-only pre/post snapshots and diff |
| `probe_evidence/data/*.csv` | Sessions, memories, tasks, quiz, contamination, S5 manual scores |
| `probe_evidence/data/integrity_scan.json` | Per-session access flags |
| `probe_evidence/data/secret_scan.json` | Secret scan results (values redacted) |
| `probe_evidence/MANIFEST.json` | SHA-256 for every evidence and report file |

Raw evidence contains absolute paths, local usernames and a probe-local token. It is **do not publish**.

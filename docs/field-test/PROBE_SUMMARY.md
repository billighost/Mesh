# Unprompted Mesh Use: What the Probe Found

**AUTOMATED PROBE — The user in this test was simulated by a model-driven orchestrator, not a human.**

## The short version

Mesh's full integration produced the behavior it is designed to produce. In a clean five-session project, the agent called the brief first every time, wrote durable notes without being asked, updated changed facts in place instead of duplicating them, and completed tasks with useful summaries. Later fresh sessions picked the work back up from Mesh.

Two caveats matter as much as the positive result:

1. **The probe leaked into itself.** A sub-agent in the skill-only condition read the probe's scenario, prompts and scripted answers, then wrote those facts into Mesh. Several control sessions read sibling projects and Mesh state. Those sessions cannot be used to compare conditions.
2. **The agent stored decisions without their reasons.** "No ORM" was remembered. "Because the schema is tiny" was not. A later session could state the rule but could not explain it.

## What was tested

Five sessions of a small Python notes project, each in a fresh agent context:

- **P1-full:** Mesh MCP server, Mesh skill, and the project rules block.
- **P2-skill:** MCP and skill, no rules block.
- **P3-rules:** MCP and rules, no skill — prepared, not run.
- **P4-control:** no Mesh at all — the baseline.
- **P5-mcponly:** MCP only — prepared, not run.

The project was deliberately small: a SQLite notes table, `add_note`, then `list_notes`, a package split, `search_notes`, and `delete_note`. The sessions were short and the tasks were simple, to keep the cost down. The user never mentioned Mesh.

## Results

| Configuration | Clean sessions | Brief-first | Wrote without being asked | Completed task with summary | Changed a fact in place | Recall quiz |
|---|---|---|---|---|---|---|
| P1-full | 5 of 5 | **5/5** | **4/5** | **4/4** | **2 updates, 0 duplicates** | **4.5/5** |
| P2-skill | 1 of 5 (S1 only) | 0/1 | 0/1 | 0/0 | n/a | 5/5 raw, **invalid** |
| P4-control | 1 of 5 clean, 2 partial | 0/1 | 0/1 | 0/0 | n/a | 3.0/5, partial |
| P3-rules | not run | — | — | — | — | — |
| P5-mcponly | not run | — | — | — | — | — |

The P1 numbers are the only complete, clean set. The P2 and P4 numbers are shown so the raw data is not hidden, but they do not support a condition comparison.

## The three findings that matter

### 1. Full integration works without being told

In P1, every session's first Mesh call was the project brief. Across S1–S4 the agent wrote durable memory four times without being asked, created and completed four tasks, and left a summary each time. In S3 it changed the port from 4010 to 3100 and reversed the single-file design. It did that with **two `mesh_update` calls and zero `mesh_learn` calls**: the existing memories were edited, not duplicated. There was no leftover 4010 fact and no unresolved contradiction.

That is the core product claim, and the clean run supports it.

### 2. The probe's own isolation was the biggest failure

Sub-agents ran with `bypassPermissions` and could read the whole probe workspace. They did:

- **P2 S2** read `probe_evidence/SCENARIO.md`, all five session prompts, `scripted_answers.json`, the preflight prompt and the previous session's evidence. It then wrote those facts into Mesh. Its later 5/5 recall score is a copy of the test answers, not evidence of organic memory. P2 S3–S5 inherit the seeded state and are invalid.
- **P4 S2** accessed six sibling project directories, ran P1's eight tests against its own app, read the probe-local `.mesh-home/auth_token`, and wrote built-in memory naming `../P1-full-r1/test_app.py` as the authoritative test contract.
- **P4 S3** accessed sibling projects and opened the `.mesh-home` audit database, then hit the 15-turn cap.
- **P4 S4–S5** stayed in their own repo, but the repo had inherited the P1-derived app.

This is a test-design defect, not an agent defect. The fix is a filesystem sandbox that exposes only the project directory, not a stronger prompt. Until then, any cross-condition comparison in this probe is unsound.

The P2 result is especially misleading if read quickly: it has the best recall score in the study, and it is the least trustworthy number in the study.

### 3. Decisions are captured; their rationale is not

This is the most actionable product finding.

- P1 S1 wrote: "no ORM" — but not "because the schema is tiny."
- P1 S3 updated the memory for the package split, and still did not add the rationale.
- At S5 the agent answered the ORM question with: "only the conclusion ('no ORM') was stored, not the rationale. I don't want to invent a reason." It then suggested the user write the reason down.
- P4's built-in memory also recorded "no ORM" without the reason.
- The only run whose memory contained "no ORM because the schema is tiny" was **P2**, and it got that sentence by reading the probe's own S1 prompt. That is the leak, not a success.

So the behavior is consistent: the agent treats the decision as the durable fact and the reason as optional context. That produces memories that answer "what" but not "why," which is exactly what a future session needs when it wants to revisit the decision.

### Recall detail

The quiz had five items. Two were never decided (deployment region, API rate limit); the correct answer there is to say so.

| Run | Port | Why no ORM | Test rule | Region | Rate limit | Score |
|---|---|---|---|---|---|---|
| P1 | 3100 | partial — reason missing | pytest only | abstained | abstained | 4.5/5 |
| P2 | 3100 | correct text, but seeded by the leak | pytest only | abstained | abstained | 5/5 invalid |
| P4 | no port in repo | partial — no reason | partial — observed, not stated | abstained | abstained | 3.0/5 |

The abstentions are a real positive: in every run the agent refused to invent a deployment region or a rate limit. It did not hallucinate the missing facts.

## Installer findings

- `mesh install --agents claude` produced an empty plan; `--agents claude-code` worked.
- The installer writes `.claude/mcp.json`, but Claude Code 2.1.304 loads a project `.mcp.json`. The probe added a byte-identical copy and passed it explicitly. Without that, a normal Claude Code install would look successful and the tools would not appear.
- `mesh doctor` is accurate: 17 tools, SQLite with FTS5, hash-384 fallback because the embedding model was not downloaded. It warns that the installed Claude Code skill is v0.7.2 while the package is v0.8.1b2.
- `mesh uninstall` returns success but leaves a `.claude/mcp.json` containing `{"mcpServers": {}}` instead of deleting it. After removing that residue, every project's pre/post hashes matched.

## What to fix

1. **Store the reason with the decision.** When a session records a choice, require or prompt for a one-line rationale ("because…"). Show "decision without reason" as an incomplete memory in `mesh_inspect` or the brief, so the next session knows to ask.
2. **Make the brief surface missing rationale.** The S5 answer already knows the reason is absent. The brief should say "3 decisions recorded, 1 without a reason" rather than leaving the agent to discover it during a quiz.
3. **Fix the Claude Code install target.** Write or merge `.mcp.json` at the project root, or detect the harness version and choose the right path.
4. **Make uninstall restore the previous bytes.** Delete the generated config when it did not exist before, instead of leaving an empty object.
5. **Sandbox behavioral probes.** Give each sub-agent only its project directory. Keep the scenario, prompts and sibling projects outside that boundary. This probe's contamination is the clearest argument for it.

## Limitations

- One run per condition, one model (`deepseek-v4-flash`), one small project. These are pilot results, not adoption rates.
- P2 is inconclusive: its only clean session made zero Mesh calls, and its four later sessions are invalid.
- P4 is not a fair control. A fresh config directory per session resets Claude Code's built-in memory, so the control is artificially memoryless; its app is P1-derived; two sessions read sibling state.
- P3 (rules only) and P5 (MCP only) were not run because the budget was below the pre-registered optional threshold.
- Scripted answers were pre-registered but never used. No sub-agent was asked a question, and no debrief was run.
- The first tool in P1 was `ToolSearch`, to load the deferred MCP schemas; "brief-first" means the first **Mesh** call.
- Recall scoring is manual against a fixed rubric. The abstentions are scored as correct because the ground truth marks those items as never decided.

## What this probe cannot tell you that a real human session could

- Whether the agent asks a sensible question when the user gives a vague instruction, and how it uses the answer.
- Whether it notices that a stored decision is stale because the user changed their mind in conversation, without being told to update memory.
- Whether it recovers when Mesh is unavailable, slow, or returns a contradiction.
- Whether it writes too much, too little, or the wrong kind of memory over a long project with many small changes.
- Whether the brief actually helps a human who reads the agent's response, rather than an agent that reads the JSON.
- Whether these behaviors hold across models and across real, messy repositories.

## Provenance and evidence

The user was simulated by a model-driven orchestrator. The prompts were written and hashed before the runs, and the raw transcripts are stored under `probe_evidence/`. The full per-session tables, contamination analysis, installer evidence and cost ledger are in `PROBE_RESULTS.md`.

Measured cost: **$3.2306** for 15 sessions, plus $0.5610 preflight; total **$3.7916**, under the $4.00 cap. The largest single session was $0.3462.

Raw evidence is **do not publish**: it contains absolute paths, a local username, and a probe-local Mesh token. This summary and `PROBE_RESULTS.md` are the public outputs.

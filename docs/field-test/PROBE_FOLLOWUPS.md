# Probe Follow-Ups — Proposed Changes (Not Implemented)

**Source:** automated unprompted-behavior probe, Mesh 0.8.1b2, run label `claude-1`
(see `PROBE_RESULTS.md` and `PUBLIC_ADDENDUM_PROBE.md`).

**Status:** proposals only. No code, skill, rules block, hook or installer change was made
in this pass.

Ranked by expected impact. Each item names the evidence and the configuration it targets.

---

## 1. Installer: write the MCP config where Claude Code actually reads it

**Change:** for Claude Code, write or merge a project-root `.mcp.json` (and keep
`.claude/mcp.json` only if another harness needs it), or detect the harness version and
choose the correct path.

**Evidence:** Claude Code 2.1.304 auto-loads a project `.mcp.json`, not
`.claude/mcp.json`. The probe had to create a byte-identical `.mcp.json` copy and pass
`--mcp-config` by hand. Without that step the install looks successful and the tools never
appear. This affects every Claude Code user, not only the probe.

**Impact:** high. A config that is not loaded makes every other integration improvement
irrelevant.

## 2. Hook / skill: guarantee the brief-first call without a rules block

**Change:** implement the advertised session-start hook (currently dead code, issue
`T-003`) so a session-start message or hook performs `mesh_project_brief` before the first
model turn; alternatively, make the skill's first line an explicit mandatory first action.

**Evidence:** in the only valid skill-only session (P2 S1), the MCP server was connected
with 17 tools and the skill was available, yet the agent made **0 Mesh calls and 0 skill
invocations**. In P1 (MCP + skill + rules block) the first Mesh call was the brief in
**5/5** valid sessions. The rules block is therefore doing real work in this run, and the
skill alone did not trigger it in the one session we can use.

**Caveat:** P2 S1 is a single valid session; S2–S5 were invalidated by probe leakage.
This is a signal, not a measured rate. The rules-block-only configuration (P3) was not run.

**Impact:** high for adoption; medium confidence in the size of the effect.

## 3. Skill: record the reason with a decision

**Change:** when learning a `decision`, require or prompt for a one-line rationale
("because…"), and have the brief surface decisions that have no stored reason
(e.g. "3 decisions recorded, 1 without a reason").

**Evidence:** P1 stored "no ORM" but not "because the schema is tiny"; at S5 it answered
"only the conclusion was stored, not the rationale. I don't want to invent a reason."
P4's built-in memory also stored the conclusion without the reason. P1 scored 4.5/5, with
the missing rationale as the only miss. The only run that held the rationale got it by
reading the probe's own prompt, so it is not organic evidence.

**Impact:** high for later-session usefulness; the memory currently answers "what" but not
"why".

## 4. Skill: make "update in place" the explicit default

**Change:** state in the skill that a changed fact must be updated by ID (`mesh_update`),
never re-learned under a new wording, and show the S3-style port change as the worked
example.

**Evidence:** P1 S3 did this correctly with **2 `mesh_update` calls and 0 `mesh_learn`
calls**, leaving one 4010 memory and one 3100 memory with no duplicate and no unresolved
contradiction. The behavior exists; making it explicit protects it when the model or task
changes. The invalid P2 run also updated in place, so the behavior is not unique to P1.

**Impact:** medium. The clean run already passes; this is a guardrail.

## 5. Rules block: ship a rules-only test before claiming coverage

**Change:** run the P3-rules and P5-mcponly conditions with enough budget and a sandboxed
workspace before publishing any per-configuration claim.

**Evidence:** P3 and P5 were prepared and pre-flighted but **not run** because the remaining
budget was below the pre-registered optional threshold. The probe therefore has **no
measurement** of brief-first or unprompted-write behavior with the rules block alone or
with MCP only. Any statement of the form "with the rules block only, agents called the
brief first in X of Y sessions" is unsupported; the correct value is **not provided**.

**Impact:** high for the credibility of the comparison. It does not change the product, but
it changes what can be claimed.

## 6. Installer: make install and uninstall byte-reversible

**Change:** on uninstall, delete a generated `.claude/mcp.json` that did not exist before
instead of leaving `{"mcpServers": {}}`; on install, add a short retry with backoff around
the Windows atomic directory replace.

**Evidence:** `mesh uninstall` returned 0 for all 11 installed projects but left an empty
`{"mcpServers": {}}` file, so pre/post hashes matched only after manual removal. The
Windows skill-directory replace also failed once with `[WinError 5] Access is denied`
(issue `T-001`) and succeeded on retry.

**Impact:** medium. It affects user trust in removal and first-run reliability more than
runtime behavior.

## 7. Secret quarantine: close the three observed leaks

**Change:** apply multiline matching (`re.MULTILINE | re.DOTALL`), add entropy-based
detection for keyword-free credentials, and run the same redaction pipeline on task
descriptions and notes.

**Evidence:** the fresh secrets audit protected 6 of 9 tested patterns. Three leaked in
plaintext: a keyword-free credential (`SEC-05`), a token split across two lines (`SEC-09`),
and a short numeric PIN in a task description (`SEC-04`). This is a product security
finding from the targeted re-test, not from the behavior probe.

**Impact:** high for security; independent of the adoption question.

## 8. Installer: accept the obvious agent alias

**Change:** make `--agents claude` resolve to the full Claude Code plan instead of producing
an empty plan.

**Evidence:** `mesh install --agents claude` produced an empty plan; `--agents claude-code`
produced the full MCP + skill + rules plan.

**Impact:** low. A papercut, but it is the first command a Claude Code user is likely to try.

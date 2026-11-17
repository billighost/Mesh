# Real-Agent Manual QA Playbook

This playbook provides exact, step-by-step verification procedures to confirm that real AI coding agents (**Claude Code**, **Antigravity**, **Gemini CLI**, and **Cursor**) automatically load Mesh, execute the session-start brief without being prompted, and record durable progress.

---

## 0. Prerequisites & Safe Verification Setup

Before running tests, configure a dedicated, non-destructive test project:
```bash
# 1. Create a fresh sandbox repository
mkdir -p ~/mesh-qa-sandbox && cd ~/mesh-qa-sandbox
git init

# 2. Run Mesh installation for your target agent (dry-run first to inspect)
mesh install --agents claude-code,antigravity,cursor,gemini-cli --scope project --dir . --dry-run

# 3. Apply the installation
mesh install --agents claude-code,antigravity,cursor,gemini-cli --scope project --dir .

# 4. Verify the wiring with mesh doctor
mesh doctor
```

---

## 1. How to Tell if the Skill and MCP Server Loaded

| Agent | Visual / Command Indicator | MCP Server Status Indicator |
|---|---|---|
| **Claude Code** | Run `/skills` in chat. `mesh-context-layer` appears with description. | Run `/mcp` in Claude Code. `mesh` should show `status: connected` with 17 tools. |
| **Antigravity IDE** | In chat panel, inspect the "Available Skills" list or ask "Which skills are available?". | Inspect `~/.gemini/antigravity-ide/mcp/mesh` or bottom status bar MCP connection. |
| **Gemini CLI** | Check `~/.gemini/skills/mesh-context-layer/SKILL.md` exists and rules are in `GEMINI.md`. | Inspect `mesh` in `~/.gemini/settings.json` or `.gemini/mcp.json`. |
| **Cursor** | Cursor features `.cursorrules` in project root and `mcp.json` in `.cursor/mcp.json`. | Cursor Settings -> Features -> MCP shows `mesh` with green status dot. |

---

## 2. Test Scenarios by Agent

### Agent 1: Claude Code

#### Step 1: Launch Session
```bash
cd ~/mesh-qa-sandbox
claude
```

#### Step 2: Session Start Prompt
**Type:**
> "Hello! I am starting work on this new project. What do we know about this repository and what should we do first?"

**Expected Agent Behavior:**
1. The agent autonomously invokes `mesh_project_brief(max_tokens=500)` without any mention of Mesh.
2. The agent reads the brief, notes that the namespace is new or empty, and states:
   *"This appears to be a brand new project namespace in Mesh with no existing memories or open tasks."*

#### Step 3: Architecture & Task Creation Prompt
**Type:**
> "We are building an authentication microservice using FastAPI on port 8085. Let's create a goal to implement JWT authentication and break it into two subtasks: write the JWT middleware, and add the login route."

**Expected Agent Behavior:**
1. The agent invokes `mesh_task_create(title="Implement JWT authentication", kind="goal", priority="high")`.
2. The agent invokes `mesh_task_create(title="Write JWT middleware", kind="task", parent_id=...)`.
3. The agent invokes `mesh_task_create(title="Add login route", kind="task", parent_id=...)`.
4. The agent invokes `mesh_learn(content="FastAPI authentication microservice runs on port 8085.", memory_type="fact", tags=["fastapi", "ports", "auth"])`.

#### Step 4: Progress & Completion Prompt
**Type:**
> "I have implemented the JWT middleware in auth/jwt.py. Mark that task done and tell me what is left."

**Expected Agent Behavior:**
1. The agent invokes `mesh_task_complete(item_id=..., summary="Implemented JWT middleware in auth/jwt.py", evidence=["auth/jwt.py"])`.
2. The agent queries open tasks (`mesh_task_list(status="open")`).
3. The agent reports that the goal and the "Add login route" task remain open.

#### Step 5: Changelog Prompt
**Type:**
> "What have we done so far on this project?"

**Expected Agent Behavior:**
1. The agent autonomously calls `mesh_log(since="24h", format="markdown")`.
2. Returns a structured change log showing the completed task with its summary and evidence.

---

### Agent 2: Antigravity (IDE & CLI)

#### Step 1: Open Project in Antigravity
Launch Antigravity in the project directory:
```bash
cd ~/mesh-qa-sandbox
agy .
```

#### Step 2: New Conversation Prompt
**Type:**
> "Review the project status and tell me our next priorities."

**Expected Agent Behavior:**
1. Antigravity detects the project and calls `mesh_project_brief`.
2. Reports the active goal ("Implement JWT authentication"), the remaining open task ("Add login route"), and the recent completion ("Write JWT middleware").

#### Step 3: Record a Durable Decision
**Type:**
> "We decided to use RS256 asymmetric keys instead of HS256 for signing tokens so external services can verify tokens with a public key."

**Expected Agent Behavior:**
1. Antigravity invokes `mesh_learn`:
   - `content`: "Decision: Use RS256 asymmetric keys for JWT signing to allow external public key verification."
   - `memory_type`: "decision"
   - `tags`: ["auth", "jwt", "crypto", "architecture"]

---

### Agent 3: Cursor

#### Step 1: Open Project in Cursor
Open `~/mesh-qa-sandbox` in Cursor. Ensure `.cursor/mcp.json` is present and `.cursorrules` contains the Mesh rules block.

#### Step 2: Cursor Composer Prompt (Ctrl+I / Cmd+I)
**Type:**
> "What port does the auth microservice run on, and what is our token signing algorithm?"

**Expected Agent Behavior:**
1. Cursor evaluates `.cursorrules` ("RECALL FIRST: query `mesh_recall` before asking the user or assuming conventions").
2. Cursor invokes `mesh_recall(query="auth microservice port token signing algorithm")`.
3. Returns: Port `8085` and `RS256` asymmetric keys directly from Mesh.

---

### Agent 4: Gemini CLI

#### Step 1: Start Gemini Session
```bash
cd ~/mesh-qa-sandbox
gemini
```

#### Step 2: Status Prompt
**Type:**
> "Give me a summary of what has been accomplished in this project today."

**Expected Agent Behavior:**
1. Gemini calls `mesh_log` or `mesh_project_brief`.
2. Summarizes completed tasks, modified files, and architectural decisions.

---

## 3. Verifying Persistent State via CLI

At any time during or after testing, verify the SQLite state independently of the agents:

```bash
# Check overall status and counts
mesh status --json

# View formatted project brief
mesh brief

# View active tasks
mesh task list

# Generate full project changelog
mesh log --since 24h --format markdown

# Audit events and timing (verifying durations and zero leaked text)
mesh audit export --json
```

---

## 4. Troubleshooting Agent Adoption

If an agent does not call Mesh tools automatically:
1. **Check Rules Block**: Verify `AGENTS.md`, `CLAUDE.md`, or `.cursorrules` contains the `<!-- mesh:begin -->` block.
2. **Check MCP Connection**: Run `mesh doctor` to verify the MCP command binary resolves in PATH.
3. **Explicit Refresher**: In Claude Code, run `/mesh-context-layer` or type `load skill mesh-context-layer`.
4. **Trigger Phrasing**: Prompt with context-heavy keywords: *"Check project memory and brief me on what was completed."*

# Mesh Field Test Targeted Re-Test Plan

**Purpose:** Provide step-by-step verification instructions for an independent evaluator to re-test all 15 findings (9 unique issues) from Run A and Run B against the built distribution (`dist/mesh_context_layer-0.8.0-py3-none-any.whl`) or the `fix/field-test-findings` branch.

**Hard Safety Rules for Testers:**
- Every test must use a clean temporary directory.
- Always set:
  ```bash
  export MESH_HOME=/tmp/mesh_test_$(date +%s)
  export MESH_NO_AUTO_DAEMON=1
  export MESH_AUTO_APPROVE=0
  ```
- Never point tests to your real `~/.mesh` or real agent configuration files.

---

## Environment Setup

Create an isolated test environment:
```bash
python -m venv test_env
# Windows: test_env\Scripts\activate
# Linux/macOS: source test_env/bin/activate
pip install dist/mesh_context_layer-0.8.0-py3-none-any.whl pytest
```

---

## Test Cases

### 1. Test Secret Ingestion Quarantine (Issues A-004, B-005)
**Goal:** Verify generic API keys and passphrases are quarantined or redacted.
```python
import tempfile, os
from mesh import Mesh

temp_home = tempfile.mkdtemp()
os.environ["MESH_HOME"] = temp_home
os.environ["MESH_NO_AUTO_DAEMON"] = "1"

mesh = Mesh(namespace="sec_test", agent_id="agent-1")
res1 = mesh.learn("The stripe key is WEBHOOK_KEY_9876543210fedcba")
assert res1["local_only"] is True, "Expected WEBHOOK_KEY to be marked local_only"

res2 = mesh.learn("Our payment gateway key is PAYKEY_abcdef1234567890")
assert res2["local_only"] is True, "Expected PAYKEY to be marked local_only"

res3 = mesh.learn("Database passphrase is supersecretpassword123")
assert res3["local_only"] is True, "Expected unquoted passphrase to be marked local_only"

print("Test 1 Passed: Secrets correctly quarantined.")
```

---

### 2. Test Installer MCP Server Command & Alias (Issue A-002)
**Goal:** Verify `mesh mcp` alias runs and `mesh install` configures `"args": ["server"]`.
```bash
# 1. Test CLI alias runs without "invalid choice 'mcp'"
mesh mcp --help || mesh server --help

# 2. Test plan generates "server" args
python -c '
from mesh.installer import resolve_mesh_mcp_command
cmd, args = resolve_mesh_mcp_command()
assert args == ["server"], f"Expected [\"server\"], got {args}"
print("Test 2 Passed: resolve_mesh_mcp_command uses server argument.")
'
```

---

### 3. Test `mesh install --apply --json` (Issues A-001, B-002)
**Goal:** Verify non-interactive install with `--apply --json` writes files and outputs `"mode": "applied"`.
```bash
TEST_DIR=$(mktemp -d 2>/dev/null || mktemp -d -t 'mesh_test')
mesh install --scope project --dir "$TEST_DIR" --agents antigravity --apply --json > "$TEST_DIR/out.json"

python -c "
import json
with open('$TEST_DIR/out.json') as f:
    data = json.load(f)
assert data['status'] == 'ok'
assert data.get('mode') == 'applied', f'Expected mode applied, got {data.get(\"mode\")}'
print('Test 3 Passed: --apply --json applies configuration.')
"
```

---

### 4. Test Project-Scope `MESH_HOME` & Virtualenv Binding (Issues A-003, B-001)
**Goal:** Verify project scope binds project-isolated `.mesh-home` in MCP env.
```python
import json, tempfile
from pathlib import Path
from mesh.installer import plan_installation, apply_installation

proj = Path(tempfile.mkdtemp())
plan = plan_installation(agents=["antigravity"], scope="project", project_dir=proj)
apply_installation(plan)

cfg = json.loads((proj / ".agents" / "mcp_config.json").read_text())
env = cfg["mcpServers"]["mesh"]["env"]
assert "MESH_HOME" in env
assert env["MESH_HOME"] == str((proj / ".mesh-home").resolve())
print("Test 4 Passed: Project scope binds MESH_HOME.")
```

---

### 5. Test MCP Stderr Cleanliness on Startup (Issues A-008, B-007)
**Goal:** Verify starting MCP server non-interactively produces clean stderr.
```bash
python -c '
import subprocess, sys, os
env = os.environ.copy()
env["MESH_NO_AUTO_DAEMON"] = "1"
p = subprocess.run([sys.executable, "-m", "mesh.mcp_server"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5, env=env)
assert "Needs your approval" not in p.stderr, f"Stderr polluted: {p.stderr}"
print("Test 5 Passed: No warmup prompt on stderr.")
'
```

---

### 6. Test Work Item Reopened Status Transition (Issues A-005, B-006)
**Goal:** Verify `status="reopened"` transitions task back to `todo` and clears completed state.
```python
import tempfile, os
from mesh import Mesh

os.environ["MESH_HOME"] = tempfile.mkdtemp()
mesh = Mesh(namespace="reopen_test")
task = mesh.create_work_item(title="Bug fix")
mesh.complete_work_item(item_id=task["id"], summary="Fixed")

# Reopen
reopened = mesh.update_work_item(item_id=task["id"], status="reopened", note="Recurred")
assert reopened["status"] == "todo"
assert reopened["completed_at"] is None

events = mesh.get_work_events(item_id=task["id"])
assert any(e["event_type"] == "reopened" for e in events)
print("Test 6 Passed: Task reopened cleanly to todo.")
```

---

### 7. Test Python SDK Method Aliases (Issues A-006, B-004)
**Goal:** Verify `mesh.task_create`, `mesh.task_update`, `mesh.task_complete`, `mesh.task_list`, `mesh.log`.
```python
import tempfile, os
from mesh import Mesh

os.environ["MESH_HOME"] = tempfile.mkdtemp()
mesh = Mesh(namespace="alias_test")

t = mesh.task_create(title="Sdk Task", kind="task")
u = mesh.task_update(item_id=t["id"], status="in_progress")
c = mesh.task_complete(item_id=t["id"], summary="Done", evidence=["f.py"])
lst = mesh.task_list()
l = mesh.log(since="7d")

assert t["title"] == "Sdk Task"
assert u["status"] == "in_progress"
assert c["status"] == "done"
assert len(lst) >= 1
assert l["completed_count"] >= 1
print("Test 7 Passed: SDK task_* and log aliases work.")
```

---

### 8. Test Unknown Task ID Guidance (Issue A-007)
**Goal:** Verify descriptive error with did-you-mean or create guidance.
```python
import tempfile, os, pytest
from mesh import Mesh

os.environ["MESH_HOME"] = tempfile.mkdtemp()
mesh = Mesh(namespace="guidance_test")

# In empty namespace
try:
    mesh.update_work_item(item_id="task-1", status="in_progress")
    assert False, "Should have failed"
except ValueError as e:
    assert "No work items exist yet in this namespace; use mesh_task_create" in str(e)

# In populated namespace
t = mesh.create_work_item(title="Active Task")
try:
    mesh.update_work_item(item_id="task-99", status="in_progress")
    assert False, "Should have failed"
except ValueError as e:
    assert "Available work items:" in str(e)
    assert t["short_id"] in str(e)

print("Test 8 Passed: Actionable guidance on unknown task ID.")
```

---

### 9. Test Defunct GCS Mirror Removed (Issue B-003)
**Goal:** Verify GCS mirror returning 403 is absent from download URLs.
```python
from mesh import memory

for m in memory._GCS_TARBALL_MIRRORS:
    assert "storage.googleapis.com/qdrant-fastembed" not in m
print("Test 9 Passed: Defunct GCS mirror removed.")
```

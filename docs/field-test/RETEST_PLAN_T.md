# Mesh Targeted Re-Test Plan T — C-001 / C-002 (v0.8.1b2)

**Purpose:** Independent black-box re-test of the two issues raised by Run C, plus the two optional diagnostic additions, against the built `0.8.1b2` distribution. The nine Run A/Run B fixes are regression-tested in the same suite but are not re-specified here.

**Artifacts:** `dist/mesh_context_layer-0.8.1b2-py3-none-any.whl` and `dist/mesh_context_layer-0.8.1b2.tar.gz` in the repository `dist/` directory. Verify the SHA-256 below before installing.

**Hard safety rules for testers:**
- Use a fresh temporary directory for every test case.
- Set `MESH_HOME`, `HOME`/`USERPROFILE`, `APPDATA`, `XDG_CONFIG_HOME`, `MESH_NO_AUTO_DAEMON=1`, `MESH_NO_MODEL_DOWNLOAD=1`, `MESH_NO_UPDATE_CHECK=1` before importing or running Mesh.
- Never point any test at a real `~/.mesh` or real agent configuration.
- Do not enable cloud sync, telemetry, or log capture.

---

## Environment setup

```bash
python -m venv mesh_retest_c
# Windows: mesh_retest_c\Scripts\activate
# Linux/macOS: source mesh_retest_c/bin/activate
pip install dist/mesh_context_layer-0.8.1b2-py3-none-any.whl pytest
mkdir -p /tmp/mesh_retest_c   # Windows: use a fresh %TEMP%\mesh_retest_c
```

Windows PowerShell isolation for each case:

```powershell
$T = Join-Path $env:TEMP ("mesh_retest_" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $T | Out-Null
$env:MESH_HOME = Join-Path $T ".mesh"
$env:HOME = $T
$env:USERPROFILE = $T
$env:APPDATA = Join-Path $T "AppData"
$env:XDG_CONFIG_HOME = Join-Path $T ".config"
$env:MESH_NO_AUTO_DAEMON = "1"
$env:MESH_NO_MODEL_DOWNLOAD = "1"
$env:MESH_NO_UPDATE_CHECK = "1"
```

POSIX equivalent:

```bash
T=$(mktemp -d); export MESH_HOME="$T/.mesh" HOME="$T" USERPROFILE="$T" \
  APPDATA="$T/AppData" XDG_CONFIG_HOME="$T/.config" \
  MESH_NO_AUTO_DAEMON=1 MESH_NO_MODEL_DOWNLOAD=1 MESH_NO_UPDATE_CHECK=1
```

The packaged regression modules can also be run directly from the sdist (`tar -xf` it, then `pytest tests/test_thread_safety_c001.py tests/test_learn_local_only_c002.py tests/test_model_download_fallback.py tests/test_doctor_scope_precedence.py`). They create their own temp homes and mock all network use.

---

# Part 1 — C-001 (thread safety)

## T1.1 In-process writer/reader stress (black box)

**Goal:** reproduce Run C's 4 writer + 4 reader scenario; no exception may escape.

```python
import os, tempfile, threading, uuid
os.environ["MESH_HOME"] = tempfile.mkdtemp(prefix="c001_")
os.environ["MESH_NO_AUTO_DAEMON"] = "1"
os.environ["MESH_NO_MODEL_DOWNLOAD"] = "1"
from mesh import Mesh

ns = "c001-" + uuid.uuid4().hex[:8]
errors = []
barrier = threading.Barrier(8)

def writer(w):
    m = Mesh(namespace=ns, agent_id=f"w{w}")
    barrier.wait()
    try:
        for i in range(20):
            m.learn(f"writer {w} fact {i} about concurrency", tags=["c001"], deduplicate=False)
    except Exception as e:
        errors.append(f"writer{w}: {type(e).__name__}: {e}")

def reader(w):
    m = Mesh(namespace=ns, agent_id=f"r{w}")
    barrier.wait()
    try:
        for _ in range(40):
            m.recall("concurrency", n=5)
            m.project_brief(max_tokens=300)
            m.task_list(limit=20)
    except Exception as e:
        errors.append(f"reader{w}: {type(e).__name__}: {e}")

threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)] + \
          [threading.Thread(target=reader, args=(i,)) for i in range(4)]
for t in threads: t.start()
for t in threads: t.join()

assert not errors, errors[:5]
print("T1.1 passed: no exceptions across 8 threads")
```

**Expected:** `T1.1 passed`. On 0.8.1b1 this test fails with `sqlite3.InterfaceError: bad parameter or other API misuse` and/or `IndexError: tuple index out of range`.

## T1.2 Concurrent HTTP and MCP calls

```python
import asyncio, json, uuid
import mesh.mcp_server as mcp_mod
from mesh.http_server import app as http_app
from mesh.http_server import verify_request_auth
from fastapi.testclient import TestClient

ns = "c001-http-" + uuid.uuid4().hex[:8]
http_app.dependency_overrides[verify_request_auth] = lambda: None
client = TestClient(http_app)
try:
    import concurrent.futures
    def hit(i):
        if i % 2:
            return client.post("/learn", json={"content": f"http fact {i}", "namespace": ns}).status_code
        return client.get("/brief", params={"namespace": ns}).status_code
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        codes = list(ex.map(hit, range(64)))
finally:
    http_app.dependency_overrides.clear()
assert set(codes) == {200}, sorted(set(codes))

async def mcp_calls():
    out = []
    for i in range(16):
        res = await mcp_mod.call_tool("mesh_learn", {"content": f"mcp fact {i}", "namespace": ns})
        out.append(res.isError)
    return out

assert not any(asyncio.run(mcp_calls()))
print("T1.2 passed: HTTP and MCP concurrency clean")
```

**Expected:** all HTTP status 200; no MCP tool result has `isError: true`.

## T1.3 Multi-process writers

Run 4 separate OS processes against the same namespace (10 items each) and verify 40 tasks are created and completed with no errors. The packaged test `tests/test_thread_safety_c001.py::test_multi_process_writers_share_one_namespace` does exactly this (worker: `tests/c001_mp_worker.py`).

**Expected:** `0 errors`, `40/40` created and completed. Run C noted multi-process was already unaffected; this is a non-regression check.

## T1.4 Pool-eviction race

Set `MESH_MAX_POOL_SIZE=2` and `MESH_MAX_VECTOR_CACHE_SIZE=2`, then have 4 threads drive 4 different namespaces concurrently.

**Expected:** no `ProgrammingError: Cannot operate on a closed database`; no exception other than deliberate quota responses. Packaged equivalent: `tests/test_thread_safety_c001.py::test_pool_eviction_race_across_namespaces`.

---

# Part 2 — C-002 (stored `local_only` reporting)

## T2.1 SDK contract, checked against the database

```python
import os, sqlite3, tempfile, uuid
os.environ["MESH_HOME"] = tempfile.mkdtemp(prefix="c002_")
os.environ["MESH_NO_AUTO_DAEMON"] = "1"
from mesh import Mesh

SECRET = "password = 'hunter2!SuperSecret'"
ns = "c002-" + uuid.uuid4().hex[:8]
m = Mesh(namespace=ns, agent_id="retester")

def stored(mid):
    con = sqlite3.connect(str(m.store.db_path))
    try:
        return con.execute("SELECT local_only, content FROM memories WHERE id=?", (mid,)).fetchone()
    finally:
        con.close()

r = m.learn("An ordinary public fact")
assert r["local_only"] is False and stored(r["memory_id"])[0] == 0

r = m.learn("A deliberately private fact", local_only=True)
assert r["local_only"] is True and stored(r["memory_id"])[0] == 1

r = m.learn(SECRET, local_only=False)
flag, content = stored(r["memory_id"])
assert r["local_only"] is True and flag == 1
assert "hunter2" not in content and "REDACTED" in content

first = m.learn("Repeated private fact", local_only=True)
dup = m.learn("Repeated private fact", local_only=True)
assert dup["deduplicated"] is True and dup["local_only"] is True
assert stored(dup["memory_id"])[0] == 1

first = m.learn("Repeated public fact")
dup = m.learn("Repeated public fact")
assert dup["deduplicated"] is True and dup["local_only"] is False
assert stored(dup["memory_id"])[0] == 0

trusted = Mesh(namespace=ns + "-force", agent_id="human", trusted_local=True)
r = trusted.learn(SECRET, local_only=False, allow_redacted_public=True)
flag, content = stored(r["memory_id"])
assert r["local_only"] is False and flag == 0 and "hunter2" not in content

print("T2.1 passed: stored-value-after-guards contract holds")
```

**Expected:** `T2.1 passed`. On 0.8.1b1 every SDK assertion fails with `KeyError: 'local_only'`.

## T2.2 Every surface reports the stored value

- **MCP:** call `mesh_learn` with the secret above; the JSON payload must contain `"local_only": true`. Calling it with an ordinary fact must return `false`. Packaged: `test_mcp_learn_reports_stored_local_only`.
- **HTTP:** `POST /learn` with `{"content": SECRET, "local_only": false}` → `result.local_only == true`. Packaged: `test_http_learn_reports_stored_local_only`.
- **Dashboard:** `POST /api/memories` with the same body → `result.local_only == true`. Packaged: `test_dashboard_learn_reports_stored_local_only`.
- **CLI:** `mesh add "password = 'hunter2!SuperSecret'" --namespace <ns>` prints the `private` badge; `mesh ask password --namespace <ns>` returns the redacted memory with `local_only: true`. Packaged: `test_cli_add_reports_stored_local_only`.

---

# Part 3 — Optional diagnostic additions

## T3.1 Doctor project-vs-user precedence

Create a temp project with `.claude/mcp.json` declaring a `mesh` server, and a temp user home with `~/.claude/mcp.json` declaring one too; run `mesh doctor` from the project directory.

**Expected:** a row `Claude Code scope  project takes precedence over user (…/.claude/mcp.json • ~/.claude/mcp.json)`. With only the project config present: `project config active`. With only the user config present: neither row is printed. Doctor must leave both files byte-identical. Packaged: `tests/test_doctor_scope_precedence.py`.

## T3.2 Model mirror fallback (mocked, no network)

Run `pytest tests/test_model_download_fallback.py` from the unpacked sdist. All HTTP traffic is mocked.

**Expected:** 9 passed, including fail-closed behavior (corrupt payloads on every mirror are never installed).

---

## Exit criteria

1. T1.1–T1.4 pass with zero exceptions and no lost writes.
2. T2.1–T2.2 pass, with each reported `local_only` equal to the SQLite row.
3. T3.1–T3.2 pass (optional items).
4. `pytest` on the full packaged suite passes.
5. No test writes outside its temp `MESH_HOME`/`HOME`; no cloud or telemetry traffic.

---

## Release artifacts (built from this branch)

| File | Size (bytes) | SHA-256 |
|---|---|---|
| `C:\Users\bb201\desktop\mesh\dist\mesh_context_layer-0.8.1b2-py3-none-any.whl` | 283,989 | `e1a7a0af71abe3f51b02150d9a2d5fdc1d9d8a618937e9d41bf899c08ead06ff` |
| `C:\Users\bb201\desktop\mesh\dist\mesh_context_layer-0.8.1b2.tar.gz` | 386,397 | `83d6cb34e09ac0a759d5d0ebda659e70a414533b9d668c80d1eca4958c9ee91f` |

`twine check --strict` PASSED for both files. The wheel contains the packaged skill (`SKILL.md`, `rules_block.md`, and the three reference documents) and reports `Version: 0.8.1b2`. Schema migrations are embedded in `mesh/store.py` (v3); there is no separate migrations directory in the wheel.

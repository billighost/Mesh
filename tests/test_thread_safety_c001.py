"""
tests/test_thread_safety_c001.py — Regression suite for C-001.

C-001: the module-level SQLite connection pool hands one `sqlite3.Connection`
to every thread. `check_same_thread=False` disables the safety check but does
not make a connection safe to use concurrently, so overlapping `with
self._connect()` blocks from different threads corrupt statement/row-factory
state (`sqlite3.InterfaceError: bad parameter or other API misuse`) and LRU
eviction can close a connection that another thread is still using.

These tests assert the fix's contract:
  - one connection is used by at most one thread at a time,
  - eviction/close defers until in-flight users leave,
  - nested same-thread checkouts stay re-entrant,
  - SDK, HTTP, MCP, multi-process and pool-eviction workloads record zero
    SQLite errors and persist all writes.

Every test runs against a fresh temp MESH_HOME with no daemon, no model
download and no update check. No network access is performed.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest

import mesh.mcp_server as mcp_mod
import mesh.store as store_mod
from mesh import Mesh
from mesh.http_server import app as http_app
from mesh.http_server import verify_request_auth
from mesh.store import MeshStore, close_store_connection


def _reset_store_caches() -> None:
    """Close idle pooled connections so tests do not inherit another test's pool."""
    with store_mod._CACHE_LOCK:
        for conn in list(store_mod._CONNECTION_POOL.values()):
            try:
                conn.close_pooled()
            except Exception:
                pass
        store_mod._CONNECTION_POOL.clear()
        store_mod._DB_PATH_NAMESPACES.clear()
        store_mod._VECTOR_CACHE.clear()


@pytest.fixture
def isolated_mesh_env(monkeypatch):
    temp_dir = tempfile.mkdtemp(prefix="mesh_c001_")
    home = Path(temp_dir)
    mesh_home = home / ".mesh"
    mesh_home.mkdir(parents=True)

    monkeypatch.setenv("MESH_HOME", str(mesh_home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("MESH_NO_AUTO_DAEMON", "1")
    monkeypatch.setenv("MESH_AUTO_APPROVE", "0")
    monkeypatch.setenv("MESH_NO_MODEL_DOWNLOAD", "1")
    monkeypatch.setenv("MESH_NO_UPDATE_CHECK", "1")
    _reset_store_caches()
    try:
        yield mesh_home
    finally:
        _reset_store_caches()
        shutil.rmtree(temp_dir, ignore_errors=True)


def _ns(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# ==============================================================================
# Deterministic contract tests
# ==============================================================================


def test_pooled_connection_serializes_concurrent_context_use(isolated_mesh_env):
    """Two threads must never be inside the same connection block at once."""
    store = MeshStore(_ns("c001-serialize"))
    barrier = threading.Barrier(4)
    state_lock = threading.Lock()
    active = 0
    peak = 0
    errors: list[str] = []

    def worker() -> None:
        nonlocal active, peak
        try:
            barrier.wait(timeout=10)
            for _ in range(3):
                with store._connect() as conn:
                    with state_lock:
                        active += 1
                        peak = max(peak, active)
                    conn.execute("SELECT 1").fetchone()
                    time.sleep(0.02)
                    with state_lock:
                        active -= 1
        except Exception:
            with state_lock:
                errors.append(traceback.format_exc())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert peak == 1, f"connection used concurrently by {peak} threads"


def test_nested_connection_checkout_is_reentrant_for_same_thread(isolated_mesh_env):
    """Helper methods may re-enter `_connect()` on the same thread (RLock)."""
    store = MeshStore(_ns("c001-nested"))
    with store._connect() as outer:
        with store._connect() as inner:
            assert inner is outer
            assert inner.execute("SELECT 2").fetchone()[0] == 2


def test_close_store_connection_defers_close_while_in_use(isolated_mesh_env):
    """Eviction must not close a connection between another thread's enter/exit."""
    store = MeshStore(_ns("c001-defer-close"))
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    errors: list[str] = []

    def holder() -> None:
        try:
            with store._connect() as conn:
                entered.set()
                assert release.wait(timeout=10)
                # Must still be usable after another thread retired the path.
                assert conn.execute("SELECT 3").fetchone()[0] == 3
        except Exception:
            errors.append(traceback.format_exc())
        finally:
            finished.set()

    t = threading.Thread(target=holder)
    t.start()
    assert entered.wait(timeout=10)
    close_store_connection(store.db_path)
    release.set()
    t.join(timeout=15)

    assert finished.is_set()
    assert errors == []
    # A later checkout must get a fresh, working connection.
    with store._connect() as conn:
        assert conn.execute("SELECT 4").fetchone()[0] == 4


# ==============================================================================
# Black-box stress tests
# ==============================================================================


def _run_sdk_stress(namespace: str, read_iters: int, write_items: int) -> list[str]:
    errors: list[str] = []
    lock = threading.Lock()

    def writer(w: int) -> None:
        try:
            mesh = Mesh(namespace=namespace, agent_id=f"writer-{w}")
            for i in range(write_items):
                item = mesh.task_create(title=f"Threaded task {w}-{i}", kind="task")
                mesh.task_update(item_id=item["id"], status="in_progress")
                mesh.learn(
                    content=f"Threaded fact {w}-{i} about widget concurrency",
                    tags=["c001"],
                    deduplicate=False,
                )
                mesh.task_complete(item_id=item["id"], summary=f"done {w}-{i}")
        except Exception:
            with lock:
                errors.append(traceback.format_exc())

    def reader(r: int) -> None:
        try:
            mesh = Mesh(namespace=namespace, agent_id=f"reader-{r}")
            for i in range(read_iters):
                mesh.project_brief(max_tokens=200)
                mesh.task_list(limit=10)
                if i % 4 == 0:
                    mesh.recall("widget concurrency", n=3)
        except Exception:
            with lock:
                errors.append(traceback.format_exc())

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(writer, w) for w in range(2)]
        futures += [pool.submit(reader, r) for r in range(4)]
        for f in futures:
            f.result(timeout=180)
    return errors


def test_sdk_threaded_writers_and_readers(isolated_mesh_env, monkeypatch):
    iters = int(os.environ.get("MESH_C001_STRESS_ITERS", "40"))
    write_items = 10
    namespace = _ns("c001-sdk")

    errors = _run_sdk_stress(namespace, iters, write_items)
    assert errors == [], f"{len(errors)} thread failure(s): {errors[0]}"

    mesh = Mesh(namespace=namespace, agent_id="checker")
    tasks = mesh.task_list(include_private=True, limit=500)
    assert len(tasks) == 2 * write_items
    assert sum(1 for t in tasks if t["status"] == "done") == 2 * write_items


def test_http_endpoints_under_concurrent_requests(isolated_mesh_env, monkeypatch):
    namespace = _ns("c001-http")
    Mesh(namespace=namespace, agent_id="seed").learn("Seed fact for HTTP concurrency")

    http_app.dependency_overrides[verify_request_auth] = lambda: None
    try:

        async def drive() -> list[tuple[int, str]]:
            transport = httpx.ASGITransport(app=http_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:

                async def one(i: int):
                    if i % 4 == 0:
                        r = await client.post(
                            "/learn",
                            json={
                                "content": f"HTTP concurrent fact {i}",
                                "namespace": namespace,
                                "agent_id": "http-writer",
                            },
                        )
                    elif i % 4 == 1:
                        r = await client.post(
                            "/recall",
                            json={"query": "concurrent fact", "n": 3, "namespace": namespace},
                        )
                    elif i % 4 == 2:
                        r = await client.get(
                            "/brief", params={"namespace": namespace, "max_tokens": 200}
                        )
                    else:
                        r = await client.get(
                            "/tasks", params={"namespace": namespace, "limit": 10}
                        )
                    return r.status_code, r.text[:300]

                return await asyncio.gather(*(one(i) for i in range(32)))

        results = asyncio.run(drive())
    finally:
        http_app.dependency_overrides.clear()

    failures = [(code, body) for code, body in results if code != 200]
    assert failures == [], failures[:3]
    assert Mesh(namespace=namespace, agent_id="checker").count() >= 9


def test_mcp_tool_calls_under_concurrency(isolated_mesh_env, monkeypatch):
    namespace = _ns("c001-mcp")
    # Never contact a real daemon or the network.
    monkeypatch.setattr(mcp_mod, "_try_proxy_via_daemon", lambda *a, **k: None)

    async def drive():
        async def one(i: int):
            if i % 4 == 0:
                return await mcp_mod.call_tool(
                    "mesh_learn",
                    {"content": f"MCP concurrent fact {i}", "namespace": namespace},
                )
            if i % 4 == 1:
                return await mcp_mod.call_tool(
                    "mesh_recall", {"query": "MCP concurrent", "n": 3, "namespace": namespace}
                )
            if i % 4 == 2:
                return await mcp_mod.call_tool(
                    "mesh_task_list", {"namespace": namespace, "limit": 10}
                )
            return await mcp_mod.call_tool(
                "mesh_project_brief", {"namespace": namespace, "max_tokens": 200}
            )

        return await asyncio.gather(*(one(i) for i in range(16)))

    results = asyncio.run(drive())
    bad: list[Any] = []
    for res in results:
        if res.isError:
            bad.append(res.content[0].text if res.content else "unknown MCP error")
            continue
        json.loads(res.content[0].text)
    assert bad == [], bad[:3]


def test_multi_process_writers_share_one_namespace(isolated_mesh_env):
    namespace = _ns("c001-mp")
    repo_root = Path(__file__).resolve().parents[1]
    worker = Path(__file__).with_name("c001_mp_worker.py")
    procs = 4
    items = 10

    children = [
        subprocess.Popen(
            [
                sys.executable,
                str(worker),
                str(repo_root),
                str(isolated_mesh_env),
                namespace,
                f"w{w}",
                str(items),
            ],
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for w in range(procs)
    ]
    outputs = []
    for proc in children:
        out, err = proc.communicate(timeout=240)
        assert proc.returncode == 0, err[-2000:]
        outputs.append(json.loads(out.strip().splitlines()[-1]))

    assert [o["errors"] for o in outputs] == [[]] * procs
    assert sum(o["created"] for o in outputs) == procs * items

    tasks = Mesh(namespace=namespace, agent_id="checker").task_list(
        include_private=True, limit=500
    )
    assert len(tasks) == procs * items
    assert sum(1 for t in tasks if t["status"] == "done") == procs * items


def test_pool_eviction_race_across_namespaces(isolated_mesh_env, monkeypatch):
    """Force constant LRU eviction while several threads use the same namespaces."""
    monkeypatch.setenv("MESH_MAX_POOL_SIZE", "2")
    monkeypatch.setenv("MESH_MAX_VECTOR_CACHE_SIZE", "2")
    namespaces = [_ns(f"c001-evict{i}") for i in range(4)]
    for ns in namespaces:
        Mesh(namespace=ns, agent_id="seed").learn(f"Seed fact for {ns}")

    errors: list[str] = []
    lock = threading.Lock()
    loops = 6

    def worker(tid: int) -> None:
        try:
            for loop in range(loops):
                ns = namespaces[(tid + loop) % len(namespaces)]
                mesh = Mesh(namespace=ns, agent_id=f"evict-{tid}")
                mesh.project_brief(max_tokens=200)
                mesh.task_list(limit=10)
                mesh.recall("seed fact", n=2)
                if loop % 2 == 0:
                    item = mesh.task_create(title=f"evict {tid}-{loop}", kind="task")
                    mesh.task_complete(item_id=item["id"], summary="ok")
        except Exception:
            with lock:
                errors.append(traceback.format_exc())

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(worker, range(4)))

    assert errors == [], f"{len(errors)} eviction failure(s): {errors[0]}"
    for ns in namespaces:
        assert Mesh(namespace=ns, agent_id="checker").count() >= 1

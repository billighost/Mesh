import os
import sys
import time
import json
import shutil
import tempfile
import threading
import subprocess
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Ensure PROJECT_DIR is root
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from event_logger import log_event

def run_scenario(name, num_writers, items_per_writer, num_readers, reader_loops, prefix="c001"):
    temp_dir = tempfile.mkdtemp(prefix=f"{prefix}_{name}_")
    mesh_home = Path(temp_dir) / ".mesh"
    mesh_home.mkdir(parents=True, exist_ok=True)

    old_env = {k: os.environ.get(k) for k in ["MESH_HOME", "MESH_NO_AUTO_DAEMON", "MESH_NO_MODEL_DOWNLOAD", "MESH_NO_UPDATE_CHECK", "MESH_AUTO_APPROVE"]}
    os.environ["MESH_HOME"] = str(mesh_home)
    os.environ["MESH_NO_AUTO_DAEMON"] = "1"
    os.environ["MESH_NO_MODEL_DOWNLOAD"] = "1"
    os.environ["MESH_NO_UPDATE_CHECK"] = "1"
    os.environ["MESH_AUTO_APPROVE"] = "0"

    from mesh import Mesh
    from mesh.store import MeshStore
    import mesh.store as store_mod

    # Reset cache to be clean
    with store_mod._CACHE_LOCK:
        for conn in list(store_mod._CONNECTION_POOL.values()):
            try:
                conn.close_pooled()
            except Exception:
                pass
        store_mod._CONNECTION_POOL.clear()
        store_mod._DB_PATH_NAMESPACES.clear()
        store_mod._VECTOR_CACHE.clear()

    ns = f"{prefix}-{name}-{time.time_ns()}"
    total_expected = num_writers * items_per_writer
    errors = []
    lock_timeouts = 0
    created_items = []
    completed_items = []
    write_latencies = []
    read_latencies = []
    lock = threading.Lock()

    barrier = threading.Barrier(num_writers + num_readers)

    def writer_worker(w_id):
        nonlocal lock_timeouts
        m = Mesh(namespace=ns, agent_id=f"writer_{w_id}")
        barrier.wait()
        for i in range(items_per_writer):
            t_start = time.perf_counter()
            try:
                # 1. create task
                item = m.task_create(title=f"Parallel task {i} from writer_{w_id}", kind="task")
                with lock:
                    created_items.append(item["id"])

                # 2. update task
                m.task_update(item_id=item["id"], status="in_progress", note=f"Progress on step {i}")

                # 3. learn fact
                m.learn(content=f"Writer {w_id} fact {i}: concurrency checkpoint {time.time()}", memory_type="fact", tags=["concurrency", f"w{w_id}"])

                # 4. complete task
                m.task_complete(item_id=item["id"], summary=f"Completed {i} by {w_id}", evidence=[f"proof_{w_id}_{i}.log"])
                with lock:
                    completed_items.append(item["id"])
                write_latencies.append(time.perf_counter() - t_start)
            except Exception as e:
                err_str = f"Writer {w_id} loop {i}: {type(e).__name__}: {str(e)}"
                if "locked" in err_str.lower() or "busy" in err_str.lower():
                    lock_timeouts += 1
                with lock:
                    errors.append(err_str)

    def reader_worker(r_id):
        m = Mesh(namespace=ns, agent_id=f"reader_{r_id}")
        barrier.wait()
        for i in range(reader_loops):
            t_start = time.perf_counter()
            try:
                m.recall("concurrency checkpoint", n=5)
                m.project_brief(max_tokens=300)
                m.task_list(limit=20)
                with lock:
                    read_latencies.append(time.perf_counter() - t_start)
                time.sleep(0.005)
            except Exception as e:
                with lock:
                    errors.append(f"Reader {r_id} loop {i}: {type(e).__name__}: {str(e)}")

    t0 = time.perf_counter()
    threads = []
    for w in range(num_writers):
        threads.append(threading.Thread(target=writer_worker, args=(w,)))
    for r in range(num_readers):
        threads.append(threading.Thread(target=reader_worker, args=(r,)))

    for t in threads:
        t.start()
    for t in threads:
        t.join()
    dur_total = time.perf_counter() - t0

    # Final consistency check
    checker = Mesh(namespace=ns, agent_id="checker")
    all_stored_tasks = checker.task_list(limit=1000)
    all_stored_ids = {t["id"] for t in all_stored_tasks}
    lost_writes = total_expected - len(all_stored_ids)
    all_completed = sum(1 for t in all_stored_tasks if t.get("status") == "done")
    titles = [t["title"] for t in all_stored_tasks]
    duplicate_titles = len(titles) - len(set(titles))

    # Calculate average latencies in ms
    avg_write_ms = round((sum(write_latencies) / len(write_latencies) * 1000), 2) if write_latencies else 0
    avg_read_ms = round((sum(read_latencies) / len(read_latencies) * 1000), 2) if read_latencies else 0

    result = {
        "scenario": name,
        "duration_seconds": round(dur_total, 3),
        "parallel_writers": num_writers,
        "items_per_writer": items_per_writer,
        "parallel_readers": num_readers,
        "reader_loops": reader_loops,
        "total_expected_tasks": total_expected,
        "total_created_reported": len(created_items),
        "total_stored_tasks": len(all_stored_tasks),
        "total_completed_tasks": all_completed,
        "lost_writes": lost_writes,
        "duplicate_titles": duplicate_titles,
        "lock_timeouts": lock_timeouts,
        "total_errors": len(errors),
        "error_samples": errors[:5],
        "avg_write_duration_ms": avg_write_ms,
        "avg_read_duration_ms": avg_read_ms,
        "verdict": "PASS" if len(errors) == 0 and lost_writes == 0 and duplicate_titles == 0 and all_completed == total_expected else "FAIL"
    }

    log_event("phase1", "test_runner", f"c001_{name}", "run_scenario", dur_total * 1000, f"Verdict: {result['verdict']}", result)

    # Cleanup temp dir
    with store_mod._CACHE_LOCK:
        for conn in list(store_mod._CONNECTION_POOL.values()):
            try:
                conn.close_pooled()
            except Exception:
                pass
        store_mod._CONNECTION_POOL.clear()
        store_mod._DB_PATH_NAMESPACES.clear()
        store_mod._VECTOR_CACHE.clear()

    shutil.rmtree(temp_dir, ignore_errors=True)

    # Restore env
    for k, v in old_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    return result

def run_multiprocess_test():
    temp_dir = tempfile.mkdtemp(prefix="c001_mp_")
    mesh_home = Path(temp_dir) / ".mesh"
    mesh_home.mkdir(parents=True, exist_ok=True)
    ns = f"c001-mp-{time.time_ns()}"

    worker_script = PROJECT_DIR / "vendor" / "mesh_context_layer-0.8.1b2" / "tests" / "c001_mp_worker.py"
    env = os.environ.copy()
    env["MESH_HOME"] = str(mesh_home)
    env["MESH_NO_AUTO_DAEMON"] = "1"
    env["MESH_NO_MODEL_DOWNLOAD"] = "1"
    env["MESH_NO_UPDATE_CHECK"] = "1"
    env["MESH_AUTO_APPROVE"] = "0"

    t0 = time.perf_counter()
    procs = []
    for w in range(4):
        p = subprocess.Popen(
            [sys.executable, str(worker_script), str(PROJECT_DIR), str(mesh_home), ns, f"proc-{w}", "10"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env
        )
        procs.append(p)

    errors = []
    for i, p in enumerate(procs):
        out, err = p.communicate(timeout=60)
        if p.returncode != 0:
            errors.append(f"Process {i} failed (rc={p.returncode}): {err.strip()}")

    dur = time.perf_counter() - t0

    old_mesh_home = os.environ.get("MESH_HOME")
    os.environ["MESH_HOME"] = str(mesh_home)
    from mesh import Mesh
    checker = Mesh(namespace=ns, agent_id="mp-checker")
    tasks = checker.task_list(limit=200)
    completed = sum(1 for t in tasks if t.get("status") == "done")
    if old_mesh_home: os.environ["MESH_HOME"] = old_mesh_home

    res = {
        "scenario": "multi_process_4x10",
        "duration_seconds": round(dur, 3),
        "processes": 4,
        "items_per_process": 10,
        "total_expected": 40,
        "total_stored": len(tasks),
        "total_completed": completed,
        "errors": errors,
        "verdict": "PASS" if len(errors) == 0 and len(tasks) == 40 and completed == 40 else "FAIL"
    }
    log_event("phase1", "test_runner", "c001_multi_process", "run_multiprocess", dur * 1000, f"Verdict: {res['verdict']}", res)
    shutil.rmtree(temp_dir, ignore_errors=True)
    return res

def run_mixed_thread_and_process():
    temp_dir = tempfile.mkdtemp(prefix="c001_mixed_")
    mesh_home = Path(temp_dir) / ".mesh"
    mesh_home.mkdir(parents=True, exist_ok=True)
    ns = f"c001-mixed-{time.time_ns()}"

    worker_script = PROJECT_DIR / "vendor" / "mesh_context_layer-0.8.1b2" / "tests" / "c001_mp_worker.py"
    env = os.environ.copy()
    env["MESH_HOME"] = str(mesh_home)
    env["MESH_NO_AUTO_DAEMON"] = "1"
    env["MESH_NO_MODEL_DOWNLOAD"] = "1"
    env["MESH_NO_UPDATE_CHECK"] = "1"
    env["MESH_AUTO_APPROVE"] = "0"

    old_env = {k: os.environ.get(k) for k in ["MESH_HOME", "MESH_NO_AUTO_DAEMON", "MESH_NO_MODEL_DOWNLOAD", "MESH_NO_UPDATE_CHECK", "MESH_AUTO_APPROVE"]}
    os.environ["MESH_HOME"] = str(mesh_home)
    os.environ["MESH_NO_AUTO_DAEMON"] = "1"
    os.environ["MESH_NO_MODEL_DOWNLOAD"] = "1"
    os.environ["MESH_NO_UPDATE_CHECK"] = "1"
    os.environ["MESH_AUTO_APPROVE"] = "0"

    from mesh import Mesh

    t0 = time.perf_counter()
    # 2 child processes doing 15 items each
    procs = [
        subprocess.Popen([sys.executable, str(worker_script), str(PROJECT_DIR), str(mesh_home), ns, f"mp-{w}", "15"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        for w in range(2)
    ]

    # 4 in-process threads doing 15 items each
    thread_errors = []
    def t_worker(w):
        m = Mesh(namespace=ns, agent_id=f"thread-{w}")
        for i in range(15):
            try:
                t = m.task_create(title=f"Mixed thread task {i} from {w}", kind="task")
                m.task_update(item_id=t["id"], status="in_progress")
                m.learn(f"Thread {w} step {i} mixed concurrency checkpoint")
                m.task_complete(item_id=t["id"], summary="done", evidence=["m.log"])
            except Exception as e:
                thread_errors.append(f"Thread {w} err: {type(e).__name__}: {e}")

    threads = [threading.Thread(target=t_worker, args=(w,)) for w in range(4)]
    for t in threads: t.start()

    # Wait for both threads and processes
    for t in threads: t.join()
    proc_errors = []
    for i, p in enumerate(procs):
        out, err = p.communicate(timeout=60)
        if p.returncode != 0:
            proc_errors.append(f"Proc {i} failed: {err.strip()}")

    dur = time.perf_counter() - t0

    checker = Mesh(namespace=ns, agent_id="mixed-checker")
    tasks = checker.task_list(limit=200)
    completed = sum(1 for t in tasks if t.get("status") == "done")
    total_expected = 2 * 15 + 4 * 15 # 30 + 60 = 90

    res = {
        "scenario": "mixed_2proc_4thread_90items",
        "duration_seconds": round(dur, 3),
        "processes": 2,
        "threads": 4,
        "total_expected": total_expected,
        "total_stored": len(tasks),
        "total_completed": completed,
        "thread_errors": thread_errors,
        "proc_errors": proc_errors,
        "verdict": "PASS" if len(thread_errors) == 0 and len(proc_errors) == 0 and len(tasks) == total_expected and completed == total_expected else "FAIL"
    }

    log_event("phase1", "test_runner", "c001_mixed_concurrency", "run_mixed", dur * 1000, f"Verdict: {res['verdict']}", res)

    for k, v in old_env.items():
        if v is None: os.environ.pop(k, None)
        else: os.environ[k] = v
    shutil.rmtree(temp_dir, ignore_errors=True)
    return res

def run_mcp_concurrency():
    temp_dir = tempfile.mkdtemp(prefix="c001_mcp_")
    mesh_home = Path(temp_dir) / ".mesh"
    mesh_home.mkdir(parents=True, exist_ok=True)
    ns = f"c001-mcp-{time.time_ns()}"

    old_env = {k: os.environ.get(k) for k in ["MESH_HOME", "MESH_NO_AUTO_DAEMON", "MESH_NO_MODEL_DOWNLOAD", "MESH_NO_UPDATE_CHECK"]}
    os.environ["MESH_HOME"] = str(mesh_home)
    os.environ["MESH_NO_AUTO_DAEMON"] = "1"
    os.environ["MESH_NO_MODEL_DOWNLOAD"] = "1"
    os.environ["MESH_NO_UPDATE_CHECK"] = "1"

    import mesh.mcp_server as mcp_mod

    async def mcp_worker(client_id, count):
        errors = []
        for i in range(count):
            try:
                if i % 3 == 0:
                    res = await mcp_mod.call_tool("mesh_learn", {"content": f"mcp fact {client_id}_{i}", "namespace": ns})
                elif i % 3 == 1:
                    res = await mcp_mod.call_tool("mesh_recall", {"query": f"mcp fact {client_id}", "namespace": ns})
                else:
                    res = await mcp_mod.call_tool("mesh_project_brief", {"namespace": ns})
                if getattr(res, "isError", False):
                    errors.append(f"MCP client {client_id} op {i} returned isError=True")
            except Exception as e:
                errors.append(f"MCP client {client_id} op {i} exception: {type(e).__name__}: {e}")
        return errors

    t0 = time.perf_counter()
    async def run_all():
        tasks = [mcp_worker(c, 20) for c in range(8)] # 8 concurrent MCP clients x 20 ops = 160 ops
        return await asyncio.gather(*tasks)

    client_errors = asyncio.run(run_all())
    flat_errors = [e for sub in client_errors for e in sub]
    dur = time.perf_counter() - t0

    res = {
        "scenario": "mcp_concurrent_8x20",
        "duration_seconds": round(dur, 3),
        "clients": 8,
        "ops_per_client": 20,
        "total_ops": 160,
        "total_errors": len(flat_errors),
        "errors": flat_errors[:5],
        "verdict": "PASS" if len(flat_errors) == 0 else "FAIL"
    }

    log_event("phase1", "test_runner", "c001_mcp_concurrency", "run_mcp", dur * 1000, f"Verdict: {res['verdict']}", res)

    for k, v in old_env.items():
        if v is None: os.environ.pop(k, None)
        else: os.environ[k] = v
    shutil.rmtree(temp_dir, ignore_errors=True)
    return res

if __name__ == "__main__":
    print("=== C-001 CONCURRENCY VERIFICATION SUITE ===")
    
    # 1. Exact Run C reproduction: 4 writers (25 tasks each = 100 items), 4 readers (30 loops each = 120 calls)
    print("\n--- 1. Exact Run C Scenario (4 writers x 25 items, 4 readers x 30 loops) ---")
    s1 = run_scenario("exact_run_c", num_writers=4, items_per_writer=25, num_readers=4, reader_loops=30)
    print(f"Result: {s1['verdict']} | Duration: {s1['duration_seconds']}s | Errors: {s1['total_errors']} | Stored: {s1['total_stored_tasks']}/{s1['total_expected_tasks']}")

    # 2. Heavier variant: 8 writers (25 tasks each = 200 items), 8 readers (40 loops each)
    print("\n--- 2. Heavier Scenario (8 writers x 25 items, 8 readers x 40 loops) ---")
    s2 = run_scenario("heavier_8x25", num_writers=8, items_per_writer=25, num_readers=8, reader_loops=40)
    print(f"Result: {s2['verdict']} | Duration: {s2['duration_seconds']}s | Errors: {s2['total_errors']} | Stored: {s2['total_stored_tasks']}/{s2['total_expected_tasks']}")

    # 3. Multi-process concurrency: 4 separate processes x 10 items
    print("\n--- 3. Multi-Process Concurrency (4 processes x 10 items) ---")
    s3 = run_multiprocess_test()
    print(f"Result: {s3['verdict']} | Duration: {s3['duration_seconds']}s | Errors: {len(s3['errors'])} | Stored: {s3['total_stored']}/{s3['total_expected']}")

    # 4. Mixed thread + process concurrency (2 processes + 4 threads = 90 items)
    print("\n--- 4. Mixed Thread + Process Concurrency (2 processes + 4 threads) ---")
    s4 = run_mixed_thread_and_process()
    print(f"Result: {s4['verdict']} | Duration: {s4['duration_seconds']}s | Errors: {len(s4['thread_errors']) + len(s4['proc_errors'])} | Stored: {s4['total_stored']}/{s4['total_expected']}")

    # 5. MCP concurrency (8 clients x 20 ops)
    print("\n--- 5. Concurrent MCP Client Calls (8 clients x 20 ops = 160 ops) ---")
    s5 = run_mcp_concurrency()
    print(f"Result: {s5['verdict']} | Duration: {s5['duration_seconds']}s | Errors: {s5['total_errors']}")

    combined = {
        "run_label": "T",
        "exact_run_c": s1,
        "heavier_variant": s2,
        "multi_process": s3,
        "mixed_thread_process": s4,
        "mcp_concurrency": s5,
        "overall_verdict": "PASS" if all(x["verdict"] == "PASS" for x in [s1, s2, s3, s4, s5]) else "FAIL"
    }

    out_file = PROJECT_DIR / "report" / "data" / "concurrency_results.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(combined, indent=2), encoding="utf-8")
    print(f"\nAll results saved to {out_file}. Overall verdict: {combined['overall_verdict']}")

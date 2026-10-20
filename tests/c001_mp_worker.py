"""Subprocess worker for tests/test_thread_safety_c001.py (C-001 multi-process leg).

Usage: python c001_mp_worker.py <repo_root> <mesh_home> <namespace> <worker_id> <items>
Prints one JSON line. No network, no daemon, temp MESH_HOME supplied by parent.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback

repo_root, mesh_home, namespace, worker_id, items_raw = sys.argv[1:6]
sys.path.insert(0, repo_root)
os.environ["MESH_HOME"] = mesh_home
os.environ["MESH_NO_AUTO_DAEMON"] = "1"
os.environ["MESH_AUTO_APPROVE"] = "0"
os.environ["MESH_NO_MODEL_DOWNLOAD"] = "1"
os.environ["MESH_NO_UPDATE_CHECK"] = "1"

from mesh import Mesh  # noqa: E402

items = int(items_raw)
result = {"worker": worker_id, "errors": [], "created": 0, "completed": 0, "duration": 0.0}
started = time.perf_counter()
try:
    mesh = Mesh(namespace=namespace, agent_id=f"mp-{worker_id}")
    for i in range(items):
        item = mesh.task_create(title=f"MP task {i} from {worker_id}", kind="task")
        result["created"] += 1
        mesh.task_update(item_id=item["id"], status="in_progress")
        mesh.learn(
            content=f"MP fact {worker_id}-{i} about widget concurrency",
            tags=["c001"],
            deduplicate=False,
        )
        mesh.task_complete(item_id=item["id"], summary=f"done {worker_id}-{i}")
        result["completed"] += 1
except Exception as exc:  # noqa: BLE001 - report every failure to the parent test
    result["errors"].append(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1200:]}")
result["duration"] = round(time.perf_counter() - started, 3)
print(json.dumps(result))

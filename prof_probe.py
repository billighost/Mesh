import os, tempfile, cProfile, pstats, io
os.environ.update(MESH_HOME=tempfile.mkdtemp(), MESH_NO_AUTO_DAEMON="1",
                  MESH_NO_DAEMON_PROXY="1", MESH_NO_MODEL_DOWNLOAD="1",
                  MESH_FORCE_HASH_EMBED="1")
from mesh import Mesh
from mesh.audit import log_audit_event
m = Mesh(namespace="p", agent_id="p", create=True, trusted_local=True)
g = m.create_work_item(title="g", kind="goal")

def run(label, fn, n=40):
    fn()
    pr = cProfile.Profile(); pr.enable()
    for _ in range(n): fn()
    pr.disable()
    s = io.StringIO()
    pstats.Stats(pr, stream=s).sort_stats("cumulative").print_stats(14)
    print("=====", label); print(s.getvalue())

run("create_work_item", lambda: m.create_work_item(title="t", parent_id=g["id"]))
run("log_audit_event", lambda: log_audit_event("sdk", "x", "p", 1.0))
run("learn", lambda: m.learn(f"fact {os.urandom(4).hex()} runs on port 5432"))
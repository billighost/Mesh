#!/usr/bin/env python3
r"""
profile_windows.py - measure where Mesh spends its time on THIS machine and write one report file.

    python profile_windows.py                  # about 3-6 minutes, writes mesh_profile_report.txt
    python profile_windows.py --quick          # fewer iterations (about 1-2 minutes)
    python profile_windows.py --home D:\meshprof   # use another drive/folder (e.g. one excluded from Defender)

It uses a THROWAWAY data folder (never your real ~/.mesh), starts no daemon you have to stop, and
does not change any file in the repo. Send back mesh_profile_report.txt.

Sections of the report
  A  environment (OS, Python, SQLite, which embedding engine is active)
  B  floors: how long a do-nothing process, a stat(), a file create/delete and a SQLite commit take here
  C  timing table twice: background ONNX worker ON (default) and OFF (MESH_BG_ONNX=0)
  D  cProfile (by self time and by cumulative time) + filesystem-call counts for each slow operation
  E  HTTP: in-process ASGI with ONE persistent client, and a live daemon in a separate process
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import cProfile
import http.client
import io
import json
import os
import platform
import pstats
import random
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

WORDS = ("redis postgres port timeout cache deploy auth token queue worker retry index schema kafka nginx "
         "docker kube helm vault consul staging service").split()


def pctl(ts: list[float], p: float) -> float:
    s = sorted(ts)
    return s[min(len(s) - 1, int(round((len(s) - 1) * p)))]


def measure(fn, n: int, warm: int = 4) -> dict:
    for _ in range(warm):
        fn()
    ts = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t) * 1000.0)
    return {"p50": statistics.median(ts), "p95": pctl(ts, 0.95), "min": min(ts), "n": n}


# --------------------------------------------------------------------------------------------------
# throwaway environment
# --------------------------------------------------------------------------------------------------
def prepare_env(home_root: str | None, bg_off: bool) -> str:
    base = tempfile.mkdtemp(prefix="meshprof_", dir=home_root)
    os.environ.update(
        MESH_HOME=os.path.join(base, ".mesh"),
        MESH_NO_AUTO_DAEMON="1",
        MESH_NO_DAEMON_PROXY="1",
        MESH_NO_UPDATE_CHECK="1",
        MESH_NO_MODEL_DOWNLOAD="1",
        MESH_QUIET="1",
        MESH_AUTO_APPROVE="0",
    )
    if bg_off:
        os.environ["MESH_BG_ONNX"] = "0"
    else:
        os.environ.pop("MESH_BG_ONNX", None)
    return base


def seed(namespace: str, count: int, learn_fn_cache: dict):
    from mesh import Mesh

    m = Mesh(namespace=namespace, agent_id="prof", create=True, trusted_local=True)
    rnd = random.Random(namespace)
    for i in range(count):
        m.learn(f"{rnd.choice(WORDS)} {rnd.choice(WORDS)} service {i} uses {rnd.choice(WORDS)} on port {5000 + i} "
                f"with {rnd.choice(WORDS)} retries {i % 7}")
    return m


def quiesce():
    """Let the stage-2 worker finish so seeding noise does not leak into the measurements."""
    try:
        from mesh import memory as mem

        mem.drain_reembed_worker(120.0)
    except Exception:
        pass


def build_ops(n_pool: int) -> dict:
    """name -> callable. Seeds a ~100-memory namespace and a 1,000-memory namespace first."""
    from mesh import Mesh
    from mesh import cli as cli_mod
    from mesh.digest import generate_digest
    from mesh.io import export_namespace

    cache: dict = {}
    m100 = seed("prof_100", 100, cache)
    m1k = seed("prof_1k", 1000, cache)
    quiesce()
    pool_u = [m100.learn(f"pool update {uuid.uuid4().hex}")["memory_id"] for _ in range(n_pool)]
    pool_f = [m100.learn(f"pool forget {uuid.uuid4().hex}")["memory_id"] for _ in range(n_pool)]
    quiesce()
    export_path = os.path.join(os.environ["MESH_HOME"], "prof_export.json")
    rnd = random.Random(5)

    def q():
        return f"{rnd.choice(WORDS)} {rnd.choice(WORDS)} port {rnd.random()}"

    def cli_inproc(argv):
        def run():
            old = sys.argv
            try:
                sys.argv = argv
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    try:
                        cli_mod.main()
                    except SystemExit:
                        pass
            finally:
                sys.argv = old

        return run

    m100.create_work_item(title="seed task")
    ops = {
        "learn (new fact)": lambda: m100.learn(f"Prof fact {uuid.uuid4().hex} runs on port 5432 for service alpha"),
        "update (content change)": lambda: m100.update(pool_u.pop(), content=f"changed {uuid.uuid4().hex}"),
        "forget": lambda: m100.forget(pool_f.pop()),
        "recall ~100 mems": lambda: m100.recall(q()),
        "recall 1,000 mems": lambda: m1k.recall(q()),
        "recall_with_context": lambda: m100.recall_with_context("staging service port", n=5),
        "create_work_item": lambda: m100.create_work_item(title=f"t {uuid.uuid4().hex}"),
        "generate_digest": lambda: generate_digest(namespace="prof_100", hours=24),
        "export_namespace": lambda: export_namespace("prof_100", output_path=export_path, include_embeddings=False),
        "cli in-proc: task list": cli_inproc(["mesh", "task", "list", "--namespace", "prof_100", "--json"]),
        "cli in-proc: add": cli_inproc(["mesh", "add", "profiling note about redis", "--namespace", "prof_100"]),
        "cli in-proc: ask": cli_inproc(["mesh", "ask", "redis port", "--namespace", "prof_100", "--json"]),
        "cli in-proc: status": cli_inproc(["mesh", "status", "--namespace", "prof_100", "--json"]),
    }
    return ops


# --------------------------------------------------------------------------------------------------
# filesystem-call counting (what Windows pays for)
# --------------------------------------------------------------------------------------------------
class FsCounter:
    NAMES = ("stat", "lstat", "open", "mkdir", "listdir", "scandir", "replace", "chmod", "readlink", "access")

    def __init__(self):
        self.on = False
        self.busy = False
        self.total = collections.Counter()
        self.sites = collections.Counter()
        self._orig: list = []

    def _wrap(self, mod, name, label):
        orig = getattr(mod, name, None)
        if orig is None:
            return
        counter = self

        def f(*a, **k):
            if counter.on and not counter.busy:
                counter.busy = True
                try:
                    counter.total[label] += 1
                    fr = sys._getframe(1)
                    while fr is not None and "mesh" not in os.path.basename(os.path.dirname(fr.f_code.co_filename)).lower() \
                            and "mesh" not in os.path.basename(fr.f_code.co_filename).lower():
                        fr = fr.f_back
                    if fr is not None:
                        counter.sites[(label, os.path.basename(fr.f_code.co_filename), fr.f_lineno, fr.f_code.co_name)] += 1
                finally:
                    counter.busy = False
            return orig(*a, **k)

        setattr(mod, name, f)
        self._orig.append((mod, name, orig))

    def install(self):
        for n in self.NAMES:
            self._wrap(os, n, n)
        for n in ("exists", "isfile", "isdir", "islink", "lexists"):
            self._wrap(os.path, n, "path." + n)

    def uninstall(self):
        for mod, name, orig in reversed(self._orig):
            setattr(mod, name, orig)
        self._orig.clear()


# --------------------------------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------------------------------
def section_a(out):
    out("A. ENVIRONMENT")
    out(f"  platform      : {platform.platform()}")
    out(f"  python        : {sys.version.split()[0]} ({platform.architecture()[0]}) at {sys.executable}")
    import sqlite3

    out(f"  sqlite        : {sqlite3.sqlite_version}")
    out(f"  cpu count     : {os.cpu_count()}")
    try:
        from mesh import memory as mem
        from mesh import c_bridge

        out(f"  engine (cold) : {engine_line()}")
        out(f"  native core   : {c_bridge.native_info() if hasattr(c_bridge, 'native_info') else 'see mesh.c_bridge'}")
    except Exception as e:  # pragma: no cover
        out(f"  engine        : (unavailable: {e})")
    out(f"  MESH_HOME     : {os.environ.get('MESH_HOME')}")
    out(f"  temp drive    : {Path(os.environ['MESH_HOME']).drive or '/'}")
    out(f"  MESH_* env    : { {k: v for k, v in os.environ.items() if k.startswith('MESH_') and k != 'MESH_HOME'} }")
    out("")


def section_b(out, base: str):
    out("B. FLOORS (what this machine charges for basic operations)")
    # do-nothing processes
    exe = None
    gcc = shutil.which("gcc")
    if gcc:
        src = Path(base) / "noop.c"
        src.write_text("int main(void){return 0;}\n")
        exe_path = Path(base) / ("noop.exe" if os.name == "nt" else "noop")
        r = subprocess.run([gcc, "-O2", str(src), "-o", str(exe_path)], capture_output=True)
        if r.returncode == 0:
            exe = str(exe_path)
    if exe:
        r = measure(lambda: subprocess.run([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL), 30, 3)
        out(f"  spawn a do-nothing native exe : p50 {r['p50']:7.2f} ms   p95 {r['p95']:7.2f}   (compare: mesh_fast --version)")
    else:
        out("  spawn a do-nothing native exe : skipped (no gcc on PATH)")
    r = measure(lambda: subprocess.run([sys.executable, "-c", "pass"], stdout=subprocess.DEVNULL), 15, 2)
    out(f"  spawn `python -c pass`        : p50 {r['p50']:7.2f} ms   p95 {r['p95']:7.2f}")
    r = measure(lambda: subprocess.run([sys.executable, "-S", "-c", "pass"], stdout=subprocess.DEVNULL), 15, 2)
    out(f"  spawn `python -S -c pass`     : p50 {r['p50']:7.2f} ms   p95 {r['p95']:7.2f}")
    # syscalls
    target = os.path.join(base, "floor.txt")
    Path(target).write_text("x")
    r = measure(lambda: [os.stat(target) for _ in range(200)], 20, 2)
    out(f"  os.stat(existing file)        : {r['p50'] * 1000 / 200:7.1f} us per call")
    r = measure(lambda: [os.lstat(target) for _ in range(200)], 20, 2)
    out(f"  os.lstat(existing file)       : {r['p50'] * 1000 / 200:7.1f} us per call")
    missing = os.path.join(base, "missing.txt")

    def stat_missing():
        for _ in range(200):
            try:
                os.stat(missing)
            except OSError:
                pass

    r = measure(stat_missing, 20, 2)
    out(f"  os.stat(missing file)         : {r['p50'] * 1000 / 200:7.1f} us per call")

    def create_delete():
        p = os.path.join(base, f"f_{uuid.uuid4().hex}.tmp")
        with open(p, "wb") as fh:
            fh.write(b"hello")
        os.remove(p)

    r = measure(create_delete, 60, 5)
    out(f"  create+write+close+delete file: p50 {r['p50']:7.3f} ms   p95 {r['p95']:7.3f}   (antivirus scans show up here)")
    import sqlite3

    con = sqlite3.connect(os.path.join(base, "floor.db"), isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("CREATE TABLE t(x)")

    def commit_one():
        con.execute("BEGIN")
        con.execute("INSERT INTO t VALUES (1)")
        con.execute("COMMIT")

    r = measure(commit_one, 200, 10)
    out(f"  sqlite insert+commit (sync=OFF): p50 {r['p50']:7.3f} ms   p95 {r['p95']:7.3f}")
    con.close()
    out("")


def run_summary_child(home_root, bg_off: bool, quick: bool) -> dict:
    cmd = [sys.executable, str(Path(__file__).resolve()), "--summary-only"]
    if quick:
        cmd.append("--quick")
    if home_root:
        cmd += ["--home", home_root]
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("MESH_") and k not in ("MESH_FORCE_HASH_EMBED",):
            del env[k]
    if bg_off:
        env["MESH_BG_ONNX"] = "0"
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    for line in reversed(r.stdout.splitlines()):
        if line.startswith("{"):
            return json.loads(line)
    return {"_error": (r.stderr or r.stdout)[-1500:]}


def summary_table(label_a, a, label_b, b, out):
    names = [k for k in a if not k.startswith("_")]
    out(f"  {'operation':34s} | {label_a + ' p50':>14s} {label_a + ' p95':>14s} | {label_b + ' p50':>13s} {label_b + ' p95':>13s}")
    for k in names:
        ra, rb = a.get(k), (b or {}).get(k)
        fa = f"{ra['p50']:14.2f} {ra['p95']:14.2f}" if isinstance(ra, dict) else f"{'error':>29s}"
        fb = f"{rb['p50']:13.2f} {rb['p95']:13.2f}" if isinstance(rb, dict) else f"{'-':>27s}"
        out(f"  {k:34s} | {fa} | {fb}")


def engine_line() -> str:
    try:
        from mesh import memory as mem

        st = mem.get_engine_status()
        keep = {k: st.get(k) for k in ("backend", "model_tag", "model_loaded", "onnx_cached_on_disk", "embedding_degraded", "last_error")}
        return str(keep)
    except Exception as e:  # pragma: no cover
        return f"(unavailable: {e})"


def section_d(out, ops, n):
    out("D. PROFILES  (p50 shown first, then cProfile over the same call, then filesystem-call counts)")
    out("  embedding engine after seeding: " + engine_line())
    fs = FsCounter()
    fs.install()
    try:
        for name, fn in ops.items():
            out(f"--- {name} " + "-" * max(1, 70 - len(name)))
            try:
                for _ in range(3):
                    fn()
                ts = []
                for _ in range(n):
                    t = time.perf_counter()
                    fn()
                    ts.append((time.perf_counter() - t) * 1000.0)
                out(f"  p50 {statistics.median(ts):.2f} ms   p95 {pctl(ts, 0.95):.2f} ms   (n={n})")
                pr = cProfile.Profile()
                pr.enable()
                try:
                    for _ in range(n):
                        fn()
                finally:
                    pr.disable()
                for key, title in (("tottime", "by SELF time"), ("cumulative", "by CUMULATIVE time")):
                    s = io.StringIO()
                    pstats.Stats(pr, stream=s).sort_stats(key).print_stats(14)
                    lines = [ln for ln in s.getvalue().splitlines() if ln.strip()]
                    start = next((i for i, ln in enumerate(lines) if "ncalls" in ln), 0)
                    out(f"  [{title}] (totals are for {n} calls)")
                    for ln in lines[start:start + 15]:
                        out("    " + ln[:170])
                fs.total.clear()
                fs.sites.clear()
                fs.on = True
                k = 10
                for _ in range(k):
                    fn()
                fs.on = False
                per = {kk: round(v / k, 1) for kk, v in fs.total.items()}
                out(f"  filesystem calls per call: {per}  (total {round(sum(fs.total.values()) / k, 1)})")
                for site, v in fs.sites.most_common(6):
                    out(f"    {v / k:5.1f}x  {site[0]:12s} {site[1]}:{site[2]} in {site[3]}")
            except Exception as e:
                fs.on = False
                out(f"  ERROR: {type(e).__name__}: {e}")
            out("")
    finally:
        fs.uninstall()


def section_e(out, n):
    out("E. HTTP")
    try:
        from fastapi.testclient import TestClient

        from mesh import dashboard as dash
        from mesh import http_server as hs
        from mesh.daemon import get_or_create_auth_token

        tok = get_or_create_auth_token()
        H = {"Authorization": f"Bearer {tok}", "X-Mesh-Token": tok}
        hc = TestClient(hs.app, base_url="http://127.0.0.1:7701")
        hc.__enter__()
        dc = TestClient(dash.api, base_url="http://127.0.0.1:7433")
        dc.__enter__()
        body = {"query": "redis port", "namespace": "prof_100"}
        routes = {
            "ASGI GET /health": lambda: hc.get("/health"),
            "ASGI POST /recall": lambda: hc.post("/recall", json=body, headers=H),
            "ASGI POST /learn": lambda: hc.post("/learn", json={"content": f"http fact {uuid.uuid4().hex}", "namespace": "prof_100"}, headers=H),
            "ASGI GET /inspect": lambda: hc.get("/inspect?namespace=prof_100", headers=H),
            "ASGI dashboard GET /api/digest": lambda: dc.get("/api/digest?namespace=prof_100&hours=24", headers=H),
            "ASGI dashboard GET /api/stats": lambda: dc.get("/api/stats", headers=H),
        }
        out("  in-process ASGI, ONE persistent client (so this is server cost, not test-client thread start-up):")
        for k, fn in routes.items():
            try:
                r = measure(fn, n, 4)
                out(f"    {k:34s} p50 {r['p50']:7.2f} ms   p95 {r['p95']:7.2f}")
            except Exception as e:
                out(f"    {k:34s} ERROR {type(e).__name__}: {e}")
        pr = cProfile.Profile()
        pr.enable()
        try:
            for _ in range(n):
                routes["ASGI POST /recall"]()
        finally:
            pr.disable()
        s = io.StringIO()
        pstats.Stats(pr, stream=s).sort_stats("tottime").print_stats(16)
        lines = [ln for ln in s.getvalue().splitlines() if ln.strip()]
        start = next((i for i, ln in enumerate(lines) if "ncalls" in ln), 0)
        out("  cProfile of POST /recall (by self time):")
        for ln in lines[start:start + 17]:
            out("    " + ln[:170])
    except Exception as e:
        out(f"  in-process ASGI section failed: {type(e).__name__}: {e}")

    # live daemon in its own process
    out("")
    try:
        with socket.socket() as s_:
            s_.bind(("127.0.0.1", 0))
            port = s_.getsockname()[1]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.Popen([sys.executable, "-m", "mesh.http_server", "--port", str(port), "--no-warmup", "--yes"],
                                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 40
            while time.time() < deadline:
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
                    break
                except OSError:
                    time.sleep(0.2)
            from mesh.daemon import get_or_create_auth_token

            tok = get_or_create_auth_token()
            hdr = {"Authorization": f"Bearer {tok}", "X-Mesh-Token": tok, "Content-Type": "application/json"}
            payload = json.dumps({"query": "redis port", "namespace": "prof_100"})
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)

            def keepalive_recall():
                conn.request("POST", "/recall", body=payload, headers=hdr)
                conn.getresponse().read()

            def fresh_recall():
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                c.request("POST", "/recall", body=payload, headers=hdr)
                c.getresponse().read()
                c.close()

            def fresh_health():
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                c.request("GET", "/health")
                c.getresponse().read()
                c.close()

            out("  live daemon in a SEPARATE process, real TCP sockets:")
            for label, fn in (("GET /health (new connection)", fresh_health),
                              ("POST /recall (keep-alive)", keepalive_recall),
                              ("POST /recall (new connection)", fresh_recall)):
                r = measure(fn, n, 5)
                out(f"    {label:34s} p50 {r['p50']:7.2f} ms   p95 {r['p95']:7.2f}")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception as e:
        out(f"  live daemon section failed: {type(e).__name__}: {e}")
    out("")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--home", default=None, help="parent folder for the throwaway data (default: system temp)")
    ap.add_argument("--out", default="mesh_profile_report.txt")
    ap.add_argument("--summary-only", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    n = 15 if args.quick else 40

    if args.summary_only:
        base = prepare_env(args.home, bg_off=os.environ.get("MESH_BG_ONNX") == "0")
        try:
            ops = build_ops(2 * n + 30)
            res = {name: measure(fn, n, 3) for name, fn in ops.items()}
            quiesce()
            print(json.dumps(res))
        finally:
            shutil.rmtree(base, ignore_errors=True)
        return 0

    lines: list[str] = []

    def out(s: str = ""):
        print(s, flush=True)
        lines.append(s)

    out("MESH PROFILE REPORT  " + time.strftime("%Y-%m-%d %H:%M:%S"))
    out("=" * 100)
    base = prepare_env(args.home, bg_off=False)
    try:
        section_a(out)
        section_b(out, base)
        out("C. TIMING TABLE  (ms)  background ONNX worker ON (default) vs OFF (MESH_BG_ONNX=0)")
        out("   (each column is a fresh process with a fresh throwaway database)")
        a = run_summary_child(args.home, bg_off=False, quick=args.quick)
        b = run_summary_child(args.home, bg_off=True, quick=args.quick)
        if "_error" in a:
            out("  ON-run failed:\n" + a["_error"])
        else:
            summary_table("ON", a, "OFF", b if "_error" not in b else None, out)
        if "_error" in b:
            out("  OFF-run failed:\n" + b["_error"])
        out("")
        out("   building profile data (seeding 1,100 memories)...")
        ops = build_ops(2 * n + 30)
        section_d(out, ops, n)
        quiesce()
        section_e(out, n)
    finally:
        Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
        shutil.rmtree(base, ignore_errors=True)
    out(f"Report written to {Path(args.out).resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

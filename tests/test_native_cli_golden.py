"""
Golden + security tests for the native C client (c_cli/mesh_fast.c).

* Byte-identical stdout vs `python -m mesh.cli ask ... --json` against a REAL daemon process.
* The bearer token must never reach a socket before the identity proof verifies.
* Replies > 64 KB, chunked replies and tricky JSON are rendered exactly like json.dumps(indent=2).
* Hostile / ambiguous arguments fall back to Python and never reach the wire.
Skipped when no C compiler is available.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "c_cli" / "mesh_fast.c"
TOKEN = "golden-test-token-0123456789abcdef"

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("gcc") is None or not SRC.exists(),
    reason="needs gcc on a POSIX host (Windows build is compile-checked in build_and_verify.py)",
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def native_bin(tmp_path_factory) -> str:
    out = tmp_path_factory.mktemp("bin") / "mesh_fast"
    version = subprocess.run(
        [sys.executable, "-c", "from mesh.version import VERSION; print(VERSION)"],
        capture_output=True, text=True, cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT)),
    ).stdout.strip()
    r = subprocess.run(
        ["gcc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter",
         f'-DMESH_VERSION="{version}"', *os.environ.get("MESH_NATIVE_CFLAGS", "").split(), str(SRC), "-o", str(out)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    return str(out)


def _env(home: Path, port: int, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MESH_")}
    env.update(
        PYTHONPATH=str(ROOT), MESH_HOME=str(home), MESH_HTTP_PORT=str(port), MESH_NO_AUTO_DAEMON="1",
        MESH_NO_MODEL_DOWNLOAD="1", MESH_FORCE_HASH_EMBED="1", MESH_NAMESPACE="gold",
        MESH_PYTHON=sys.executable, LC_ALL="C.UTF-8", PYTHONUTF8="1",
    )
    env.update(extra)
    return env


@pytest.fixture(scope="module")
def daemon(tmp_path_factory):
    home = tmp_path_factory.mktemp("meshhome")
    os.chmod(home, 0o700)
    tok = home / "auth_token"
    tok.write_text(TOKEN, encoding="utf-8")
    os.chmod(tok, 0o600)
    port = _free_port()
    env = _env(home, port)

    # seed through the SDK (same MESH_HOME) before the daemon starts
    seed = (
        "from mesh import Mesh\n"
        "m = Mesh(namespace='gold', agent_id='seed', create=True, trusted_local=True)\n"
        "for t in ['Redis runs on port 6379 in production', 'Postgres replica lag alert threshold is 30 seconds',\n"
        "          'Café ☕ staging uses région eu-west — naïve “quotes” and emoji 🚀', 'Deploy via make release; rollback with make revert',\n"
        "          'Backslash C:\\\\path\\\\to and \"double quotes\" and tab\\there', 'Redis cache TTL is 300 seconds for sessions']:\n"
        "    m.learn(t)\n"
        "m.learn('decision: use Redis for sessions', memory_type='decision', tags=['infra','cache'])\n"
        "for i in range(24):\n"
        "    m.learn(('bulk-redis-entry %d ' % i) + ('lorem ipsum redis cache ' * 320))\n"
    )
    r = subprocess.run([sys.executable, "-c", seed], env=env, capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    (home / "daemon.pid").write_text(str(os.getpid()), encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "mesh.http_server", "--port", str(port), "--no-warmup", "--yes"],
        env=env, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("daemon did not start")
    yield home, port, env
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


import re

_VOLATILE = re.compile(rb'("(?:composite_score|access_count|accessed_at)": )[^\n]*')


def _norm(b: bytes) -> bytes:
    """recall() itself bumps access_count / accessed_at / the composite score, so two consecutive runs of
    the *Python* CLI differ in exactly these three lines; everything else must match byte for byte."""
    return re.sub(rb"\b\d+ ms\b", b"N ms", _VOLATILE.sub(rb"\1X", b))


def _py(argv, env):
    return subprocess.run([sys.executable, "-m", "mesh.cli", *argv], env=env, capture_output=True, cwd=ROOT)


def _native(binary, argv, env, trace=True):
    e = dict(env)
    if trace:
        e["MESH_NATIVE_TRACE"] = "1"
    return subprocess.run([binary, *argv], env=e, capture_output=True, cwd=ROOT)


CASES = [
    ["ask", "Redis", "--json"],
    ["ask", "redis cache ttl", "--json", "-n", "2"],
    ["search", "port", "--json", "--count", "3"],
    ["recall", "deploy rollback", "--json", "--n", "1"],
    ["find", "Postgres lag", "--json", "--no-other"],
    ["ask", "Redis", "--json", "--type", "decision"],
    ["ask", "Redis", "--json", "--tags", "infra, cache ,"],
    ["ask", "Redis", "--json", "--no-private", "--no-stale"],
    ["ask", "Redis", "--json", "--min-similarity", "0.05", "--min-confidence", "0.5"],
    ["ask", "Café ☕ région", "--json"],
    ["ask", 'say "hi" \\ back\tslash\nnewline', "--json"],
    ["ask", "xyzzy-nothing-matches-this-at-all", "--json"],
    ["ask", "Redis", "--json", "--namespace", "gold"],
    ["ask", "Redis", "--json", "-n", "40"],  # reply well over 64 KB
]


@pytest.mark.parametrize("argv", CASES, ids=lambda a: " ".join(a)[:60])
def test_native_output_is_byte_identical_to_python(native_bin, daemon, argv):
    home, port, env = daemon
    p = _py(argv, env)
    n = _native(native_bin, argv, env)
    assert n.returncode == p.returncode, (n.stderr, p.stderr)
    assert _norm(n.stdout) == _norm(p.stdout)
    assert b"fallback" not in n.stderr or "does-not-exist" in " ".join(argv)


def test_native_path_is_really_taken(native_bin, daemon):
    home, port, env = daemon
    n = _native(native_bin, ["ask", "Redis", "--json"], env)
    assert n.returncode == 0 and b"fallback" not in n.stderr, n.stderr
    big = _native(native_bin, ["ask", "Redis", "--json", "-n", "40"], env)
    assert b"fallback" not in big.stderr and len(big.stdout) > 70_000


def test_missing_namespace_falls_back_with_identical_output(native_bin, daemon):
    home, port, env = daemon
    argv = ["ask", "Redis", "--json", "--namespace", "does-not-exist"]
    p, n = _py(argv, env), _native(native_bin, argv, env)
    assert _norm(n.stdout) == _norm(p.stdout) and n.returncode == p.returncode
    assert b"fallback" in n.stderr


@pytest.mark.parametrize(
    "argv",
    [
        ["ask", "Redis"],  # human output -> Python
        ["ask", "--json"],  # no query
        ["ask", "Redis", "--json", "--namespace", 'x" , "evil'],
        ["ask", "Redis", "--json", "--namespace", "../etc"],
        ["ask", "Redis", "--json", "--type", "bogus"],
        ["ask", "Redis", "--json", "--min-similarity", "1e3"],
        ["ask", "Redis", "--json", "--min-similarity", "-1"],
        ["ask", "Redis", "--json", "-n", "-5"],
        ["ask", "Redis", "--json", "--coun", "3"],  # argparse abbreviation
        ["ask", "Redis", "--json", "--count=3"],
        ["ask", "one", "two", "--json"],
    ],
)
def test_ambiguous_arguments_fall_back(native_bin, daemon, argv):
    home, port, env = daemon
    n = _native(native_bin, argv, env)
    p = _py(argv, env)
    assert b"fallback" in n.stderr
    assert _norm(n.stdout) == _norm(p.stdout) and n.returncode == p.returncode


def test_version_matches_python(native_bin, daemon):
    home, port, env = daemon
    for flag in ("--version", "-v", "version"):
        assert _native(native_bin, [flag], env, trace=False).stdout == _py([flag], env).stdout


def test_other_commands_still_go_to_python(native_bin, daemon):
    home, port, env = daemon
    argv = ["brief", "--format", "json"]
    n, p = _native(native_bin, argv, env), _py(argv, env)
    assert _norm(n.stdout) == _norm(p.stdout)


# ------------------------------------------------------------------ fake daemons (protocol level)
class FakeDaemon:
    """Speaks just enough of the Mesh HTTP protocol; records every byte it receives."""

    def __init__(self, token: str, recall_body: bytes | None, chunked: bool = False, honest: bool = True):
        self.token, self.recall_body, self.chunked, self.honest = token, recall_body, chunked, honest
        self.received: list[bytes] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.stop = False
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def _loop(self):
        self.sock.settimeout(0.2)
        while not self.stop:
            try:
                c, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c: socket.socket):
        c.settimeout(2)
        data = b""
        try:
            while b"\r\n\r\n" not in data:
                chunk = c.recv(65536)
                if not chunk:
                    return
                data += chunk
            head, _, rest = data.partition(b"\r\n\r\n")
            clen = 0
            for line in head.split(b"\r\n")[1:]:
                if line.lower().startswith(b"content-length:"):
                    clen = int(line.split(b":")[1])
            while len(rest) < clen:
                rest += c.recv(65536)
            self.received.append(data + rest[len(data) - len(head) - 4 :] if False else head + b"\r\n\r\n" + rest)
            first = head.split(b"\r\n")[0]
            if first.startswith(b"GET /identity"):
                nonce = first.split(b"nonce=")[1].split(b" ")[0].decode()
                proof = hmac.new(self.token.encode(), bytes.fromhex(nonce), hashlib.sha256).hexdigest()
                if not self.honest:
                    proof = "0" * 64
                body = json.dumps({"status": "ok", "proof": proof, "pid": 1, "version": "x"}).encode()
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
            elif first.startswith(b"POST /recall") and self.recall_body is not None:
                if self.chunked:
                    out = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n"
                    b = self.recall_body
                    for i in range(0, len(b), 777):
                        piece = b[i : i + 777]
                        out += b"%x\r\n" % len(piece) + piece + b"\r\n"
                    out += b"0\r\n\r\n"
                    c.sendall(out)
                else:
                    c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(self.recall_body) + self.recall_body)
            else:
                c.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        except OSError:
            pass
        finally:
            c.close()

    def close(self):
        self.stop = True
        self.sock.close()


def _stub_python(tmp_path: Path) -> str:
    """Interpreter stand-in so the Python FALLBACK cannot talk to the fake daemon: we only want to see
    what the native client itself put on the wire."""
    stub = tmp_path / "stub_python.sh"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    return str(stub)


def _home_with_token(tmp_path: Path) -> Path:
    home = tmp_path / "h"
    home.mkdir(mode=0o700)
    tok = home / "auth_token"
    tok.write_text(TOKEN)
    tok.chmod(0o600)
    (home / "daemon.pid").write_text("1")
    return home


TRICKY = {
    "status": "ok",
    "results": [
        {"id": "a", "content": "caf\u00e9 \u2615 \U0001f680 \u2028 end", "confidence": 0.1, "similarity": 1e-05,
         "big": 12345678901234567890, "tiny": 1e-07, "huge": 1e22, "negzero": -0.0, "neg": -3, "t": True, "n": None,
         "ctl": "a\u0001b\u001fc\u007fd\"q\"\\s\n\r\t\b\f", "empty_l": [], "empty_d": {}, "nest": {"x": [1, [2, {"y": []}]], "z": {}}},
        {"id": "b", "content": "", "tags": []},
    ],
    "other_possible_memories": [{"id": "o", "snippet": "s"}],
    "filter_hint": {"hint": ["a", {"b": 1.5}]},
}


@pytest.mark.parametrize("chunked", [False, True])
def test_tricky_json_and_chunked_replies_match_json_dumps(native_bin, tmp_path, chunked):
    body = json.dumps(TRICKY, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    d = FakeDaemon(TOKEN, body, chunked=chunked)
    try:
        home = _home_with_token(tmp_path)
        env = _env(home, d.port)
        n = _native(native_bin, ["ask", "q \"x\"", "--json"], env)
    finally:
        d.close()
    expected = json.dumps(
        {"namespace": "gold", "query": 'q "x"', "count": 2, "results": TRICKY["results"],
         "other_possible_memories": TRICKY["other_possible_memories"], "filter_hint": TRICKY["filter_hint"]},
        indent=2, default=str, ensure_ascii=False,
    ) + "\n"
    assert n.stdout.decode("utf-8") == expected, n.stderr
    assert b"fallback" not in n.stderr


def test_reply_over_64kb_is_never_truncated(native_bin, tmp_path):
    results = [{"id": str(i), "content": "x" * 5000} for i in range(60)]
    body = json.dumps({"status": "ok", "results": results, "other_possible_memories": [], "filter_hint": None}).encode()
    assert len(body) > 300_000
    d = FakeDaemon(TOKEN, body)
    try:
        env = _env(_home_with_token(tmp_path), d.port)
        n = _native(native_bin, ["ask", "q", "--json"], env)
    finally:
        d.close()
    out = json.loads(n.stdout)
    assert out["count"] == 60 and out["results"][59]["content"] == "x" * 5000


def test_token_never_sent_to_unverified_listener(native_bin, tmp_path):
    d = FakeDaemon(TOKEN, b'{"status":"ok","results":[]}', honest=False)
    try:
        env = _env(_home_with_token(tmp_path), d.port)
        env["MESH_PYTHON"] = _stub_python(tmp_path)
        _native(native_bin, ["ask", "q", "--json"], env)
        time.sleep(0.3)
    finally:
        d.close()
    blob = b"".join(d.received)
    assert b"/identity" in blob  # the challenge was attempted...
    assert TOKEN.encode() not in blob  # ...but the token was NEVER transmitted
    assert b"POST /recall" not in blob


def test_token_is_sent_only_after_proof(native_bin, tmp_path):
    body = json.dumps({"status": "ok", "results": [], "other_possible_memories": [], "filter_hint": None}).encode()
    d = FakeDaemon(TOKEN, body)
    try:
        env = _env(_home_with_token(tmp_path), d.port)
        n = _native(native_bin, ["ask", "q", "--json"], env)
    finally:
        d.close()
    assert n.returncode == 0
    assert TOKEN.encode() not in d.received[0] and b"/identity" in d.received[0]
    assert TOKEN.encode() in d.received[1] and b"POST /recall" in d.received[1]


def test_unsafe_token_file_falls_back(native_bin, tmp_path):
    body = b'{"status":"ok","results":[]}'
    d = FakeDaemon(TOKEN, body)
    try:
        home = _home_with_token(tmp_path)
        (home / "auth_token").chmod(0o644)  # group/other readable -> Python must repair/decide
        stub = _stub_python(tmp_path)
        n = _native(native_bin, ["ask", "q", "--json"], _env(home, d.port, MESH_PYTHON=stub))
        assert b"fallback" in n.stderr and not d.received
        (home / "auth_token").chmod(0o600)
        real = home / "real_token"
        (home / "auth_token").rename(real)
        (home / "auth_token").symlink_to(real)
        n = _native(native_bin, ["ask", "q", "--json"], _env(home, d.port, MESH_PYTHON=stub))
        assert b"fallback" in n.stderr and not d.received
    finally:
        d.close()


def test_non_loopback_host_never_contacted(native_bin, tmp_path):
    d = FakeDaemon(TOKEN, b'{"status":"ok","results":[]}')
    try:
        env = _env(_home_with_token(tmp_path), d.port, MESH_HTTP_HOST="localhost", MESH_PYTHON=_stub_python(tmp_path))
        n = _native(native_bin, ["ask", "q", "--json"], env)
        assert b"fallback" in n.stderr and not d.received
    finally:
        d.close()


def test_native_memory_types_match_python():
    out = subprocess.run(
        [sys.executable, "-c", "from mesh.memory import VALID_MEMORY_TYPES as V; print(','.join(sorted(V)))"],
        capture_output=True, text=True, cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT)),
    ).stdout.strip().split(",")
    src = SRC.read_text(encoding="utf-8")
    listed = set(__import__("re").findall(r'"([a-z_]+)"', src[src.index("MEMORY_TYPES[]") : src.index("valid_memory_type")]))
    assert listed == set(out)

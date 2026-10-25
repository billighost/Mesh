"""apply_mesh_patches.py (round 1) and apply_round2.py must be safe to run any number of times, in any order."""
from __future__ import annotations

import hashlib
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
R1 = ROOT / "apply_mesh_patches.py"
R2 = ROOT / "apply_round2.py"

pytestmark = pytest.mark.skipif(not R2.exists(), reason="patch scripts not present")


def _load_patches():
    spec = importlib.util.spec_from_file_location("apply_round2_mod", R2)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.PATCHES


def _write_like(path: Path, original: bytes, text_lf: str) -> None:
    """Write `text_lf` (LF) back using the line-ending style `original` had."""
    data = text_lf.replace("\n", "\r\n") if b"\r\n" in original else text_lf
    path.write_bytes(data.encode("utf-8"))


def _files(root: Path) -> dict[str, bytes]:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix in {".py", ".c"} and ".pre_" not in p.name and "__pycache__" not in p.parts:
            out[str(p.relative_to(root))] = p.read_bytes()
    return out


def _digest(root: Path) -> str:
    h = hashlib.sha256()
    for k, v in _files(root).items():
        h.update(k.encode())
        h.update(v)
    return h.hexdigest()


def _run(root: Path, script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, script, *args], cwd=root, capture_output=True, text=True)


@pytest.fixture()
def tree(tmp_path):
    """A copy of the repo rolled back to its round-1 state by un-applying every round-2 edit."""
    dst = tmp_path / "repo"
    dst.mkdir()
    for rel in ("mesh", "tests"):
        shutil.copytree(ROOT / rel, dst / rel, ignore=shutil.ignore_patterns("__pycache__", "*.pre_*", "*.so", "*.dll"))
    for name in ("apply_mesh_patches.py", "apply_round2.py", "benchmark_all_surfaces.py"):
        if (ROOT / name).exists():
            shutil.copy2(ROOT / name, dst / name)
    for f, old, new in reversed(_load_patches()):
        p = dst / f
        raw = p.read_bytes()
        text = raw.decode("utf-8").replace("\r\n", "\n")
        if new in text:
            assert text.count(new) == 1, f"ambiguous new text in {f}"
            _write_like(p, raw, text.replace(new, old))
    return dst


def test_apply_changes_files_then_is_a_noop_forever(tree):
    base = _digest(tree)
    r = _run(tree, "apply_round2.py")
    assert r.returncode == 0 and "0 edit(s) to apply" not in r.stdout, r.stdout + r.stderr
    once = _digest(tree)
    assert once != base
    for _ in range(5):
        r = _run(tree, "apply_round2.py")
        assert r.returncode == 0 and "0 edit(s) to apply" in r.stdout
        assert _digest(tree) == once, "a repeated run changed files"


def test_matches_the_repo_tree_exactly(tree):
    assert _run(tree, "apply_round2.py").returncode == 0
    for rel, content in _files(tree).items():
        if rel.startswith("tests/"):
            continue
        assert content == (ROOT / rel).read_bytes(), f"{rel} differs from the shipped tree"


def test_chain_round1_round2_in_any_order_is_stable(tree):
    assert _run(tree, "apply_round2.py").returncode == 0
    settled = _digest(tree)
    for _ in range(3):
        for script in ("apply_mesh_patches.py", "apply_round2.py", "apply_round2.py", "apply_mesh_patches.py"):
            r = _run(tree, script)
            assert r.returncode == 0, r.stdout + r.stderr
            assert _digest(tree) == settled
    r1 = _run(tree, "apply_mesh_patches.py", "--check")
    assert r1.returncode == 0 and "0 edit(s) to apply" in r1.stdout


def test_check_never_writes(tree):
    base = _digest(tree)
    assert _run(tree, "apply_round2.py", "--check").returncode == 0
    assert _digest(tree) == base
    assert not list(tree.rglob("*.pre_round2"))


def test_revert_then_reapply_round_trips(tree):
    base = _digest(tree)
    assert _run(tree, "apply_round2.py").returncode == 0
    patched = _digest(tree)
    assert _run(tree, "apply_round2.py", "--revert").returncode == 0
    assert _digest(tree) == base
    assert _run(tree, "apply_round2.py").returncode == 0
    assert _digest(tree) == patched
    assert _run(tree, "apply_round2.py").returncode == 0 and _digest(tree) == patched


def test_backup_is_taken_once_and_never_overwritten(tree):
    assert _run(tree, "apply_round2.py").returncode == 0
    bak = tree / "mesh" / "store.py.pre_round2"
    first = bak.read_bytes()
    for _ in range(3):
        assert _run(tree, "apply_round2.py").returncode == 0
    assert bak.read_bytes() == first


def test_partial_application_only_applies_the_missing_edit(tree):
    assert _run(tree, "apply_round2.py").returncode == 0
    settled = _digest(tree)
    patches = _load_patches()
    f, old, new = next(p for p in patches if p[0] == "mesh/crypto.py")
    path = tree / f
    raw = path.read_bytes()
    _write_like(path, raw, raw.decode().replace("\r\n", "\n").replace(new, old))
    r = _run(tree, "apply_round2.py")
    assert r.returncode == 0 and "1 edit(s) to apply" in r.stdout, r.stdout
    assert _digest(tree) == settled


def test_missing_anchor_aborts_without_writing(tree):
    assert _run(tree, "apply_round2.py").returncode == 0
    patches = _load_patches()
    f, old, new = next(p for p in patches if p[0] == "mesh/crypto.py")
    path = tree / f
    raw = path.read_bytes()
    _write_like(path, raw, raw.decode().replace("\r\n", "\n").replace(new, "# anchor destroyed by a local edit\n"))
    other = tree / "mesh" / "store.py"
    oraw = other.read_bytes()
    _write_like(other, oraw, oraw.decode().replace("\r\n", "\n").replace(patches[0][2], patches[0][1]))
    before = _digest(tree)
    r = _run(tree, "apply_round2.py")
    assert r.returncode == 2 and "ABORTED" in r.stdout
    assert _digest(tree) == before, "an aborted run must not write anything (not even the other files)"


def test_crlf_files_stay_crlf_and_stay_stable(tree):
    for p in (tree / "mesh").glob("*.py"):
        p.write_bytes(p.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    (tree / "benchmark_all_surfaces.py").write_bytes(
        (tree / "benchmark_all_surfaces.py").read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    )
    assert _run(tree, "apply_round2.py").returncode == 0
    settled = _digest(tree)
    for rel in ("mesh/store.py", "mesh/memory.py", "mesh/cli.py", "mesh/http_server.py", "benchmark_all_surfaces.py"):
        b = (tree / rel).read_bytes()
        assert b.count(b"\r\n") == b.count(b"\n"), f"{rel}: mixed line endings"
    for _ in range(3):
        assert _run(tree, "apply_round2.py").returncode == 0
        assert _digest(tree) == settled
    assert _run(tree, "apply_round2.py", "--revert").returncode == 0


def test_every_edit_anchor_is_unique_and_new_text_is_detectable(tree):
    patches = _load_patches()
    for f, old, new in patches:
        text = (tree / f).read_bytes().decode().replace("\r\n", "\n")
        assert text.count(old) == 1 and new not in text, f"{f}: {old[:70]!r}"


def _load_round1_patches():
    spec = importlib.util.spec_from_file_location("apply_mesh_patches_mod", R1)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.PATCHES


@pytest.mark.skipif(not R1.exists(), reason="round-1 patcher not present")
def test_fresh_unpatched_tree_ends_identical_to_the_shipped_tree(tree):
    """Round 1 never applied (a truly pristine checkout): round 1 then round 2 must converge on the same bytes."""
    for f, old, new in reversed(_load_round1_patches()):
        p = tree / f
        if not p.exists():
            continue
        raw = p.read_bytes()
        text = raw.decode("utf-8").replace("\r\n", "\n")
        if new in text and f != "apply_mesh_patches.py":
            _write_like(p, raw, text.replace(new, old, 1))
    pristine = _digest(tree)
    r1 = _run(tree, "apply_mesh_patches.py")
    assert r1.returncode == 0, r1.stdout + r1.stderr
    r2 = _run(tree, "apply_round2.py")
    assert r2.returncode == 0, r2.stdout + r2.stderr
    settled = _digest(tree)
    assert settled != pristine
    for _ in range(2):
        for script in ("apply_mesh_patches.py", "apply_round2.py"):
            assert _run(tree, script).returncode == 0
        assert _digest(tree) == settled
    for rel, content in _files(tree).items():
        if rel.startswith("tests/") or rel == "apply_mesh_patches.py":
            continue
        assert content == (ROOT / rel).read_bytes(), f"{rel} differs from the shipped tree"

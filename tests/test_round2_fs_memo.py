"""Round 3: short-lived reuse of filesystem checks must keep every security guarantee."""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

import pytest

from mesh import audit, crypto


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MESH_HOME", str(tmp_path))
    monkeypatch.setenv("MESH_NO_AUTO_DAEMON", "1")
    for k in ("MESH_ENCRYPT_AT_REST", "MESH_PASSPHRASE", "MESH_ENCRYPTION_KEY"):
        monkeypatch.delenv(k, raising=False)
    audit.clear_path_trust_cache()
    crypto._invalidate_enc_memo()
    yield tmp_path
    audit.clear_path_trust_cache()
    crypto._invalidate_enc_memo()


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_symlink_swap_is_caught_after_the_memo_window(home):
    target = home / "data.db"
    target.write_text("x")
    other = home / "elsewhere"
    other.write_text("y")
    audit.reject_symlink(target)  # full check, trusted
    audit.reject_symlink(target)  # inside the window: no extra syscall, still fine
    target.unlink()
    target.symlink_to(other)
    time.sleep(0.03)  # default window is 10 ms
    with pytest.raises(ValueError, match="symlinked"):
        audit.reject_symlink(target)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_memo_can_be_disabled_for_immediate_detection(home, monkeypatch):
    monkeypatch.setenv("MESH_FS_MEMO_MS", "0")
    target = home / "data2.db"
    target.write_text("x")
    other = home / "elsewhere2"
    other.write_text("y")
    audit.reject_symlink(target)
    target.unlink()
    target.symlink_to(other)
    with pytest.raises(ValueError, match="symlinked"):
        audit.reject_symlink(target)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_a_symlink_is_never_trusted_on_first_sight(home):
    real = home / "real.db"
    real.write_text("x")
    link = home / "link.db"
    link.symlink_to(real)
    for _ in range(3):
        with pytest.raises(ValueError):
            audit.reject_symlink(link)


def test_encryption_toggle_takes_effect_immediately(home, monkeypatch):
    assert crypto.is_encryption_enabled() is False
    try:
        crypto.enable_encryption_at_rest("a-test-passphrase")
        os.environ.pop("MESH_PASSPHRASE", None)  # set directly by enable_...: remove it for real (not via monkeypatch)
        assert crypto.is_encryption_enabled() is True  # no waiting for the memo window
        crypto.disable_encryption_at_rest()
        assert crypto.is_encryption_enabled() is False
    finally:
        os.environ.pop("MESH_PASSPHRASE", None)
        crypto.disable_encryption_at_rest()


def test_encryption_memo_never_leaks_across_mesh_homes(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv("MESH_NO_AUTO_DAEMON", "1")
    for k in ("MESH_ENCRYPT_AT_REST", "MESH_PASSPHRASE", "MESH_ENCRYPTION_KEY"):
        monkeypatch.delenv(k, raising=False)
    (a / ".encryption_enabled").write_bytes(b"1")
    monkeypatch.setenv("MESH_HOME", str(a))
    audit.clear_path_trust_cache()
    assert crypto.is_encryption_enabled() is True
    monkeypatch.setenv("MESH_HOME", str(b))
    assert crypto.is_encryption_enabled() is False  # immediately, same millisecond
    monkeypatch.setenv("MESH_HOME", str(a))
    assert crypto.is_encryption_enabled() is True


def test_audit_stamp_semantics_unchanged(tmp_path):
    db = tmp_path / "audit_x.db"
    assert audit._get_audit_db_stamp(db) is None  # missing
    db.write_bytes(b"")
    assert audit._get_audit_db_stamp(db) is None  # empty file
    db.write_bytes(b"abc")
    st = db.stat()
    assert audit._get_audit_db_stamp(db) == (st.st_mtime_ns, 3, 0, 0)  # no WAL
    wal = tmp_path / "audit_x.db-wal"
    wal.write_bytes(b"walwal")
    wst = wal.stat()
    assert audit._get_audit_db_stamp(db) == (st.st_mtime_ns, 3, wst.st_mtime_ns, 6)


def test_db_stamp_semantics_unchanged(tmp_path):
    from mesh import store

    db = tmp_path / "ns.db"
    assert store._get_db_stamp(db) is None
    db.write_bytes(b"abc")
    st = db.stat()
    assert store._get_db_stamp(db) == (st.st_mtime_ns, 3, 0, 0)


def test_fast_argument_parser_renders_identical_help():
    from mesh import cli

    def build(cls):
        p = cls(prog="mesh x", description="desc", epilog="examples:\n  mesh x  # y",
                formatter_class=argparse.RawDescriptionHelpFormatter)
        p.add_argument("-n", "--count", type=int, default=5, help="how many")
        p.add_argument("--json", action="store_true", help="json output")
        sub = p.add_subparsers(dest="cmd")
        s = sub.add_parser("list", help="list things")
        s.add_argument("--status", choices=["open", "done"])
        return p

    ref, fast = build(argparse.ArgumentParser), build(cli._FastArgumentParser)
    assert fast.format_help() == ref.format_help()
    assert fast.parse_args(["-n", "3", "--json"]) == ref.parse_args(["-n", "3", "--json"])
    assert fast.parse_args(["list", "--status", "open"]) == ref.parse_args(["list", "--status", "open"])
    sub_fast = fast._subparsers._group_actions[0].choices["list"]
    assert isinstance(sub_fast, cli._FastArgumentParser)


def test_colorize_memo_returns_the_original_answer():
    from mesh import cli

    cli._install_colorize_memo()
    if sys.version_info < (3, 13):
        pytest.skip("no _colorize.can_colorize before Python 3.13")
    import _colorize

    first = _colorize.can_colorize()
    assert _colorize.can_colorize() == first
    cli._install_colorize_memo()  # idempotent: never wraps twice
    assert getattr(_colorize.can_colorize, "_mesh_memo", False)

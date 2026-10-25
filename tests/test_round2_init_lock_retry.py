"""A transient 'database is locked' while a namespace is first initialised is retried (bounded); real errors are not."""
from __future__ import annotations

import sqlite3

import pytest

from mesh import store as st


def _cls(tmp_path, monkeypatch):
    monkeypatch.setenv("MESH_HOME", str(tmp_path))
    monkeypatch.setenv("MESH_NO_AUTO_DAEMON", "1")
    return st.MeshStore


def test_locked_during_init_is_retried_then_succeeds(tmp_path, monkeypatch):
    cls = _cls(tmp_path, monkeypatch)
    real = cls._init_db
    calls = {"n": 0}

    def flaky(self):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise sqlite3.OperationalError("database is locked")
        return real(self)

    monkeypatch.setattr(cls, "_init_db", flaky)
    s = cls("retry-ok", create=True)
    assert calls["n"] == 4
    assert s.db_path.exists()


def test_non_lock_errors_are_not_retried(tmp_path, monkeypatch):
    cls = _cls(tmp_path, monkeypatch)
    calls = {"n": 0}

    def broken(self):
        calls["n"] += 1
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(cls, "_init_db", broken)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        cls("retry-bad", create=True)
    assert calls["n"] == 1


def test_retry_is_bounded(tmp_path, monkeypatch):
    cls = _cls(tmp_path, monkeypatch)
    monkeypatch.setenv("MESH_INIT_LOCK_RETRY_SECONDS", "0.2")
    calls = {"n": 0}

    def always_locked(self):
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(cls, "_init_db", always_locked)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        cls("retry-bounded", create=True)
    assert 2 <= calls["n"] < 60

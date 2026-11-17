"""Contract tests for the privately bundled SQLite used by every Mesh path."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from mesh.sqlite_runtime import is_patched_sqlite, sqlite3


@pytest.mark.parametrize(
    ("version", "safe"),
    [
        ("3.44.5", False),
        ("3.44.6", True),
        ("3.45.0", False),
        ("3.50.4", False),
        ("3.50.7", True),
        ("3.51.0", False),
        ("3.51.2", False),
        ("3.51.3", True),
        ("3.54.1", True),
        ("invalid", False),
    ],
)
def test_fixed_release_lines(version, safe):
    assert is_patched_sqlite(version) is safe


def test_bundled_runtime_is_durable_and_has_fts(tmp_path):
    assert is_patched_sqlite(sqlite3.sqlite_version)
    with sqlite3.connect(tmp_path / "fake.db", isolation_level=None) as conn:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE VIRTUAL TABLE facts USING fts5(content, tokenize='porter')")
        conn.execute("INSERT INTO facts VALUES (?)", ("The fake service port is 8042.",))
        assert conn.execute("SELECT count(*) FROM facts WHERE facts MATCH ?", ("port",)).fetchone()[0] == 1
    conn.close()


def test_rows_and_empty_descriptions(tmp_path):
    conn = sqlite3.connect(tmp_path / "fake.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT 42 AS answer, 'fake' AS label").fetchone()
    assert row[0] == row["ANSWER"] == 42
    assert dict(row) == {"answer": 42, "label": "fake"}
    assert row.keys() == ["answer", "label"]
    cursor = conn.execute("SELECT 1 AS empty_value WHERE 0")
    assert cursor.fetchone() is None
    assert cursor.description[0][0] == "empty_value"
    assert row["label"] == "fake"
    conn.close()


def test_rollback_keeps_rows_and_events_atomic(tmp_path):
    conn = sqlite3.connect(tmp_path / "fake.db", isolation_level=None)
    conn.execute("CREATE TABLE facts(id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE events(id INTEGER PRIMARY KEY)")
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO facts VALUES (1)")
            conn.execute("INSERT INTO events VALUES (1)")
            conn.execute("INSERT INTO events VALUES (1)")
    assert conn.execute("SELECT count(*) FROM facts").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    assert not conn.in_transaction
    conn.close()


def test_legacy_implicit_transactions_and_savepoint(tmp_path):
    conn = sqlite3.connect(tmp_path / "fake.db")
    conn.execute("CREATE TABLE facts(id INTEGER PRIMARY KEY)")
    conn.execute("INSERT\nINTO facts VALUES (1)")
    assert conn.in_transaction
    conn.execute("SAVEPOINT fake_nested")
    conn.execute("INSERT INTO facts VALUES (2)")
    conn.execute("ROLLBACK TO fake_nested")
    conn.execute("RELEASE fake_nested")
    conn.commit()
    assert conn.execute("SELECT id FROM facts").fetchall() == [(1,)]
    conn.close()


def test_backup_includes_committed_wal(tmp_path):
    source = sqlite3.connect(tmp_path / "source.db", isolation_level=None)
    source.execute("PRAGMA journal_mode=WAL")
    source.execute("CREATE TABLE facts(value TEXT)")
    source.execute("INSERT INTO facts VALUES (?)", ("fake committed value",))
    target = sqlite3.connect(tmp_path / "backup.db", isolation_level=None)
    source.backup(target)
    assert target.execute("SELECT value FROM facts").fetchall() == [("fake committed value",)]
    target.close()
    source.close()


def test_discarded_pragma_cursor_does_not_block_commit(tmp_path):
    conn = sqlite3.connect(tmp_path / "fake.db", isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("CREATE TABLE facts(value TEXT)")
    conn.execute("INSERT INTO facts VALUES ('fake')")
    conn.commit()
    assert conn.execute("SELECT count(*) FROM facts").fetchone()[0] == 1
    conn.close()


def test_connection_factory_and_thread_check(tmp_path):
    class CustomConnection(sqlite3.Connection):
        pass

    conn = sqlite3.connect(tmp_path / "fake.db", factory=CustomConnection)
    assert isinstance(conn, CustomConnection)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(conn.execute, "SELECT 1")
        with pytest.raises(sqlite3.ProgrammingError):
            future.result()
    conn.close()
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_read_only_connections_reject_writes(tmp_path):
    path = tmp_path / "fake.db"
    writer = sqlite3.connect(path, isolation_level=None)
    writer.execute("CREATE TABLE facts(value TEXT)")
    writer.close()
    reader = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
    with pytest.raises(sqlite3.OperationalError):
        reader.execute("INSERT INTO facts VALUES ('fake')")
    reader.close()

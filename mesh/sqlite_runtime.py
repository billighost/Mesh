"""One lazy, patched SQLite runtime for Mesh storage and maintenance.

The small DB-API facade preserves Mesh's connection subclasses while APSW
privately supplies SQLite. Do not open a Mesh database through a second SQLite
library in the same process; its locks and connection state are not shared.
"""

from __future__ import annotations

import os
import sqlite3 as _stdlib
import sys
import threading
import time
import weakref
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any

_runtime: Any = None
_runtime_lock = threading.Lock()


def is_patched_sqlite(version: str) -> bool:
    """Recognize fixed WAL-reset releases, including the two backport lines."""
    try:
        parts = tuple(int(part) for part in version.split(".")[:3])
    except (AttributeError, TypeError, ValueError):
        return False
    if len(parts) != 3:
        return False
    return parts >= (3, 51, 3) or parts[:2] == (3, 50) and parts[2] >= 7 or parts[:2] == (3, 44) and parts[2] >= 6


def _load_runtime() -> Any:
    global _runtime
    if _runtime is None:
        with _runtime_lock:
            if _runtime is None:
                try:
                    import apsw
                except ImportError as exc:
                    raise RuntimeError(
                        "Mesh requires its bundled patched SQLite runtime. "
                        "Reinstall mesh-context-layer to install the APSW dependency."
                    ) from exc
                version = apsw.sqlitelibversion()
                if not is_patched_sqlite(version) or "THREADSAFE=0" in apsw.compile_options:
                    raise RuntimeError(f"Unsafe SQLite runtime {version}; reinstall Mesh with patched SQLite.")
                probe = apsw.Connection(":memory:")
                try:
                    probe.execute("CREATE VIRTUAL TABLE runtime_check USING fts5(content)")
                finally:
                    probe.close()
                _runtime = apsw
    return _runtime


def _translate(exc: Exception) -> Exception:
    runtime = _load_runtime()
    if isinstance(exc, runtime.ConstraintError):
        kind = _stdlib.IntegrityError
    elif isinstance(
        exc, (runtime.BindingsError, runtime.MisuseError, runtime.ConnectionClosedError, runtime.CursorClosedError)
    ):
        kind = _stdlib.ProgrammingError
    elif isinstance(exc, (runtime.CorruptError, runtime.NotADBError)):
        kind = _stdlib.DatabaseError
    elif isinstance(exc, runtime.Error):
        kind = _stdlib.OperationalError
    else:
        return exc
    result = kind(str(exc))
    if hasattr(exc, "extendedresult"):
        result.sqlite_errorcode = exc.extendedresult
        result.sqlite_errorname = runtime.mapping_extended_result_codes.get(exc.extendedresult, "SQLITE_ERROR")
    return result


def _call(function: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return function(*args, **kwargs)
    except Exception as exc:
        translated = _translate(exc)
        if translated is exc:
            raise
        raise translated from exc


class Row(Sequence[Any]):
    """Tuple/index/key access matching the Row operations used by Mesh."""

    __slots__ = ("_values", "_names", "_lookup")

    def __init__(self, cursor: Cursor, values: Sequence[Any]):
        self._values = tuple(values)
        self._names = cursor._row_names
        self._lookup = cursor._row_lookup

    def __getitem__(self, key: int | slice | str) -> Any:
        if isinstance(key, str):
            try:
                key = self._lookup[key.casefold()]
            except KeyError:
                raise IndexError("No item with that key") from None
        return self._values[key]

    def __len__(self) -> int:
        return len(self._values)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def keys(self) -> list[str]:
        return list(self._names)


class Cursor:
    def __init__(self, connection: Connection):
        connection._check()
        self.connection = connection
        self._cursor = connection._connection.cursor()
        self.arraysize = 1
        self.description: tuple[tuple[Any, ...], ...] | None = None
        self._row_names: tuple[str, ...] = ()
        self._row_lookup: dict[str, int] = {}
        self.rowcount = -1
        self.lastrowid: int | None = None
        self._closed = False
        cursor_ref = weakref.ref(self)

        def capture(cursor: Any, sql: str, bindings: Any) -> bool:
            wrapper = cursor_ref()
            return True if wrapper is None else wrapper._capture_description(cursor, sql, bindings)

        self._cursor.set_exec_trace(capture)

    def _capture_description(self, cursor: Any, sql: str, bindings: Any) -> bool:
        try:
            self.description = tuple((name, None, None, None, None, None, None) for name, _ in cursor.get_description())
        except _load_runtime().ExecutionCompleteError:
            self.description = None
        self._row_names = tuple(column[0] for column in self.description or ())
        self._row_lookup = {name.casefold(): index for index, name in enumerate(self._row_names)}
        return True

    def execute(self, sql: str, parameters: Sequence[Any] | Mapping[str, Any] = ()) -> Cursor:
        self._check()
        self.description = None
        self.connection._begin_implicit(sql)
        _call(self._cursor.execute, sql, parameters)
        self.rowcount = self.connection._connection.changes() if _is_dml(sql) else -1
        self.lastrowid = self.connection._connection.last_insert_rowid()
        return self

    def executemany(self, sql: str, parameters: Iterable[Sequence[Any] | Mapping[str, Any]]) -> Cursor:
        self._check()
        count = 0
        for values in parameters:
            self.execute(sql, values)
            if self.rowcount >= 0:
                count += self.rowcount
        self.rowcount = count if _is_dml(sql) else -1
        return self

    def _check(self) -> None:
        self.connection._check()
        if self._closed:
            raise _stdlib.ProgrammingError("Cannot operate on a closed cursor.")

    def fetchone(self) -> Any:
        self._check()
        value = _call(self._cursor.fetchone)
        if value is None:
            return None
        factory = self.connection.row_factory
        if factory is None:
            return tuple(value)
        if factory is _stdlib.Row:
            factory = Row
        return factory(self, tuple(value))

    def fetchmany(self, size: int | None = None) -> list[Any]:
        count = self.arraysize if size is None else size
        if count < 0:
            raise ValueError("fetchmany size must be nonnegative")
        result = []
        for _ in range(count):
            row = self.fetchone()
            if row is None:
                break
            result.append(row)
        return result

    def fetchall(self) -> list[Any]:
        return list(self)

    def __iter__(self) -> Iterator[Any]:
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row

    def close(self) -> None:
        if not self._closed:
            _call(self._cursor.close, force=True)
            self._closed = True

    def __del__(self) -> None:
        # A discarded result-producing PRAGMA must not keep a transaction open.
        cursor = getattr(self, "_cursor", None)
        if cursor is not None and not getattr(self, "_closed", True):
            try:
                cursor.close(force=True)
            except Exception:
                pass


def _is_dml(sql: str) -> bool:
    first = sql.lstrip().split(None, 1)
    return bool(first and first[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"})


class Connection:
    def __init__(
        self,
        database: str | os.PathLike[str],
        timeout: float = 5.0,
        detect_types: int = 0,
        isolation_level: str | None = "",
        check_same_thread: bool = True,
        factory: Any = None,
        cached_statements: int = 128,
        uri: bool = False,
        **kwargs: Any,
    ):
        if detect_types:
            raise _stdlib.NotSupportedError("Mesh stores explicit types; SQLite converter flags are unsupported.")
        if kwargs:
            raise TypeError(f"Unsupported connection options: {', '.join(sorted(kwargs))}")
        if isolation_level not in (None, "", "DEFERRED", "IMMEDIATE", "EXCLUSIVE"):
            raise ValueError("Invalid isolation_level")
        if timeout < 0:
            raise ValueError("timeout must be nonnegative")
        runtime = _load_runtime()
        flags = runtime.SQLITE_OPEN_READWRITE | runtime.SQLITE_OPEN_CREATE
        if uri:
            flags |= runtime.SQLITE_OPEN_URI
        self._connection = _call(
            runtime.Connection, os.fspath(database), flags=flags, statementcachesize=max(0, cached_statements)
        )
        self._thread_id = threading.get_ident()
        self._check_same_thread = check_same_thread
        self._closed = False
        self.isolation_level = isolation_level
        self.row_factory: Any = None
        self._connection.set_busy_timeout(int(timeout * 1000))
        self._connection.execute("PRAGMA synchronous=FULL")
        if sys.platform == "darwin":
            self._connection.execute("PRAGMA fullfsync=ON")
            self._connection.execute("PRAGMA checkpoint_fullfsync=ON")

    def _check(self) -> None:
        if self._closed:
            raise _stdlib.ProgrammingError("Cannot operate on a closed database.")
        if self._check_same_thread and threading.get_ident() != self._thread_id:
            raise _stdlib.ProgrammingError("SQLite objects created in a thread can only be used in that same thread.")

    def _begin_implicit(self, sql: str) -> None:
        if self.isolation_level is not None and not self.in_transaction and _is_dml(sql):
            _call(self._connection.execute, "BEGIN " + self.isolation_level)

    @property
    def in_transaction(self) -> bool:
        self._check()
        return not self._connection.get_autocommit()

    @property
    def total_changes(self) -> int:
        self._check()
        return self._connection.total_changes()

    def cursor(self, factory: Any = None) -> Cursor:
        self._check()
        return (factory or Cursor)(self)

    def execute(self, sql: str, parameters: Sequence[Any] | Mapping[str, Any] = ()) -> Cursor:
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql: str, parameters: Iterable[Sequence[Any] | Mapping[str, Any]]) -> Cursor:
        return self.cursor().executemany(sql, parameters)

    def executescript(self, script: str) -> Cursor:
        self.commit()
        result = self.cursor()
        _call(result._cursor.execute, script)
        return result

    def commit(self) -> None:
        if self.in_transaction:
            _call(self._connection.execute, "COMMIT")

    def rollback(self) -> None:
        if self.in_transaction:
            _call(self._connection.execute, "ROLLBACK")

    def __enter__(self) -> Connection:
        self._check()
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        if exc_type is None:
            try:
                self.commit()
            except Exception:
                self.rollback()
                raise
        else:
            self.rollback()
        return False

    def close(self) -> None:
        if not self._closed:
            _call(self._connection.close, force=True)
            self._closed = True

    def backup(
        self, target: Connection, *, pages: int = -1, progress: Any = None, name: str = "main", sleep: float = 0.25
    ) -> None:
        self._check()
        if not isinstance(target, Connection):
            raise TypeError("Backups require the same Mesh SQLite runtime.")
        target._check()
        runtime = _load_runtime()
        deadline = time.monotonic() + 30.0
        with target._connection.backup("main", self._connection, name) as backup:
            while not backup.done:
                try:
                    backup.step(pages)
                except (runtime.BusyError, runtime.LockedError) as exc:
                    if time.monotonic() >= deadline:
                        raise _stdlib.OperationalError("Backup remained busy.") from exc
                    time.sleep(max(0.0, sleep))
                if progress is not None:
                    progress(
                        runtime.SQLITE_DONE if backup.done else runtime.SQLITE_OK, backup.remaining, backup.page_count
                    )

    def create_function(self, name: str, narg: int, function: Any, *, deterministic: bool = False) -> None:
        self._check()
        _call(self._connection.create_scalar_function, name, function, narg, deterministic=deterministic)

    def set_progress_handler(self, handler: Any, n: int = 0) -> None:
        self._check()
        _call(self._connection.set_progress_handler, handler, n)

    def set_authorizer(self, authorizer: Any) -> None:
        self._check()
        _call(self._connection.set_authorizer, authorizer)


def connect(database: str | os.PathLike[str], *args: Any, factory: Any = Connection, **kwargs: Any) -> Connection:
    return factory(database, *args, **kwargs)


class _SQLiteFacade:
    Connection = Connection
    Cursor = Cursor
    Row = Row
    connect = staticmethod(connect)
    Error = _stdlib.Error
    DatabaseError = _stdlib.DatabaseError
    OperationalError = _stdlib.OperationalError
    ProgrammingError = _stdlib.ProgrammingError
    IntegrityError = _stdlib.IntegrityError

    @property
    def sqlite_version(self) -> str:
        return _load_runtime().sqlitelibversion()

    @property
    def sqlite_version_info(self) -> tuple[int, ...]:
        return tuple(int(value) for value in self.sqlite_version.split("."))

    def __getattr__(self, name: str) -> Any:
        return getattr(_stdlib, name)


sqlite3 = _SQLiteFacade()

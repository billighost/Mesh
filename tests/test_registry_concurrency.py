"""Registry progress, replacement races, root isolation, and resource ownership."""
from __future__ import annotations

import gc
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mesh import audit, http_server, mcp_server, namespaces, operations
from mesh.store import NamespaceNotFoundError


class ControlledPath:
    """A real temporary path with a controlled slow or failing filesystem check."""

    def __init__(self, path: Path, *, entered=None, release=None, missing=False):
        self.path = path
        self.entered = entered
        self.release = release
        self.missing = missing

    def __fspath__(self):
        return os.fspath(self.path)

    def __str__(self):
        return str(self.path)

    def __getattr__(self, name):
        return getattr(self.path, name)

    def _check(self):
        if self.entered is not None:
            self.entered.set()
            assert self.release.wait(5)
        if self.missing:
            raise FileNotFoundError("isolated stale database path")

    def stat(self):
        self._check()
        return self.path.stat()

    def exists(self):
        self._check()
        return self.path.exists()


class FakeMesh:
    def __init__(self, namespace, agent_id, path, *, create=False, closed=None):
        self.namespace = namespace
        self.agent_id = agent_id
        self.store = SimpleNamespace(namespace=namespace, db_path=path, create=create)
        self.closed = closed

    def close(self):
        if self.closed is not None:
            self.closed.append((self.namespace, self.agent_id))


@dataclass
class Registry:
    kind: str
    module: Any
    entries: Any
    root: Path
    closed: list

    @property
    def lock(self):
        return self.module._MESH_LOCK

    @property
    def capacity(self):
        return 32 if self.kind == "sdk" else 4

    def lookup(self, namespace="facts", agent_id="test-agent", *, root=None, create=False):
        with audit.mesh_dir_context(root or self.root):
            function = self.module._get_mesh if self.kind == "sdk" else self.module.get_mesh
            return function(namespace=namespace, agent_id=agent_id, create=create)

    def evict(self, namespace=None, *, root=None, mesh_home=None):
        function = {
            "sdk": self.module.evict_mesh_cache if self.kind == "sdk" else None,
            "http": self.module.evict_http_mesh_cache if self.kind == "http" else None,
            "mcp": self.module.evict_mcp_mesh_cache if self.kind == "mcp" else None,
        }[self.kind]
        with audit.mesh_dir_context(root or self.root):
            kwargs = {"mesh_home": mesh_home} if mesh_home is not None else {}
            function(namespace, **kwargs)

    def seed(self, namespace="facts", agent_id="test-agent", *, root=None, path=None, create=False):
        root = (root or self.root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        physical_path = root / f"mesh_{namespace}.db"
        physical_path.write_bytes(b"temporary registry test path")
        instance = FakeMesh(namespace, agent_id, path or physical_path, create=create, closed=self.closed)
        with self.lock:
            self.entries[(root, namespace, agent_id)] = instance
        return instance


@pytest.fixture(params=["sdk", "http", "mcp"])
def registry(request, tmp_path, monkeypatch):
    for name, child in {
        "MESH_HOME": "mesh", "HOME": "home", "USERPROFILE": "home",
        "APPDATA": "appdata", "LOCALAPPDATA": "localappdata", "XDG_CONFIG_HOME": "xdg",
    }.items():
        path = tmp_path / child
        path.mkdir(exist_ok=True)
        monkeypatch.setenv(name, str(path))
    for name in ("MESH_NO_AUTO_DAEMON", "MESH_NO_MODEL_DOWNLOAD", "MESH_NO_UPDATE_CHECK", "MESH_NO_OUTBOX_WORKER"):
        monkeypatch.setenv(name, "1")
    for name in ("MESH_NAMESPACE", "MESH_LOCKED_NAMESPACE", "MESH_AUTHENTICATED_AGENT_ID", "MESH_MAX_OPEN_NAMESPACES"):
        monkeypatch.delenv(name, raising=False)
    module, entries = {
        "sdk": (operations, operations._MESHES),
        "http": (http_server, http_server._MESH_REGISTRY),
        "mcp": (mcp_server, mcp_server._MESH_INSTANCES),
    }[request.param]
    closed = []
    handle = Registry(request.param, module, entries, tmp_path / "mesh", closed)
    handle.evict()
    monkeypatch.setattr(http_server, "_MESH_REGISTRY_MAX", 4)
    monkeypatch.setattr(mcp_server, "_MESH_INSTANCES_MAX", 4)

    def factory(namespace, agent_id, *, create=False, trusted_local=False):
        assert trusted_local is False
        root = audit.get_mesh_dir().resolve()
        path = root / f"mesh_{namespace}.db"
        if create:
            root.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"temporary registry test path")
        elif not path.exists():
            raise NamespaceNotFoundError("isolated missing namespace")
        return FakeMesh(namespace, agent_id, path, create=create, closed=closed)

    monkeypatch.setattr(module, "Mesh", factory)
    yield handle
    handle.evict()
    gc.collect()


def test_slow_cached_path_keeps_another_root_with_same_namespace_available(registry, tmp_path):
    entered, release, ready_done = threading.Event(), threading.Event(), threading.Event()
    slow = registry.seed()
    slow.store.db_path = ControlledPath(slow.store.db_path, entered=entered, release=release)
    other_root = tmp_path / "other-root"
    ready = registry.seed(root=other_root)

    def ready_lookup():
        result = registry.lookup(root=other_root)
        ready_done.set()
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        slow_lookup = pool.submit(registry.lookup)
        try:
            assert entered.wait(2)
            fast_lookup = pool.submit(ready_lookup)
            progressed = ready_done.wait(1)
        finally:
            release.set()
        assert slow_lookup.result(timeout=2) is slow
        assert fast_lookup.result(timeout=2) is ready
    assert progressed, "A filesystem check serialized lookups in independent storage roots."


def test_slow_constructor_keeps_another_cached_namespace_available(registry, monkeypatch):
    ready = registry.seed("ready")
    entered, release, ready_done = threading.Event(), threading.Event(), threading.Event()
    original = registry.module.Mesh

    def slow_factory(namespace, agent_id, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(namespace, agent_id, **kwargs)

    monkeypatch.setattr(registry.module, "Mesh", slow_factory)

    def ready_lookup():
        result = registry.lookup("ready")
        ready_done.set()
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        creating = pool.submit(registry.lookup, "slow", create=True)
        try:
            assert entered.wait(2)
            fast_lookup = pool.submit(ready_lookup)
            progressed = ready_done.wait(1)
        finally:
            release.set()
        assert creating.result(timeout=2).namespace == "slow"
        assert fast_lookup.result(timeout=2) is ready
    assert progressed, "Slow initialization serialized an unrelated cached namespace."


@pytest.mark.parametrize("missing", [False, True])
def test_delayed_validation_never_returns_or_invalidates_a_concurrent_replacement(registry, monkeypatch, missing):
    entered, release = threading.Event(), threading.Event()
    old = registry.seed()
    old.store.db_path = ControlledPath(old.store.db_path, entered=entered, release=release, missing=missing)
    invalidations = []
    monkeypatch.setattr(namespaces, "_evict_namespace_caches", lambda *args: invalidations.append(args))
    if registry.kind != "sdk":
        monkeypatch.setattr(registry.module, "_evict_namespace_caches", lambda *args: invalidations.append(args))
    with ThreadPoolExecutor(max_workers=1) as pool:
        checking = pool.submit(registry.lookup)
        try:
            assert entered.wait(2)
            registry.evict("facts")
            replacement = registry.seed()
        finally:
            release.set()
        assert checking.result(timeout=2) is replacement
    assert invalidations == []
    assert registry.entries[(registry.root.resolve(), "facts", "test-agent")] is replacement


def test_racing_creators_publish_one_instance_without_flushing_shared_resources(registry, monkeypatch):
    registry.seed()
    registry.evict()
    rendezvous = threading.Barrier(2)
    constructed = []
    original = registry.module.Mesh

    def racing_factory(namespace, agent_id, **kwargs):
        instance = original(namespace, agent_id, **kwargs)
        constructed.append(instance)
        rendezvous.wait(timeout=3)
        return instance

    monkeypatch.setattr(registry.module, "Mesh", racing_factory)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(registry.lookup) for _ in range(2)]
        first, second = [future.result(timeout=4) for future in pending]
    assert len(constructed) == 2 and first is second
    assert len(registry.entries) == 1 and registry.closed == []


def test_create_lookup_upgrades_a_valid_cached_read_instance(registry):
    cached = registry.seed(create=False)
    assert registry.lookup(create=True) is cached
    assert cached.store.create is True


def test_namespace_eviction_is_root_scoped_and_unscoped_eviction_clears_all(registry, tmp_path):
    registry.seed()
    other_root = tmp_path / "other-root"
    other = registry.seed(root=other_root)
    registry.evict("facts")
    assert (registry.root.resolve(), "facts", "test-agent") not in registry.entries
    assert registry.lookup(root=other_root) is other
    registry.evict()
    assert not registry.entries


def test_explicit_root_eviction_uses_the_requested_root_and_preserves_other_scopes(registry, tmp_path):
    local = registry.seed()
    other_root = tmp_path / "other-root"
    registry.seed(root=other_root)
    registry.seed("additional", root=other_root)
    registry.evict(mesh_home=other_root)
    assert registry.lookup() is local
    assert all(key[0] == registry.root.resolve() for key in registry.entries)


def test_rotating_agents_keeps_other_roots_and_namespaces_cached_and_resource_limits_bounded(registry, tmp_path):
    other_root = tmp_path / "other-root"
    other = registry.seed(root=other_root)
    unrelated = registry.seed("unrelated")
    for index in range(registry.capacity + 3):
        registry.lookup(agent_id=f"rotating-{index}", create=True)
    assert len(registry.entries) <= registry.capacity
    same_store = [key for key in registry.entries if key[:2] == (registry.root.resolve(), "facts")]
    assert len(same_store) <= registry.capacity // 2
    assert registry.lookup(root=other_root) is other
    assert registry.lookup("unrelated") is unrelated
    assert registry.closed == []


def test_eviction_runs_last_reference_finalization_after_releasing_registry_lock(registry):
    observed = []

    def try_registry_lock():
        acquired = registry.lock.acquire(blocking=False)
        if acquired:
            registry.lock.release()
        return acquired

    class FinalizationProbe:
        namespace = "facts"
        store = SimpleNamespace(db_path=registry.root / "mesh_facts.db")

        def __del__(self):
            with ThreadPoolExecutor(max_workers=1) as pool:
                observed.append(pool.submit(try_registry_lock).result(timeout=2))

    with registry.lock:
        registry.entries[(registry.root.resolve(), "facts", "test-agent")] = FinalizationProbe()
        if registry.kind == "mcp":
            registry.module._mesh = next(iter(registry.entries.values()))
    registry.evict("facts")
    gc.collect()
    assert observed == [True]


@pytest.mark.parametrize("binding", ["namespace", "agent", "root"])
def test_misbound_cached_instance_is_replaced_without_invalidating_foreign_store(registry, monkeypatch, tmp_path, binding):
    cached = registry.seed()
    if binding == "namespace":
        cached.namespace = cached.store.namespace = "foreign"
    elif binding == "agent":
        cached.agent_id = "foreign-agent"
    else:
        foreign = registry.seed(root=tmp_path / "foreign-root")
        cached.store.db_path = foreign.store.db_path
    invalidations = []
    monkeypatch.setattr(namespaces, "_evict_namespace_caches", lambda *args: invalidations.append(args))
    if registry.kind != "sdk":
        monkeypatch.setattr(registry.module, "_evict_namespace_caches", lambda *args: invalidations.append(args))
    replacement = registry.lookup(create=True)
    assert replacement is not cached
    assert replacement.namespace == "facts" and replacement.agent_id == "test-agent"
    assert replacement.store.db_path.parent == registry.root.resolve()
    assert invalidations == []

"""Optional group commits preserve authority, atomic receipts, fairness and bounded leases."""
from __future__ import annotations

import contextvars
import json
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from mesh.group_commit import (
    CommitOutcomeUnknown,
    GroupAdmissionRejected,
    GroupCommitBinding,
    GroupCommitCoordinator,
    GroupCommitLimits,
)


class _Database:
    def __init__(self):
        self.rows = {}
        self.commits = []
        self.depth = 0
        self.lock = threading.RLock()
        self.fail_commit = False


class _Store:
    def __init__(self, root, namespace="group-test", database=None):
        self.namespace = namespace
        self.db_path = root / f"mesh_{namespace}.db"
        self.database = database or _Database()

    @contextmanager
    def write_transaction(self):
        database = self.database
        with database.lock:
            snapshot = dict(database.rows)
            outer = database.depth == 0
            database.depth += 1
            try:
                yield
                if outer:
                    if database.fail_commit:
                        raise OSError("Synthetic commit failure")
                    database.commits.append(dict(database.rows))
            except BaseException:
                database.rows = snapshot
                raise
            finally:
                database.depth -= 1


def _binding(operation_id, *, namespace="group-test", agent="agent-a", producer="local-user"):
    return GroupCommitBinding(namespace, producer, agent, operation_id, "mesh_learn", "a" * 64)


def _limits(**overrides):
    return replace(GroupCommitLimits(), coalesce_seconds=0, **overrides)


def _single_limits(**overrides):
    return _limits(max_pending=1, max_pending_per_producer=1, max_pending_per_agent=1,
                   max_batch_commands=1, **overrides)


def _queue(coordinator, store, operation_id, *, agent="agent-a", producer="local-user",
           byte_cost=64, write_units=1, callback=None):
    ticket = coordinator.reserve(_binding(operation_id, namespace=store.namespace, agent=agent, producer=producer),
                                 byte_cost=byte_cost, write_units=write_units)
    assert ticket.begin_preparation()
    if callback is None:
        def callback():
            store.database.rows[operation_id] = operation_id
            return operation_id
    ticket.prepared(store, callback)
    return ticket


def test_producer_binding_and_ticket_identity_cannot_be_relaxed(tmp_path):
    with pytest.raises(ValueError, match="identities must match"):
        _binding("one", agent="bob", producer="principal:alice")
    coordinator = GroupCommitCoordinator(tmp_path, autostart=False)
    ticket = coordinator.reserve(_binding("one"), byte_cost=64)
    try:
        for name, value in (("binding", _binding("other")), ("byte_cost", 1), ("write_units", 99)):
            with pytest.raises(AttributeError):
                setattr(ticket, name, value)
        for name, value in (("data_root", tmp_path / "other"), ("limits", GroupCommitLimits())):
            with pytest.raises(AttributeError):
                setattr(coordinator, name, value)
    finally:
        assert coordinator.close()


@pytest.mark.parametrize("overrides", [
    {"max_pending": True}, {"max_pending_bytes": 0}, {"coalesce_seconds": float("nan")},
    {"coalesce_seconds": 0.021}, {"max_pending_per_producer": 129},
])
def test_unbounded_or_nonfinite_configuration_is_rejected(overrides):
    with pytest.raises(ValueError):
        replace(GroupCommitLimits(), **overrides)


def test_falsy_unvalidated_configuration_is_not_silently_replaced(tmp_path):
    with pytest.raises(ValueError):
        GroupCommitCoordinator(tmp_path, limits={})
    with pytest.raises(ValueError, match="absolute"):
        GroupCommitCoordinator(Path("relative-fixture"))


def test_cancelled_preparation_retains_capacity_until_its_buffers_are_released(tmp_path):
    limits = _single_limits(max_pending_bytes=100, max_pending_bytes_per_producer=100,
                            max_pending_bytes_per_agent=100, max_batch_bytes=100, max_command_bytes=100)
    coordinator = GroupCommitCoordinator(tmp_path, limits=limits, autostart=False)
    ticket = coordinator.reserve(_binding("one"), byte_cost=90)
    assert ticket.begin_preparation() and ticket.future.cancel()
    try:
        assert coordinator.stats()["pending"] == coordinator.stats()["preparing"] == 1
        assert coordinator.stats()["pending_bytes"] == 90
        with pytest.raises(GroupAdmissionRejected) as rejected:
            coordinator.reserve(_binding("two"), byte_cost=10)
        assert rejected.value.reason == "pending_limit"
        invoked = []
        ticket.prepared(_Store(tmp_path), lambda: invoked.append(True))
        assert not invoked and coordinator.stats()["pending_bytes"] == 0
        replacement = coordinator.reserve(_binding("two"), byte_cost=10)
        replacement.fail_preparation(ValueError("Synthetic invalid memory"))
        with pytest.raises(ValueError):
            replacement.future.result(timeout=1)
        assert coordinator.stats()["pending"] == 0
    finally:
        assert coordinator.close()


def test_rotating_agent_labels_cannot_bypass_per_producer_admission(tmp_path):
    coordinator = GroupCommitCoordinator(tmp_path, limits=_limits(max_pending_per_producer=2, max_pending_per_agent=1),
                                        autostart=False)
    try:
        coordinator.reserve(_binding("one", agent="agent-a"), byte_cost=64)
        coordinator.reserve(_binding("two", agent="agent-b"), byte_cost=64)
        with pytest.raises(GroupAdmissionRejected) as rejected:
            coordinator.reserve(_binding("three", agent="agent-c"), byte_cost=64)
        assert rejected.value.reason == "producer_limit" and rejected.value.admitted is False
        other = coordinator.reserve(_binding("other", agent="bob", producer="principal:bob"), byte_cost=64)
        assert other.state == "reserved"
    finally:
        assert coordinator.close()


def test_pending_byte_limit_applies_across_producers_and_namespaces(tmp_path):
    limits = _limits(max_pending_bytes=100, max_pending_bytes_per_producer=100,
                     max_pending_bytes_per_agent=100, max_batch_bytes=100, max_command_bytes=100)
    coordinator = GroupCommitCoordinator(tmp_path, limits=limits, autostart=False)
    try:
        coordinator.reserve(_binding("one"), byte_cost=70)
        coordinator.reserve(_binding("two", namespace="other", agent="bob", producer="principal:bob"), byte_cost=30)
        with pytest.raises(GroupAdmissionRejected) as rejected:
            coordinator.reserve(_binding("three", agent="eve", producer="principal:eve"), byte_cost=1)
        assert rejected.value.reason == "pending_bytes"
        assert coordinator.stats()["max_observed_bytes"] == 100
    finally:
        assert coordinator.close()


def test_namespace_admission_capacity_is_released_after_cancellation(tmp_path):
    coordinator = GroupCommitCoordinator(tmp_path, limits=_limits(max_namespaces=1), autostart=False)
    try:
        first = coordinator.reserve(_binding("one"), byte_cost=64)
        with pytest.raises(GroupAdmissionRejected) as rejected:
            coordinator.reserve(_binding("two", namespace="other"), byte_cost=64)
        assert rejected.value.reason == "namespace_limit"
        assert first.future.cancel()
        assert coordinator.reserve(_binding("two", namespace="other"), byte_cost=64).state == "reserved"
    finally:
        assert coordinator.close()


def test_store_root_and_namespace_binding_are_checked_before_enqueue(tmp_path):
    coordinator = GroupCommitCoordinator(tmp_path, autostart=False)
    ticket = coordinator.reserve(_binding("one"), byte_cost=64)
    assert ticket.begin_preparation()
    try:
        for store in (_Store(tmp_path / "different-root"), _Store(tmp_path, "different-namespace")):
            with pytest.raises(ValueError, match="approved data root"):
                ticket.prepared(store, lambda: None)
        ticket.fail_preparation(ValueError("Synthetic binding mismatch"))
        assert coordinator.stats()["queued"] == 0
    finally:
        assert coordinator.close()


def test_ready_agents_are_served_in_round_robin_order(tmp_path):
    coordinator = GroupCommitCoordinator(tmp_path, limits=_limits(max_batch_commands=1), autostart=False)
    store, order = _Store(tmp_path), []
    tickets = []
    for agent, identifiers in (("agent-a", ["a0", "a1", "a2"]), ("agent-b", ["b0", "b1"])):
        for operation_id in identifiers:
            tickets.append(_queue(coordinator, store, operation_id, agent=agent,
                                  callback=lambda value=operation_id: order.append(value) or value))
    coordinator.start()
    try:
        assert {ticket.future.result(timeout=3) for ticket in tickets} == {"a0", "a1", "a2", "b0", "b1"}
        assert order == ["a0", "b0", "a1", "b1", "a2"]
    finally:
        assert coordinator.close()


def test_namespaces_are_fair_and_never_share_a_sql_transaction(tmp_path):
    coordinator = GroupCommitCoordinator(tmp_path, limits=_limits(max_batch_commands=2), autostart=False)
    first, second, order = _Store(tmp_path), _Store(tmp_path, "other"), []
    tickets = []
    for store, identifiers in ((first, ["a0", "a1", "a2"]), (second, ["b0", "b1"])):
        for operation_id in identifiers:
            def callback(value=operation_id, target=store):
                order.append(value)
                target.database.rows[value] = value
                return value
            tickets.append(_queue(coordinator, store, operation_id, callback=callback))
    coordinator.start()
    try:
        for ticket in tickets:
            ticket.future.result(timeout=3)
        assert order == ["a0", "a1", "b0", "b1", "a2"]
        assert [len(rows) for rows in first.database.commits] == [2, 3]
        assert [len(rows) for rows in second.database.commits] == [2]
    finally:
        assert coordinator.close()


def test_one_failed_command_rolls_back_its_savepoint_and_never_replays(tmp_path):
    coordinator = GroupCommitCoordinator(tmp_path, limits=_limits(), autostart=False)
    store, calls = _Store(tmp_path), []
    first = _queue(coordinator, store, "one")
    def fail():
        calls.append("two")
        store.database.rows["two"] = "uncommitted"
        raise ValueError("Synthetic permission change")
    failed = _queue(coordinator, store, "two", callback=fail)
    third = _queue(coordinator, store, "three")
    coordinator.start()
    try:
        assert first.future.result(timeout=3) == "one" and third.future.result(timeout=3) == "three"
        with pytest.raises(ValueError):
            failed.future.result(timeout=3)
        assert store.database.rows == {"one": "one", "three": "three"}
        assert calls == ["two"] and len(store.database.commits) == 1
    finally:
        assert coordinator.close()


def test_outer_commit_failure_keeps_every_operation_outcome_unknown(tmp_path):
    coordinator = GroupCommitCoordinator(tmp_path, limits=_limits(), autostart=False)
    store = _Store(tmp_path)
    store.database.fail_commit = True
    tickets = [_queue(coordinator, store, str(index)) for index in range(3)]
    coordinator.start()
    try:
        for ticket in tickets:
            with pytest.raises(CommitOutcomeUnknown) as unknown:
                ticket.future.result(timeout=3)
            assert unknown.value.binding == ticket.binding and unknown.value.outcome == "unknown"
        assert not store.database.rows and coordinator.stats()["failed_batches"] == 1
        assert coordinator.stats()["pending_bytes"] == 0
    finally:
        assert coordinator.close()


def test_each_command_uses_its_approved_context_and_future_publishes_after_commit(tmp_path):
    flag = contextvars.ContextVar("test_group_privacy", default="worker-default")
    coordinator = GroupCommitCoordinator(tmp_path, limits=_limits(), autostart=False)
    store, observed, commit_observations, published = _Store(tmp_path), [], [], threading.Event()
    tickets = []
    for index, policy in enumerate((False, True)):
        token = flag.set(policy)
        try:
            ticket = _queue(coordinator, store, str(index), callback=lambda: observed.append(flag.get()) or flag.get())
        finally:
            flag.reset(token)
        def done(future):
            commit_observations.append(len(store.database.commits))
            if len(commit_observations) == 2:
                published.set()
        ticket.future.add_done_callback(done)
        tickets.append(ticket)
    coordinator.start()
    try:
        assert [ticket.future.result(timeout=3) for ticket in tickets] == [False, True]
        assert published.wait(timeout=3)
        assert observed == [False, True] and commit_observations == [1, 1]
        assert flag.get() == "worker-default"
    finally:
        assert coordinator.close()


def test_running_cancellation_and_shutdown_do_not_release_inflight_capacity(tmp_path):
    coordinator = GroupCommitCoordinator(tmp_path, limits=_single_limits())
    store, entered, release = _Store(tmp_path), threading.Event(), threading.Event()
    def callback():
        entered.set()
        assert release.wait(timeout=3)
        store.database.rows["one"] = "one"
        return "one"
    ticket = _queue(coordinator, store, "one", callback=callback)
    try:
        assert entered.wait(timeout=3) and ticket.future.running()
        assert not ticket.future.cancel()
        assert not coordinator.close(timeout=0)
        assert coordinator.stats()["running"] == 1 and coordinator.stats()["pending_bytes"] == 64
    finally:
        release.set()
        assert ticket.future.result(timeout=3) == "one"
        assert coordinator.close()


def test_cancel_close_wins_before_a_waiting_batch_can_start(tmp_path, monkeypatch):
    # Collection may retain this class after its module was reloaded. Freeze
    # the implementation clock so delayed startup still enters coalescing;
    # Event and Condition waits keep their real clocks.
    monkeypatch.setitem(GroupCommitCoordinator._next_batch.__globals__, "time",
                        SimpleNamespace(monotonic=lambda: 100.0))
    coordinator = GroupCommitCoordinator(tmp_path, limits=replace(GroupCommitLimits(), coalesce_seconds=0.020),
                                        autostart=False)
    store, entered, invoked = _Store(tmp_path), threading.Event(), []
    original = coordinator._condition.wait
    def controlled_wait(timeout=None):
        if timeout is not None:
            entered.set()
            # Await close's notification, making the race deterministic rather
            # than depending on scheduling within a 20 ms coalescing window.
            return original()
        return original(timeout)
    monkeypatch.setattr(coordinator._condition, "wait", controlled_wait)
    ticket = _queue(coordinator, store, "one", callback=lambda: invoked.append(True))
    coordinator.start()
    try:
        assert entered.wait(timeout=3)
        assert coordinator.close()
        assert ticket.future.cancelled() and not invoked
    finally:
        assert coordinator.close()


def test_future_callbacks_can_submit_without_deadlocking_the_writer(tmp_path):
    coordinator = GroupCommitCoordinator(tmp_path, limits=_single_limits(), autostart=False)
    store, submitted, following = _Store(tmp_path), threading.Event(), []
    first = _queue(coordinator, store, "one")
    def done(future):
        following.append(_queue(coordinator, store, "two"))
        submitted.set()
    first.future.add_done_callback(done)
    coordinator.start()
    try:
        assert first.future.result(timeout=3) == "one" and submitted.wait(timeout=3)
        assert following[0].future.result(timeout=3) == "two"
    finally:
        assert coordinator.close()


def test_atomic_write_units_are_not_split_or_exceeded(tmp_path):
    coordinator = GroupCommitCoordinator(tmp_path, limits=_limits(max_batch_units=3), autostart=False)
    store = _Store(tmp_path)
    atomic = _queue(coordinator, store, "atomic", write_units=3)
    scalar = _queue(coordinator, store, "scalar", agent="agent-b")
    coordinator.start()
    try:
        assert atomic.future.result(timeout=3) == "atomic" and scalar.future.result(timeout=3) == "scalar"
        assert [len(rows) for rows in store.database.commits] == [1, 2]
    finally:
        assert coordinator.close()


def test_batch_bytes_skip_an_indivisible_head_without_exceeding_the_budget(tmp_path):
    coordinator = GroupCommitCoordinator(tmp_path, limits=_limits(max_batch_bytes=100, max_command_bytes=100),
                                        autostart=False)
    store, order = _Store(tmp_path), []
    tickets = []
    for identifier, cost, agent in (("large", 80, "agent-a"), ("small", 20, "agent-a"), ("other", 40, "agent-b")):
        def callback(value=identifier):
            order.append(value)
            store.database.rows[value] = value
            return value
        tickets.append(_queue(coordinator, store, identifier, byte_cost=cost, agent=agent, callback=callback))
    coordinator.start()
    try:
        for ticket in tickets:
            ticket.future.result(timeout=3)
        assert order == ["large", "small", "other"]
        assert [set(rows) for rows in store.database.commits] == [{"large", "small"}, {"large", "small", "other"}]
    finally:
        assert coordinator.close()


def test_graceful_close_drains_an_admitted_preparation_and_rejects_new_admission(tmp_path):
    coordinator = GroupCommitCoordinator(tmp_path, limits=_limits())
    store = _Store(tmp_path)
    ticket = coordinator.reserve(_binding("one"), byte_cost=64)
    assert ticket.begin_preparation()
    try:
        assert not coordinator.close(cancel_pending=False, timeout=0)
        with pytest.raises(GroupAdmissionRejected, match="closed"):
            coordinator.reserve(_binding("two"), byte_cost=64)
        ticket.prepared(store, lambda: "one")
        assert ticket.future.result(timeout=3) == "one"
        assert coordinator.close(cancel_pending=False)
    finally:
        assert coordinator.close()


def test_fatal_callback_failure_does_not_strand_running_or_queued_callers(tmp_path):
    coordinator = GroupCommitCoordinator(tmp_path, limits=_limits(max_batch_commands=1), autostart=False)
    store, invoked, settled = _Store(tmp_path), [], threading.Event()
    def die():
        raise SystemExit("Synthetic fatal worker failure")
    first = _queue(coordinator, store, "one", callback=die)
    second = _queue(coordinator, store, "two", callback=lambda: invoked.append(True))
    second.future.add_done_callback(lambda future: settled.set())
    coordinator.start()
    try:
        with pytest.raises(CommitOutcomeUnknown):
            first.future.result(timeout=3)
        assert settled.wait(timeout=3)
        assert second.future.cancelled() and not invoked
        assert coordinator.close()
        assert coordinator.stats()["pending"] == 0 and not coordinator.stats()["worker_alive"]
    finally:
        assert coordinator.close()


def _isolated_policy(monkeypatch, namespace):
    for name, value in {
        "MESH_NAMESPACE": namespace, "MESH_LOCKED_NAMESPACE": namespace, "MESH_LOCK_NAMESPACE": namespace,
        "MESH_FORCE_HASH_EMBED": "1", "MESH_REQUIRE_MODEL": "0", "MESH_QUERY_MODE": "hash",
        "MESH_NO_AUTO_DAEMON": "1", "MESH_NO_DAEMON_PROXY": "1", "MESH_NO_MODEL_DOWNLOAD": "1",
        "MESH_NO_UPDATE_CHECK": "1", "MESH_ENABLE_AUTO_SYNC": "0", "MESH_VAULT_SECRETS": "0",
        "MESH_MCP_INCLUDE_PRIVATE": "0", "MESH_READ_ONLY": "0", "MESH_ENFORCE_AGENT_OWNERSHIP": "1",
        "MESH_ENCRYPT_AT_REST": "0",
    }.items():
        monkeypatch.setenv(name, value)
    for name in ("MESH_ENCRYPTION_KEY", "MESH_PASSPHRASE", "MESH_AUTHENTICATED_AGENT_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MESH_AGENT_SCOPES", json.dumps({"*": {"namespaces": [namespace], "mode": "rw"}}))


def _receipt_callback(mesh, binding, prepared, *, many=False):
    def callback():
        commit = mesh.commit_prepared_learn_many if many else mesh.commit_prepared_learn
        return mesh.store.execute_operation(
            binding.operation_id, binding.producer_scope, binding.operation,
            binding.parameter_digest, lambda: commit(prepared),
        )
    return callback


def test_real_mesh_group_commits_atomic_batch_and_scalar_receipts_once(tmp_path, monkeypatch):
    from mesh import store as store_module
    from mesh.memory import Mesh
    from mesh.audit import mesh_dir_context
    from mesh.operation_contract import parameter_digest
    from mesh.sqlite_runtime import is_patched_sqlite

    namespace, data_root = "group-production", tmp_path / "mesh"
    _isolated_policy(monkeypatch, namespace)
    with mesh_dir_context(data_root):
        first = Mesh(namespace=namespace, agent_id="agent-a", create=True, trusted_local=False)
        second = Mesh(namespace=namespace, agent_id="agent-b", create=True, trusted_local=False)
        items = [f"Public fixture partition {index} retains reconciliation records for {20 + index} days." for index in range(3)]
        scalar = "Public fixture export relay waits 80 seconds before retrying delivery."
        first_args, second_args = {"items": items, "atomic": True, "deduplicate": False}, {"content": scalar}
        first_binding = GroupCommitBinding(namespace, "local-user", "agent-a", "batch-operation",
                                           "mesh_learn_many", parameter_digest({"arguments": first_args, "agent_id": "agent-a"}))
        second_binding = GroupCommitBinding(namespace, "local-user", "agent-b", "scalar-operation",
                                            "mesh_learn", parameter_digest({"arguments": second_args, "agent_id": "agent-b"}))
        coordinator = GroupCommitCoordinator(data_root, limits=_limits(), autostart=False)
        first_ticket = coordinator.reserve(first_binding, byte_cost=64 * 1024, write_units=3)
        second_ticket = coordinator.reserve(second_binding, byte_cost=32 * 1024)
        assert first_ticket.begin_preparation() and second_ticket.begin_preparation()
        prepared_many = first.prepare_learn_many(items, atomic=True, deduplicate=False)
        prepared_one = second.prepare_learn(scalar, deduplicate=False)
        first_ticket.prepared(first.store, _receipt_callback(first, first_binding, prepared_many, many=True))
        second_ticket.prepared(second.store, _receipt_callback(second, second_binding, prepared_one))
        original_commit, outer_commits = store_module._commit_write, []

        def record_commit(connection, marker):
            # Outbox delivery acknowledges events in a later transaction on
            # another thread, potentially reusing this same pooled connection.
            if marker is None and threading.current_thread() is coordinator._thread:
                outer_commits.append({
                    "connection": id(connection),
                    "version": connection.execute("SELECT sqlite_version()").fetchone()[0],
                    "synchronous": connection.execute("PRAGMA synchronous").fetchone()[0],
                    "journal": connection.execute("PRAGMA journal_mode").fetchone()[0],
                })
            return original_commit(connection, marker)

        monkeypatch.setattr(store_module, "_commit_write", record_commit)
        coordinator.start()
        try:
            many = first_ticket.future.result(timeout=10)
            one = second_ticket.future.result(timeout=10)
            assert coordinator.close(cancel_pending=False)
            stats = coordinator.stats()
            assert stats["committed_batches"] == 1 and stats["executed_commands"] == 2
            assert stats["failed_batches"] == 0
            assert len(outer_commits) == 1
            assert is_patched_sqlite(outer_commits[0]["version"])
            assert outer_commits[0]["synchronous"] == 2 and outer_commits[0]["journal"] == "wal"
            records = [*many["result"], one["result"]]
            ids = [record["memory_id"] for record in records]
            assert len(set(ids)) == 4 and all(record["revision"] == 1 for record in records)
            assert all(first.get(memory_id, include_private=False)["id"] == memory_id for memory_id in ids)
            with first.store._connect() as connection:
                assert connection.execute("SELECT count(*) FROM memories").fetchone()[0] == 4
                assert connection.execute("SELECT count(*) FROM operation_receipts").fetchone()[0] == 2
                assert [row[0] for row in connection.execute("PRAGMA quick_check")] == ["ok"]
                assert list(connection.execute("PRAGMA foreign_key_check")) == []
            def must_not_run():
                raise AssertionError("A recorded operation must not repeat its callback")
            for mesh, binding in ((first, first_binding), (second, second_binding)):
                replay = mesh.store.execute_operation(binding.operation_id, binding.producer_scope, binding.operation,
                                                      binding.parameter_digest, must_not_run)
                assert replay["replayed"] is True
            assert first.count() == 4
        finally:
            coordinator.close()
            first.close()
            second.close()


def test_real_mesh_rechecks_revoked_authority_inside_each_command_savepoint(tmp_path, monkeypatch):
    from mesh.memory import Mesh
    from mesh.audit import mesh_dir_context

    namespace, data_root = "group-authority", tmp_path / "mesh"
    _isolated_policy(monkeypatch, namespace)
    with mesh_dir_context(data_root):
        first = Mesh(namespace=namespace, agent_id="agent-a", create=True, trusted_local=False)
        second = Mesh(namespace=namespace, agent_id="agent-b", create=True, trusted_local=False)
        coordinator = GroupCommitCoordinator(data_root, limits=_limits(), autostart=False)
        first_binding = _binding("revoked", namespace=namespace)
        second_binding = _binding("allowed", namespace=namespace, agent="agent-b")
        first_ticket = coordinator.reserve(first_binding, byte_cost=32 * 1024)
        second_ticket = coordinator.reserve(second_binding, byte_cost=32 * 1024)
        assert first_ticket.begin_preparation() and second_ticket.begin_preparation()
        prepared_one = first.prepare_learn("Public fixture cache uses a 40 second idle expiry.", deduplicate=False)
        prepared_two = second.prepare_learn("Public fixture scheduler waits 25 seconds between checkpoints.", deduplicate=False)
        first_ticket.prepared(first.store, _receipt_callback(first, first_binding, prepared_one))
        second_ticket.prepared(second.store, _receipt_callback(second, second_binding, prepared_two))
        monkeypatch.setenv("MESH_AGENT_SCOPES", json.dumps({
            "agent-a": {"namespaces": [namespace], "mode": "ro"},
            "agent-b": {"namespaces": [namespace], "mode": "rw"},
        }))
        coordinator.start()
        try:
            with pytest.raises(ValueError, match="read-only"):
                first_ticket.future.result(timeout=10)
            accepted = second_ticket.future.result(timeout=10)
            assert accepted["result"]["status"] in {"stored", "stored_with_conflict"}
            assert second.count() == 1
            assert first.store.get_operation_receipt("revoked", "local-user") is None
            assert second.store.get_operation_receipt("allowed", "local-user") is not None
        finally:
            assert coordinator.close()
            first.close()
            second.close()


@pytest.mark.parametrize("separate_producers", [False, True])
def test_real_mesh_receipt_scope_handles_same_operation_id_without_in_memory_replay(
        tmp_path, monkeypatch, separate_producers):
    from mesh.audit import mesh_dir_context
    from mesh.memory import Mesh
    from mesh.operation_contract import parameter_digest

    namespace, data_root = "group-receipts", tmp_path / "mesh"
    _isolated_policy(monkeypatch, namespace)
    with mesh_dir_context(data_root):
        coordinator = GroupCommitCoordinator(data_root, limits=_limits(), autostart=False)
        content = "Public fixture export relay uses a 60 second delivery timeout."
        meshes, tickets, actual_commits = [], [], []
        for index, actor in enumerate(("alice", "bob" if separate_producers else "alice")):
            mesh = Mesh(namespace=namespace, agent_id=actor, create=True, trusted_local=False)
            meshes.append(mesh)
            scope = "principal:" + actor if separate_producers else "local-user"
            binding = GroupCommitBinding(namespace, scope, actor, "same-operation", "mesh_learn",
                                          parameter_digest({"arguments": {"content": content}, "agent_id": actor}))
            ticket = coordinator.reserve(binding, byte_cost=32 * 1024)
            assert ticket.begin_preparation()
            prepared = mesh.prepare_learn(content, deduplicate=False)
            def callback(target=mesh, identity=binding, operation=prepared, number=index):
                def commit():
                    actual_commits.append(number)
                    return target.commit_prepared_learn(operation)
                return target.store.execute_operation(identity.operation_id, identity.producer_scope,
                                                       identity.operation, identity.parameter_digest, commit)
            ticket.prepared(mesh.store, callback)
            tickets.append(ticket)
        coordinator.start()
        try:
            results = [ticket.future.result(timeout=10) for ticket in tickets]
            assert coordinator.close(cancel_pending=False)
            assert results[0]["replayed"] is False
            assert results[1]["replayed"] is (not separate_producers)
            expected = 2 if separate_producers else 1
            assert len(actual_commits) == expected and meshes[0].count() == expected
            with meshes[0].store._connect() as connection:
                assert connection.execute("SELECT count(*) FROM operation_receipts").fetchone()[0] == expected
            ids = {result["result"]["memory_id"] for result in results}
            assert len(ids) == expected
        finally:
            coordinator.close()
            for mesh in meshes:
                mesh.close()

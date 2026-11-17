"""Bounded optional daemon commits for already-authorized prepared mutations.

The operation adapter owns profile binding, side-effect-free preparation,
point-of-commit authorization and receipt execution. This module owns admission,
fair scheduling, cancellation and publication after the shared durable commit.
It never retries an admitted operation or starts a daemon implicitly.
"""
from __future__ import annotations

import contextvars
import math
import re
import threading
import time
import weakref
from collections import Counter, OrderedDict, deque
from concurrent.futures import Future, InvalidStateError
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

_ELIGIBLE = frozenset({"mesh_learn", "mesh_learn_many", "mesh_update"})
_MIB = 1024 * 1024


@dataclass(frozen=True)
class GroupCommitBinding:
    """Identity taken from a reviewed profile and server-approved operation."""

    namespace: str
    producer_scope: str
    agent_id: str
    operation_id: str
    operation: str
    parameter_digest: str

    def __post_init__(self) -> None:
        from .namespaces import validate_namespace
        from .operation_contract import validate_operation_id, validate_producer_scope

        object.__setattr__(self, "namespace", validate_namespace(self.namespace))
        validate_producer_scope(self.producer_scope)
        validate_operation_id(self.operation_id)
        if (
            not isinstance(self.agent_id, str) or not 1 <= len(self.agent_id) <= 64
            or any(ord(char) < 32 or ord(char) == 127 for char in self.agent_id)
        ):
            raise ValueError("Invalid reviewed agent identity.")
        if self.producer_scope.startswith("principal:") and self.producer_scope != "principal:" + self.agent_id:
            raise ValueError("Authenticated producer and reviewed agent identities must match.")
        if not isinstance(self.operation, str) or self.operation not in _ELIGIBLE:
            raise ValueError("Only prepared learn, learn_many and update operations are eligible.")
        if not isinstance(self.parameter_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", self.parameter_digest):
            raise ValueError("A canonical operation parameter digest is required.")


@dataclass(frozen=True)
class GroupCommitLimits:
    max_pending: int = 128
    max_pending_bytes: int = 16 * _MIB
    max_pending_per_producer: int = 32
    max_pending_bytes_per_producer: int = 8 * _MIB
    max_pending_per_agent: int = 8
    max_pending_bytes_per_agent: int = 4 * _MIB
    max_namespaces: int = 32
    max_batch_commands: int = 32
    max_batch_units: int = 128
    max_batch_bytes: int = 4 * _MIB
    max_command_bytes: int = 4 * _MIB
    coalesce_seconds: float = 0.002

    def __post_init__(self) -> None:
        for name in (
            "max_pending", "max_pending_per_producer", "max_pending_per_agent", "max_namespaces",
            "max_batch_commands", "max_batch_units",
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 4096:
                raise ValueError("Commit count limits must be positive bounded integers.")
        for name in (
            "max_pending_bytes", "max_pending_bytes_per_producer", "max_pending_bytes_per_agent",
            "max_batch_bytes", "max_command_bytes",
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 256 * _MIB:
                raise ValueError("Commit byte limits must be positive bounded integers.")
        if not self.max_pending_per_agent <= self.max_pending_per_producer <= self.max_pending:
            raise ValueError("Agent and producer limits cannot exceed the global pending limit.")
        if not self.max_pending_bytes_per_agent <= self.max_pending_bytes_per_producer <= self.max_pending_bytes:
            raise ValueError("Agent and producer byte limits cannot exceed the global byte limit.")
        if not self.max_command_bytes <= self.max_batch_bytes <= self.max_pending_bytes:
            raise ValueError("Command and batch bytes cannot exceed the global byte limit.")
        if self.max_batch_commands > self.max_pending:
            raise ValueError("A batch cannot exceed pending admission capacity.")
        if (
            type(self.coalesce_seconds) not in (int, float)
            or not math.isfinite(self.coalesce_seconds) or not 0 <= self.coalesce_seconds <= 0.020
        ):
            raise ValueError("The coalescing window must be between zero and 20 milliseconds.")


class GroupAdmissionRejected(RuntimeError):
    """No ticket was admitted and no guarded mutation was started."""

    admitted = False

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__("Group commit admission rejected: " + reason)


class CommitOutcomeUnknown(RuntimeError):
    """An admitted batch failed; the stable receipt identity must be preserved."""

    outcome = "unknown"

    def __init__(self, binding: GroupCommitBinding, failure_type: str):
        self.binding = binding
        self.failure_type = failure_type
        super().__init__("Group commit outcome is unknown; inspect the stable operation receipt.")


class CommitTicket:
    """A capacity lease retained until preparation and commit have actually settled."""

    def __init__(self, coordinator: GroupCommitCoordinator, number: int,
                 binding: GroupCommitBinding, byte_cost: int, write_units: int):
        self._coordinator = coordinator
        self._number = number
        self._binding = binding
        self._byte_cost = byte_cost
        self._write_units = write_units
        self._future: Future[Any] = Future()
        self._state = "reserved"
        self._context: contextvars.Context | None = None
        self._store: Any = None
        self._callback: Callable[[], Any] | None = None
        self._ready_at = 0.0

    @property
    def binding(self) -> GroupCommitBinding:
        return self._binding

    @property
    def byte_cost(self) -> int:
        return self._byte_cost

    @property
    def write_units(self) -> int:
        return self._write_units

    @property
    def future(self) -> Future[Any]:
        return self._future

    @property
    def state(self) -> str:
        with self._coordinator._condition:
            return self._state

    def begin_preparation(self) -> bool:
        """Capture approved caller context before bounded preparation starts."""
        return self._coordinator._begin_preparation(self)

    def prepared(self, store: Any, callback: Callable[[], Any]) -> Future[Any]:
        """Queue a callback that repeats authorization and executes its receipt."""
        self._coordinator._prepared(self, store, callback)
        return self.future

    def fail_preparation(self, error: BaseException) -> None:
        """Release a preparation lease even if its caller already cancelled."""
        self._coordinator._fail_preparation(self, error)


class GroupCommitCoordinator:
    """One bounded writer for an explicit approved data root, with fair ready queues."""

    def __init__(self, data_root: Path | str, *, limits: GroupCommitLimits | None = None,
                 autostart: bool = True):
        requested_root = Path(data_root).expanduser()
        if not requested_root.is_absolute():
            raise ValueError("Group commits require an explicit absolute approved data root.")
        self._data_root = requested_root.resolve()
        self._limits = GroupCommitLimits() if limits is None else limits
        if not isinstance(self.limits, GroupCommitLimits) or type(autostart) is not bool:
            raise ValueError("A validated limit configuration and boolean startup mode are required.")
        self._condition = threading.Condition(threading.RLock())
        self._tickets: dict[int, CommitTicket] = {}
        self._ready: OrderedDict[str, OrderedDict[tuple[str, str], deque[CommitTicket]]] = OrderedDict()
        self._namespace_pending: Counter[str] = Counter()
        self._producer_pending: Counter[str] = Counter()
        self._producer_bytes: Counter[str] = Counter()
        self._agent_pending: Counter[tuple[str, str]] = Counter()
        self._agent_bytes: Counter[tuple[str, str]] = Counter()
        self._pending_bytes = self._number = 0
        self._max_observed_pending = self._max_observed_bytes = 0
        self._rejected: Counter[str] = Counter()
        self._committed_batches = self._failed_batches = self._executed_commands = 0
        self._cancelled_before_commit = self._preparation_failures = 0
        self._closing = False
        self._cancel_on_close = False
        self._thread: threading.Thread | None = None
        if autostart:
            self.start()

    @property
    def data_root(self) -> Path:
        return self._data_root

    @property
    def limits(self) -> GroupCommitLimits:
        return self._limits

    def start(self) -> None:
        with self._condition:
            if self._closing:
                raise GroupAdmissionRejected("closed")
            if self._thread is not None:
                return
            # Portable across Python versions and thread-context inheritance
            # modes: the worker never inherits one request's privileges.
            self._thread = threading.Thread(
                target=lambda: contextvars.Context().run(self._run), name="mesh-group-commit", daemon=True,
            )
            self._thread.start()

    def reserve(self, binding: GroupCommitBinding, *, byte_cost: int, write_units: int = 1) -> CommitTicket:
        """Fail fast before preparation when count, bytes or producer capacity is full.

        The adapter computes byte_cost conservatively from validated request data
        plus prepared buffers. It must never accept a model-supplied cost.
        """
        if not isinstance(binding, GroupCommitBinding):
            raise ValueError("A reviewed group commit binding is required.")
        if type(byte_cost) is not int or byte_cost <= 0 or type(write_units) is not int or write_units <= 0:
            raise ValueError("Positive integer byte and write-unit costs are required.")
        with self._condition:
            reason = self._admission_reason(binding, byte_cost, write_units)
            if reason:
                self._rejected[reason] += 1
                raise GroupAdmissionRejected(reason)
            number = self._number
            self._number += 1
            ticket = CommitTicket(self, number, binding, byte_cost, write_units)
            self._tickets[number] = ticket
            self._pending_bytes += byte_cost
            owner = (binding.producer_scope, binding.agent_id)
            self._namespace_pending[binding.namespace] += 1
            self._producer_pending[binding.producer_scope] += 1
            self._producer_bytes[binding.producer_scope] += byte_cost
            self._agent_pending[owner] += 1
            self._agent_bytes[owner] += byte_cost
            self._max_observed_pending = max(self._max_observed_pending, len(self._tickets))
            self._max_observed_bytes = max(self._max_observed_bytes, self._pending_bytes)
            weak_coordinator, weak_ticket = weakref.ref(self), weakref.ref(ticket)

            def done(future: Future[Any]) -> None:
                coordinator, current = weak_coordinator(), weak_ticket()
                if coordinator is not None and current is not None:
                    coordinator._on_done(current, future)

            ticket.future.add_done_callback(done)
            return ticket

    def _admission_reason(self, binding: GroupCommitBinding, byte_cost: int, units: int) -> str | None:
        limits = self.limits
        owner = (binding.producer_scope, binding.agent_id)
        if self._closing:
            return "closed"
        if byte_cost > limits.max_command_bytes:
            return "command_bytes"
        if units > limits.max_batch_units:
            return "command_units"
        if binding.namespace not in self._namespace_pending and len(self._namespace_pending) >= limits.max_namespaces:
            return "namespace_limit"
        if len(self._tickets) >= limits.max_pending:
            return "pending_limit"
        if self._pending_bytes + byte_cost > limits.max_pending_bytes:
            return "pending_bytes"
        if self._producer_pending[binding.producer_scope] >= limits.max_pending_per_producer:
            return "producer_limit"
        if self._producer_bytes[binding.producer_scope] + byte_cost > limits.max_pending_bytes_per_producer:
            return "producer_bytes"
        if self._agent_pending[owner] >= limits.max_pending_per_agent:
            return "agent_limit"
        if self._agent_bytes[owner] + byte_cost > limits.max_pending_bytes_per_agent:
            return "agent_bytes"
        return None

    def _begin_preparation(self, ticket: CommitTicket) -> bool:
        cancel = False
        with self._condition:
            if ticket._state == "cancelled" or ticket.future.done():
                return False
            if ticket._state != "reserved":
                raise ValueError("Preparation can begin only once on a reserved ticket.")
            if self._closing and self._cancel_on_close:
                self._cancelled_before_commit += 1
                self._release(ticket, "cancelled")
                cancel = True
            else:
                ticket._context = contextvars.copy_context()
                ticket._state = "preparing"
        if cancel:
            ticket.future.cancel()
        return not cancel

    def _check_store(self, ticket: CommitTicket, store: Any) -> None:
        path = getattr(store, "db_path", None)
        if (
            not isinstance(path, Path) or path.resolve() != self.data_root / f"mesh_{ticket.binding.namespace}.db"
            or getattr(store, "namespace", None) != ticket.binding.namespace
            or not callable(getattr(store, "write_transaction", None))
        ):
            raise ValueError("Prepared store does not match the approved data root and namespace.")

    def _prepared(self, ticket: CommitTicket, store: Any, callback: Callable[[], Any]) -> None:
        if not callable(callback):
            raise ValueError("A prepared mutation callback is required.")
        self._check_store(ticket, store)
        cancel = False
        with self._condition:
            if ticket._state not in {"preparing", "cancelled_preparing"}:
                raise ValueError("Only an active preparation can supply a callback.")
            if ticket.future.done() or (self._closing and self._cancel_on_close):
                if ticket._state == "preparing":
                    self._cancelled_before_commit += 1
                self._release(ticket, "cancelled")
                cancel = not ticket.future.done()
            else:
                ticket._store, ticket._callback = store, callback
                ticket._ready_at = time.monotonic()
                ticket._state = "queued"
                owners = self._ready.setdefault(ticket.binding.namespace, OrderedDict())
                owner = (ticket.binding.producer_scope, ticket.binding.agent_id)
                owners.setdefault(owner, deque()).append(ticket)
                self._condition.notify_all()
        if cancel:
            ticket.future.cancel()

    def _fail_preparation(self, ticket: CommitTicket, error: BaseException) -> None:
        if not isinstance(error, BaseException):
            raise ValueError("Preparation failure must be an exception.")
        with self._condition:
            if ticket._state not in {"reserved", "preparing", "cancelled_preparing"}:
                raise ValueError("Only an uncommitted preparation can fail.")
            self._preparation_failures += 1
            self._release(ticket, "cancelled" if ticket.future.cancelled() else "done")
        if not ticket.future.done():
            try:
                ticket.future.set_exception(error)
            except InvalidStateError:
                # Request cancellation can win after the lease was released.
                if not ticket.future.cancelled():
                    raise

    @staticmethod
    def _decrement(counter: Counter, key: Any, amount: int = 1) -> None:
        counter[key] -= amount
        if counter[key] == 0:
            del counter[key]

    def _release(self, ticket: CommitTicket, state: str) -> None:
        if self._tickets.pop(ticket._number, None) is None:
            return
        owner = (ticket.binding.producer_scope, ticket.binding.agent_id)
        self._decrement(self._namespace_pending, ticket.binding.namespace)
        self._decrement(self._producer_pending, ticket.binding.producer_scope)
        self._decrement(self._producer_bytes, ticket.binding.producer_scope, ticket.byte_cost)
        self._decrement(self._agent_pending, owner)
        self._decrement(self._agent_bytes, owner, ticket.byte_cost)
        self._pending_bytes -= ticket.byte_cost
        ticket._state = state
        ticket._store = ticket._callback = ticket._context = None
        self._condition.notify_all()

    def _remove_ready(self, ticket: CommitTicket) -> None:
        namespace = ticket.binding.namespace
        owner = (ticket.binding.producer_scope, ticket.binding.agent_id)
        owners = self._ready.get(namespace)
        queue = owners.get(owner) if owners else None
        if queue is not None:
            try:
                queue.remove(ticket)
            except ValueError:
                pass
            if not queue:
                owners.pop(owner, None)
            if not owners:
                self._ready.pop(namespace, None)

    def _on_done(self, ticket: CommitTicket, future: Future[Any]) -> None:
        with self._condition:
            if ticket._state == "preparing":
                # The native preparation still owns buffers. Cancellation must
                # not create fresh admission capacity until its owner settles.
                ticket._state = "cancelled_preparing"
                self._cancelled_before_commit += 1
                self._condition.notify_all()
                return
            if ticket._state in {"reserved", "queued", "running"}:
                if ticket._state == "queued":
                    self._remove_ready(ticket)
                if future.cancelled():
                    self._cancelled_before_commit += 1
                self._release(ticket, "cancelled" if future.cancelled() else "done")

    def _next_batch(self) -> list[CommitTicket] | None:
        with self._condition:
            while not self._ready:
                if self._closing and (self._cancel_on_close or not self._tickets):
                    return None
                self._condition.wait()
            if self._closing and self._cancel_on_close:
                return None
            namespace = next(iter(self._ready))
            owners = self._ready[namespace]
            deadline = min(queue[0]._ready_at for queue in owners.values()) + self.limits.coalesce_seconds
            while not self._closing and time.monotonic() < deadline:
                count = sum(len(queue) for queue in owners.values())
                if count >= self.limits.max_batch_commands:
                    break
                self._condition.wait(timeout=max(0.0, deadline - time.monotonic()))
                owners = self._ready.get(namespace)
                if not owners:
                    break
            if self._closing and self._cancel_on_close:
                return None
            batch: list[CommitTicket] = []
            byte_cost = units = 0
            while namespace in self._ready and len(batch) < self.limits.max_batch_commands:
                owners = self._ready[namespace]
                selected = None
                # Skip an owner whose next indivisible atomic command does not
                # fit, allowing another owner to use the remaining batch budget.
                for _ in range(len(owners)):
                    owner, queue = next(iter(owners.items()))
                    candidate = queue[0]
                    owners.move_to_end(owner)
                    if (byte_cost + candidate.byte_cost <= self.limits.max_batch_bytes
                            and units + candidate.write_units <= self.limits.max_batch_units):
                        selected = candidate
                        queue.popleft()
                        if not queue:
                            owners.pop(owner)
                        if not owners:
                            self._ready.pop(namespace)
                        break
                if selected is None:
                    break
                if not selected.future.set_running_or_notify_cancel():
                    self._cancelled_before_commit += 1
                    self._release(selected, "cancelled")
                    continue
                selected._state = "running"
                batch.append(selected)
                byte_cost += selected.byte_cost
                units += selected.write_units
            if namespace in self._ready:
                self._ready.move_to_end(namespace)
            return batch

    def _execute_batch(self, batch: list[CommitTicket]) -> None:
        results: list[tuple[CommitTicket, Any, BaseException | None]] = []
        try:
            # Only one namespace/database participates in an outer transaction.
            # Different MeshStore objects share the thread-owned writer lease.
            with batch[0]._store.write_transaction():
                for ticket in batch:
                    try:
                        def execute() -> Any:
                            with ticket._store.write_transaction():
                                return ticket._callback()
                        result = ticket._context.run(execute)
                    except Exception as exc:
                        results.append((ticket, None, exc))
                    else:
                        results.append((ticket, result, None))
            with self._condition:
                self._committed_batches += 1
                self._executed_commands += len(batch)
        except BaseException as exc:
            with self._condition:
                self._failed_batches += 1
            # A failure around commit cannot authorize an automatic replay,
            # including callbacks that returned a tentative result beforehand.
            for ticket in batch:
                self._publish(ticket, None, CommitOutcomeUnknown(ticket.binding, type(exc).__name__))
            if not isinstance(exc, Exception):
                raise
        else:
            for ticket, result, error in results:
                self._publish(ticket, result, error)

    def _publish(self, ticket: CommitTicket, result: Any, error: BaseException | None) -> None:
        # User callbacks may reenter the coordinator, so no coordinator lock is
        # held while completing their future. All SQL commit contexts have exited.
        with self._condition:
            self._release(ticket, "done")
        if error is None:
            ticket.future.set_result(result)
        else:
            ticket.future.set_exception(error)

    def _run(self) -> None:
        try:
            while True:
                batch = self._next_batch()
                if batch is None:
                    return
                if batch:
                    self._execute_batch(batch)
        except BaseException:
            # A fatal worker error must not strand pending callers. Their
            # queued/preparing commands have never entered a commit transaction.
            with self._condition:
                running = [ticket for ticket in self._tickets.values()
                           if ticket._state == "running" and not ticket.future.done()]
            for ticket in running:
                self._publish(ticket, None, CommitOutcomeUnknown(ticket.binding, "FatalWorkerFailure"))
            self.close(cancel_pending=True, timeout=0)

    def close(self, *, cancel_pending: bool = True, timeout: float = 5.0) -> bool:
        if type(cancel_pending) is not bool or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= 60:
            raise ValueError("Use boolean cancellation and a bounded shutdown timeout.")
        with self._condition:
            self._closing = True
            self._cancel_on_close = self._cancel_on_close or cancel_pending
            pending = [ticket for ticket in self._tickets.values() if ticket._state != "running"] if cancel_pending else []
            self._condition.notify_all()
            thread = self._thread
        for ticket in pending:
            ticket.future.cancel()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        with self._condition:
            return (thread is None or not thread.is_alive()) and not self._tickets

    def stats(self) -> dict[str, Any]:
        with self._condition:
            states = Counter(ticket._state for ticket in self._tickets.values())
            return {
                "pending": len(self._tickets), "pending_bytes": self._pending_bytes,
                "preparing": states["preparing"] + states["cancelled_preparing"],
                "queued": states["queued"], "running": states["running"],
                "active_namespaces": len(self._namespace_pending),
                "max_observed_pending": self._max_observed_pending, "max_observed_bytes": self._max_observed_bytes,
                "committed_batches": self._committed_batches, "failed_batches": self._failed_batches,
                "executed_commands": self._executed_commands, "rejected_admissions": dict(self._rejected),
                "cancelled_before_commit": self._cancelled_before_commit,
                "preparation_failures": self._preparation_failures, "closing": self._closing,
                "worker_alive": self._thread is not None and self._thread.is_alive(),
            }

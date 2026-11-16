"""Optional bounded, content-free instrumentation for isolated storage diagnostics."""
from __future__ import annotations

from bisect import bisect_left
from collections import deque
from functools import wraps
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Any

VERSION = "storage-stage-profile-v1"
PHASES = ("startup", "ingestion", "verification", "shutdown")
STAGES = (
    "store_commit", "audit_commit", "write_begin", "savepoint_begin",
    "namespace_acquire", "namespace_hold", "os_lock_acquire", "writer_lease",
    "writer_checkout", "group_ready_wait", "prepare", "learning_candidates",
    "model_batch", "embedding_publish", "event_delivery",
)
BOUNDS_NS = (0, *(1 << power for power in range(10, 44)))
MAX_THREADS = 64
MAX_SIDECAR_BYTES = 512 * 1024
STATES = frozenset({"complete", "partial", "unobserved", "not_requested"})
_COUNTERS = ("count", "failed_count", "wall_sum_ns", "thread_cpu_sum_ns", "wall_max_ns", "thread_cpu_max_ns")
LOSS_COUNTERS = ("dropped_spans", "inflight_spans", "checkpoint_failures", "restore_failures")
_HEX = re.compile(r"[a-f0-9]{64}")


class ProfileError(ValueError):
    """A fixed content-free profile contract was not satisfied."""


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def source_digest() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def binding(config: dict[str, Any]) -> dict[str, Any]:
    value = {
        "runtime_sha256": _digest(config["source_manifest"]),
        "harness_sha256": _digest(config["benchmark_source_manifest"]),
        "fixture_sha256": config["storage_profile_fixture_sha256"],
        "profiler_sha256": source_digest(),
        "backend": config["backend"], "surface": config["surface"], "mode": config["storage_profile_mode"],
        "group_commit": config["group_commit"], "agents": config["storage_profile_agents"],
        "input_facts": config["storage_profile_input_facts"],
    }
    validate_binding(value)
    return value


def validate_binding(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {
        "runtime_sha256", "harness_sha256", "fixture_sha256", "profiler_sha256",
        "backend", "surface", "mode", "group_commit", "agents", "input_facts",
    }:
        raise ProfileError("ProfileBindingInvalid")
    if any(type(value[name]) is not str or not _HEX.fullmatch(value[name]) for name in (
        "runtime_sha256", "harness_sha256", "fixture_sha256", "profiler_sha256",
    )):
        raise ProfileError("ProfileSourceDigestInvalid")
    if (any(type(value[name]) is not str for name in ("backend", "surface", "mode"))
            or value["backend"] not in ("hash", "neural") or value["surface"] not in ("bare-sdk", "receipt-sdk", "http")
            or value["mode"] not in ("scalar", "atomic") or type(value["group_commit"]) is not int
            or value["group_commit"] not in (0, 1) or type(value["agents"]) is not int
            or value["agents"] not in (1, 4, 8, 16) or type(value["input_facts"]) is not int
            or not 1 <= value["input_facts"] <= 100000):
        raise ProfileError("ProfileCellBindingInvalid")


def unobserved(requested: bool) -> dict[str, Any]:
    return {"version": VERSION, "state": "unobserved" if requested else "not_requested", "observations": []}


def complete_observation(value: dict[str, Any]) -> bool:
    phases = value.get("phases", {})
    return (value.get("state") == "complete" and all(value.get(name) == 0 for name in LOSS_COUNTERS)
            and isinstance(phases, dict) and set(phases) == set(PHASES)
            and all(isinstance(phases[phase], dict) and phases[phase].get("state") == "complete" for phase in PHASES))


def _empty() -> dict[str, Any]:
    return {**dict.fromkeys(_COUNTERS, 0), "wall_bins": [0] * (len(BOUNDS_NS) + 1),
            "thread_cpu_bins": [0] * (len(BOUNDS_NS) + 1)}


def _upper(bins: list[int], fraction: float) -> int | None:
    count = sum(bins)
    if not count:
        return None
    target, accumulated = math.ceil(count * fraction), 0
    for index, observed in enumerate(bins):
        accumulated += observed
        if accumulated >= target:
            return BOUNDS_NS[index] if index < len(BOUNDS_NS) else None
    return None


class _ThreadBuffer:
    def __init__(self) -> None:
        self.rows = [[_empty() for _ in STAGES] for _ in PHASES]
        self.namespace_depth = 0
        self.inflight = 0


class _NamespaceContext:
    def __init__(self, profiler: StorageProfiler, original: Any) -> None:
        self.profiler, self.original = profiler, original
        self.buffer = None
        self.hold = None

    def __enter__(self) -> Any:
        result = self.profiler.call("namespace_acquire", self.original.__enter__)
        self.buffer = self.profiler.buffer()
        if self.buffer is not None:
            if self.buffer.namespace_depth == 0:
                self.hold = self.profiler.started()
            self.buffer.namespace_depth += 1
        return result

    def __exit__(self, kind: Any, value: Any, traceback: Any) -> Any:
        failed = kind is not None
        try:
            return self.original.__exit__(kind, value, traceback)
        except BaseException:
            failed = True
            raise
        finally:
            if self.buffer is not None:
                self.buffer.namespace_depth -= 1
            if self.hold is not None:
                self.profiler.finished("namespace_hold", self.hold, failed)


class StorageProfiler:
    """Own patches and fixed per-thread buffers without a global recording lock."""

    def __init__(self, enabled: bool, *, output: Path | None = None, source_binding: dict[str, Any] | None = None,
                 wall_clock: Any = time.perf_counter_ns, cpu_clock: Any = time.thread_time_ns,
                 ready_clock: Any = time.monotonic, max_threads: int = MAX_THREADS) -> None:
        if type(enabled) is not bool or type(max_threads) is not int or not 1 <= max_threads <= MAX_THREADS:
            raise ProfileError("ProfileLimitsInvalid")
        if enabled:
            validate_binding(source_binding)
        self.enabled, self.output = enabled, output
        self.source_binding = dict(source_binding) if enabled else None
        self.wall_clock, self.cpu_clock = wall_clock, cpu_clock
        self.ready_clock = ready_clock
        self.local = threading.local()
        self.buffers = [_ThreadBuffer() for _ in range(max_threads)] if enabled else []
        # deque claims are bounded; recording on an owned buffer takes no shared lock.
        self.available = deque(self.buffers)
        self.registered = self.dropped_spans = self.checkpoint_failures = self.restore_failures = 0
        self.phase_index = 0
        self.visited = {0} if enabled else set()
        self.patches: list[tuple[Any, str, Any, Any]] = []
        self.coalesce_ns = None
        self.stop_event = threading.Event()
        self.checkpoint_thread = None
        self.closed = False
        self.close_complete = False
        self.publish_lock = threading.Lock()

    def buffer(self) -> _ThreadBuffer | None:
        if not self.enabled:
            return None
        if not hasattr(self.local, "buffer"):
            try:
                self.local.buffer = self.available.popleft()
                self.registered += 1
            except IndexError:
                self.local.buffer = None
        return self.local.buffer

    def phase(self, value: str) -> None:
        if not self.enabled:
            return
        if value not in PHASES:
            raise ProfileError("ProfilePhaseInvalid")
        self.phase_index = PHASES.index(value)
        self.visited.add(self.phase_index)

    def started(self) -> tuple[int, int, int] | None:
        if not self.enabled:
            return None
        try:
            buffer = self.buffer()
            if buffer is None:
                self.dropped_spans += 1
                return None
            result = self.phase_index, self.wall_clock(), self.cpu_clock()
            buffer.inflight += 1
            return result
        except Exception:
            self.dropped_spans += 1
            return None

    def record(self, stage: str, phase: int, wall: int, cpu: int, failed: bool = False) -> None:
        if not self.enabled:
            return
        if stage not in STAGES or type(phase) is not int or not 0 <= phase < len(PHASES):
            raise ProfileError("ProfileStageInvalid")
        buffer = self.buffer()
        if buffer is None:
            self.dropped_spans += 1
            return
        row = buffer.rows[phase][STAGES.index(stage)]
        wall, cpu = max(0, int(wall)), max(0, int(cpu))
        row["wall_bins"][bisect_left(BOUNDS_NS, wall)] += 1
        row["thread_cpu_bins"][bisect_left(BOUNDS_NS, cpu)] += 1
        row["count"] += 1
        row["failed_count"] += int(failed)
        row["wall_sum_ns"] += wall
        row["thread_cpu_sum_ns"] += cpu
        row["wall_max_ns"] = max(row["wall_max_ns"], wall)
        row["thread_cpu_max_ns"] = max(row["thread_cpu_max_ns"], cpu)

    def finished(self, stage: str, started: tuple[int, int, int] | None, failed: bool) -> None:
        if started is None:
            return
        try:
            phase, wall, cpu = started
            self.record(stage, phase, self.wall_clock() - wall, self.cpu_clock() - cpu, failed)
        except Exception:
            self.dropped_spans += 1
        finally:
            buffer = self.buffer()
            if buffer is not None:
                buffer.inflight = max(0, buffer.inflight - 1)

    def call(self, stage: str, original: Any, *args: Any, **kwargs: Any) -> Any:
        started = self.started()
        failed = False
        try:
            return original(*args, **kwargs)
        except BaseException:
            failed = True
            raise
        finally:
            self.finished(stage, started, failed)

    def wrap(self, stage: str, original: Any) -> Any:
        if stage not in STAGES:
            raise ProfileError("ProfileStageInvalid")
        @wraps(original)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            return self.call(stage, original, *args, **kwargs)
        return wrapped

    def patch(self, owner: Any, name: str, replacement: Any) -> None:
        original = getattr(owner, name)
        setattr(owner, name, replacement)
        self.patches.append((owner, name, original, replacement))

    def install(self, modules: Any = None) -> StorageProfiler:
        if not self.enabled:
            return self
        if self.patches or self.closed:
            raise ProfileError("ProfileSessionAlreadyUsed")
        if modules is None:
            from mesh import audit, group_commit, memory, sqlite_runtime, store
            modules = {"audit": audit, "group": group_commit, "memory": memory,
                       "sqlite": sqlite_runtime, "store": store}
        audit, group, memory, sqlite, store = (modules[name] for name in ("audit", "group", "memory", "sqlite", "store"))
        try:
            self.buffer()
            original_commit = sqlite.Connection.commit
            @wraps(original_commit)
            def commit(connection: Any, *args: Any, **kwargs: Any) -> Any:
                try:
                    active = connection.in_transaction
                except Exception:
                    active = False
                stage = ("store_commit" if isinstance(connection, store._PooledConnection)
                         else "audit_commit" if isinstance(connection, audit._ManagedAuditConnection) else None)
                return self.call(stage, original_commit, connection, *args, **kwargs) if active and stage else original_commit(connection, *args, **kwargs)
            self.patch(sqlite.Connection, "commit", commit)
            original_begin = store._begin_write
            @wraps(original_begin)
            def begin(connection: Any, *args: Any, **kwargs: Any) -> Any:
                try:
                    active = connection.in_transaction
                except Exception:
                    active = False
                return self.call("savepoint_begin" if active else "write_begin", original_begin, connection, *args, **kwargs)
            self.patch(store, "_begin_write", begin)
            original_namespace = store.namespace_write_lock
            @wraps(original_namespace)
            def namespace(*args: Any, **kwargs: Any) -> Any:
                return _NamespaceContext(self, original_namespace(*args, **kwargs))
            self.patch(store, "namespace_write_lock", namespace)
            if getattr(memory, "namespace_write_lock", None) is original_namespace:
                self.patch(memory, "namespace_write_lock", namespace)
            for owner, name, stage in (
                (store, "_acquire_os_file_lock", "os_lock_acquire"),
                (store.MeshStore, "_connect", "writer_lease"),
                (store.MeshStore, "publish_embedding", "embedding_publish"),
                (memory.Mesh, "_prepare_learn", "prepare"),
                (memory.Mesh, "_learning_candidates", "learning_candidates"),
                (memory._DirectOnnxEmbedding, "_encode_raw_batches", "model_batch"),
                (audit, "deliver_mutation_events", "event_delivery"),
            ):
                self.patch(owner, name, self.wrap(stage, getattr(owner, name)))
            original_checkout = store._ConnectionCheckout.__enter__
            @wraps(original_checkout)
            def checkout(instance: Any, *args: Any, **kwargs: Any) -> Any:
                return (original_checkout(instance, *args, **kwargs) if isinstance(instance, store._ReaderCheckout)
                        else self.call("writer_checkout", original_checkout, instance, *args, **kwargs))
            self.patch(store._ConnectionCheckout, "__enter__", checkout)
            original_batch = group.GroupCommitCoordinator._execute_batch
            @wraps(original_batch)
            def batch(instance: Any, tickets: Any, *args: Any, **kwargs: Any) -> Any:
                try:
                    now = self.ready_clock()
                    self.coalesce_ns = int(instance.limits.coalesce_seconds * 1e9)
                    for ticket in tickets if type(tickets) is list else ():
                        ready = ticket._ready_at
                        if type(ready) in (int, float) and math.isfinite(ready) and 0 < ready <= now:
                            self.record("group_ready_wait", self.phase_index, int((now - ready) * 1e9), 0)
                except Exception:
                    self.dropped_spans += 1
                return original_batch(instance, tickets, *args, **kwargs)
            self.patch(group.GroupCommitCoordinator, "_execute_batch", batch)
            if self.output is not None:
                self.publish(False)
                self.checkpoint_thread = threading.Thread(target=self._checkpoint, name="storage-profile-checkpoint", daemon=True)
                self.checkpoint_thread.start()
        except BaseException:
            self.restore()
            raise
        return self

    def snapshot(self, complete: bool = False) -> dict[str, Any]:
        if not self.enabled:
            return unobserved(False)
        inflight = sum(buffer.inflight for buffer in self.buffers)
        complete = (complete and self.closed and self.close_complete and len(self.visited) == len(PHASES)
                    and not any((inflight, self.dropped_spans, self.checkpoint_failures, self.restore_failures)))
        phases = {}
        for phase_index, phase in enumerate(PHASES):
            stages = {}
            for stage_index, stage in enumerate(STAGES):
                total = _empty()
                for buffer in self.buffers:
                    row = buffer.rows[phase_index][stage_index]
                    for name in _COUNTERS:
                        total[name] = max(total[name], row[name]) if name.endswith("max_ns") else total[name] + row[name]
                    for name in ("wall_bins", "thread_cpu_bins"):
                        total[name] = [a + b for a, b in zip(total[name], row[name])]
                total.update(wall_p95_upper_ns=_upper(total["wall_bins"], .95),
                             wall_p99_upper_ns=_upper(total["wall_bins"], .99))
                stages[stage] = total
            phases[phase] = {"state": "complete" if complete and phase_index in self.visited else "partial" if phase_index in self.visited else "unobserved",
                             "stages": stages if phase_index in self.visited else {}}
        return {"version": VERSION, "state": "complete" if complete else "partial", "binding": self.source_binding,
                "histogram_upper_bounds_ns": list(BOUNDS_NS), "max_threads": len(self.buffers),
                "registered_threads": len(self.buffers) - len(self.available), "dropped_spans": self.dropped_spans,
                "inflight_spans": inflight,
                "checkpoint_failures": self.checkpoint_failures, "restore_failures": self.restore_failures,
                "configured_coalesce_ns": self.coalesce_ns, "in_progress_snapshot_approximate": not complete,
                "stage_times_overlap": True, "phase_attribution_approximate": True,
                "dropped_count_approximate_under_concurrent_overflow": True,
                "calling_thread_cpu_excludes_native_threads": True,
                "periodic_checkpoint_loss_possible": True, "phases": phases}

    def publish(self, complete: bool) -> None:
        if self.output is None or not self.enabled:
            return
        # Publication alone shares this lock; runtime recording never acquires it.
        if not self.publish_lock.acquire(blocking=False):
            self.checkpoint_failures += 1
            return
        try:
            output = json.dumps(self.snapshot(complete), sort_keys=True, separators=(",", ":")).encode()
            if len(output) > MAX_SIDECAR_BYTES:
                raise ProfileError("ProfileCheckpointTooLarge")
            temporary = self.output.with_suffix(".tmp")
            with temporary.open("wb") as handle:
                handle.write(output)
                handle.flush()
            os.replace(temporary, self.output)
        except Exception:
            self.checkpoint_failures += 1
        finally:
            self.publish_lock.release()

    def _checkpoint(self) -> None:
        while not self.stop_event.wait(1.0):
            self.publish(False)

    def restore(self) -> None:
        for owner, name, original, replacement in reversed(self.patches):
            try:
                if getattr(owner, name, None) is replacement:
                    setattr(owner, name, original)
                else:
                    self.restore_failures += 1
            except Exception:
                self.restore_failures += 1
        self.patches.clear()

    def close(self, complete: bool = True) -> dict[str, Any]:
        if not self.enabled:
            return unobserved(False)
        if self.closed:
            return self.snapshot(self.close_complete)
        self.stop_event.set()
        checkpoint_stopped = self.checkpoint_thread is None
        if self.checkpoint_thread is not None:
            try:
                self.checkpoint_thread.join(timeout=5)
                checkpoint_stopped = not self.checkpoint_thread.is_alive()
            except Exception:
                checkpoint_stopped = False
            if not checkpoint_stopped:
                self.checkpoint_failures += 1
        self.restore()
        self.closed = True
        self.close_complete = (complete and checkpoint_stopped and len(self.visited) == len(PHASES)
                               and not any((sum(buffer.inflight for buffer in self.buffers), self.dropped_spans,
                                            self.checkpoint_failures, self.restore_failures)))
        # An unsettled checkpoint owner may still hold the temporary file. Its
        # eventual publication is partial; never compete with it during close.
        if checkpoint_stopped:
            self.publish(self.close_complete)
        if self.checkpoint_failures:
            self.close_complete = False
        return self.snapshot(self.close_complete)

    def __enter__(self) -> StorageProfiler:
        return self.install()

    def __exit__(self, kind: Any, value: Any, traceback: Any) -> bool:
        self.close(kind is None)
        return False


def sanitize(value: Any, expected_binding: dict[str, Any], *, interrupted: bool = False) -> dict[str, Any]:
    """Project only fixed counters/enums and validated source digests."""
    validate_binding(expected_binding)
    if not isinstance(value, dict) or value.get("version") != VERSION or value.get("binding") != expected_binding:
        raise ProfileError("ProfileSourceOrCellMismatch")
    if (value.get("state") not in ("complete", "partial") or value.get("histogram_upper_bounds_ns") != list(BOUNDS_NS)
            or type(value.get("max_threads")) is not int or not 1 <= value["max_threads"] <= MAX_THREADS):
        raise ProfileError("ProfileShapeInvalid")
    result = {"version": VERSION, "state": "partial" if interrupted else value["state"], "binding": dict(expected_binding),
              "histogram_upper_bounds_ns": list(BOUNDS_NS), "max_threads": value["max_threads"],
              "in_progress_snapshot_approximate": interrupted or value["state"] != "complete",
              "stage_times_overlap": True, "calling_thread_cpu_excludes_native_threads": True,
              "phase_attribution_approximate": True,
              "dropped_count_approximate_under_concurrent_overflow": True,
              "periodic_checkpoint_loss_possible": True, "phases": {}}
    for name in ("registered_threads", "dropped_spans", "inflight_spans", "checkpoint_failures", "restore_failures"):
        if type(value.get(name)) is not int or value[name] < 0:
            raise ProfileError("ProfileCounterInvalid")
        result[name] = value[name]
    if result["registered_threads"] > result["max_threads"]:
        raise ProfileError("ProfileThreadCapExceeded")
    coalesce = value.get("configured_coalesce_ns")
    if coalesce is not None and (type(coalesce) is not int or not 0 <= coalesce <= 20000000):
        raise ProfileError("ProfileCoalesceInvalid")
    result["configured_coalesce_ns"] = coalesce
    phases = value.get("phases")
    if not isinstance(phases, dict) or set(phases) != set(PHASES):
        raise ProfileError("ProfilePhasesInvalid")
    for phase in PHASES:
        source = phases[phase]
        if not isinstance(source, dict) or source.get("state") not in ("complete", "partial", "unobserved"):
            raise ProfileError("ProfilePhaseStateInvalid")
        state = "partial" if interrupted and source["state"] != "unobserved" else source["state"]
        projected = result["phases"][phase] = {"state": state, "stages": {}}
        if state == "unobserved":
            if source.get("stages") != {}:
                raise ProfileError("UnobservedProfileHasMeasurements")
            continue
        stages = source.get("stages")
        if not isinstance(stages, dict) or set(stages) != set(STAGES):
            raise ProfileError("ProfileStageCatalogInvalid")
        for stage in STAGES:
            row, clean = stages[stage], {}
            if not isinstance(row, dict):
                raise ProfileError("ProfileStageShapeInvalid")
            for name in _COUNTERS:
                if type(row.get(name)) is not int or row[name] < 0:
                    raise ProfileError("ProfileStageCounterInvalid")
                clean[name] = row[name]
            for name in ("wall_bins", "thread_cpu_bins"):
                bins = row.get(name)
                if not isinstance(bins, list) or len(bins) != len(BOUNDS_NS) + 1 or any(type(n) is not int or n < 0 for n in bins):
                    raise ProfileError("ProfileHistogramInvalid")
                clean[name] = list(bins)
            if state == "complete" and (clean["failed_count"] > clean["count"] or any(sum(clean[name]) != clean["count"] for name in ("wall_bins", "thread_cpu_bins"))):
                raise ProfileError("ProfileFinalCountMismatch")
            clean.update(wall_p95_upper_ns=_upper(clean["wall_bins"], .95), wall_p99_upper_ns=_upper(clean["wall_bins"], .99))
            projected["stages"][stage] = clean
    if value["state"] == "complete" and not complete_observation(value):
        raise ProfileError("ProfileCompletionInvalid")
    return result


def recover(path: Path, expected_binding: dict[str, Any], *, interrupted: bool = False) -> dict[str, Any]:
    validate_binding(expected_binding)
    path = Path(path)
    for parent in (path, *path.parents):
        if parent.exists():
            attributes = parent.lstat()
            if stat.S_ISLNK(attributes.st_mode) or getattr(attributes, "st_file_attributes", 0) & 0x400:
                raise ProfileError("ProfileRedirectedPath")
    if not path.is_file():
        return {"version": VERSION, "state": "unobserved", "binding": expected_binding, "phases": {}}
    if path.stat().st_size > MAX_SIDECAR_BYTES:
        raise ProfileError("ProfileCheckpointTooLarge")
    with path.open("rb") as handle:
        raw = handle.read(MAX_SIDECAR_BYTES + 1)
    if len(raw) > MAX_SIDECAR_BYTES:
        raise ProfileError("ProfileCheckpointTooLarge")
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise ProfileError("ProfileCheckpointInvalidJson") from exc
    return sanitize(value, expected_binding, interrupted=interrupted)

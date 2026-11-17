"""Concurrent guarded SDK writes with verified counts and full-home isolation.

Run: python benchmarks/concurrency_stress.py --output concurrency.json
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import multiprocessing as mp
import platform
import sys
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks.isolation import isolated_benchmark_home


@dataclass
class _WriteTally:
    attempted: int = 0
    inserted: int = 0
    deduplicated: int = 0
    failed: int = 0
    error_classes: Counter[str] = field(default_factory=Counter)

    def record(self, result: Any) -> None:
        if not isinstance(result, dict) or result.get("error") or result.get("status") not in ("stored", "stored_with_conflict"):
            self.failed += 1
            self.error_classes["RejectedOrInvalidResult"] += 1
            return
        disposition = result.get("disposition")
        if disposition is None:
            disposition = "deduplicated" if result.get("deduplicated") is True else "inserted"
        if disposition == "deduplicated" and result.get("memory_id"):
            self.deduplicated += 1
        elif disposition == "inserted" and result.get("memory_id"):
            self.inserted += 1
        else:
            self.failed += 1
            self.error_classes["UnknownDisposition"] += 1

    def merge(self, other: "_WriteTally") -> None:
        self.attempted += other.attempted
        self.inserted += other.inserted
        self.deduplicated += other.deduplicated
        self.failed += other.failed
        self.error_classes.update(other.error_classes)

    def report(self, submitted: int | None = None) -> dict[str, Any]:
        return {
            "submitted": self.attempted if submitted is None else submitted,
            "attempted": self.attempted,
            "committed_operations": self.inserted + self.deduplicated,
            "inserted": self.inserted,
            "deduplicated": self.deduplicated,
            "failed": self.failed,
            "unattempted": 0 if submitted is None else submitted - self.attempted,
            "error_classes": dict(self.error_classes),
        }


def _collect_futures(futures: list[Any]) -> tuple[list[Any], Counter[str]]:
    """Observe every completed task, even when an earlier future failed."""
    results = []
    errors: Counter[str] = Counter()
    for future in concurrent.futures.as_completed(futures):
        try:
            results.append(future.result())
        except Exception as exc:
            errors[type(exc).__name__] += 1
    return results, errors


def _learn_items(namespace: str, worker_id: str, count: int, content: Callable[[int], str]) -> _WriteTally:
    from mesh.memory import Mesh

    tally = _WriteTally()
    with Mesh(namespace=namespace, agent_id=worker_id) as mesh:
        for index in range(count):
            tally.attempted += 1
            try:
                tally.record(mesh.learn(content(index), memory_type="fact", tags=["concurrency-test", worker_id]))
            except Exception as exc:
                tally.failed += 1
                tally.error_classes[type(exc).__name__] += 1
    return tally


def _worker_process_learn(namespace: str, worker_id: int, count: int) -> _WriteTally:
    # Spawned workers inherit the coordinator's temporary homes and no-cloud flags.
    return _learn_items(
        namespace, f"process-{worker_id}", count,
        lambda index: f"Process benchmark fact process-{worker_id}-item-{index}: key=mp-{worker_id}-{index}",
    )


def _write_phase(
    mesh: Any, submitted: int, before: int, worker_results: list[_WriteTally],
    worker_errors: Counter[str], elapsed: float, expected_new: int | None = None,
) -> dict[str, Any]:
    tally = _WriteTally()
    for result in worker_results:
        tally.merge(result)
    report = tally.report(submitted)
    new_records = mesh.count() - before
    integrity_ok = new_records == tally.inserted
    if expected_new is not None:
        integrity_ok = integrity_ok and new_records == expected_new
    passed = (
        not worker_errors and report["failed"] == 0 and report["unattempted"] == 0
        and report["committed_operations"] == submitted and integrity_ok
    )
    report.update({
        "elapsed_seconds": round(elapsed, 6),
        "verified_new_records": new_records,
        "count_integrity_ok": integrity_ok,
        "worker_errors": dict(worker_errors),
        "new_records_per_second": round(new_records / elapsed, 3) if elapsed > 0 else 0,
        "committed_operations_per_second": round(report["committed_operations"] / elapsed, 3) if elapsed > 0 else 0,
        "status": "PASSED" if passed else "FAILED",
    })
    return report


def _interleaved(
    mesh: Any, namespace: str, readers: int, writers: int, duration: float,
) -> dict[str, Any]:
    from mesh.memory import Mesh

    stop = threading.Event()
    before = mesh.count()

    def reader(worker_id: int) -> dict[str, Any]:
        attempted = completed = failed = 0
        error_classes: Counter[str] = Counter()
        with Mesh(namespace=namespace, agent_id=f"reader-{worker_id}") as client:
            while not stop.is_set():
                attempted += 1
                try:
                    client.recall("primary cluster gateway interface", n=5)
                    completed += 1
                except Exception as exc:
                    failed += 1
                    error_classes[type(exc).__name__] += 1
                    stop.set()
        return {"kind": "reader", "attempted": attempted, "completed": completed, "failed": failed, "errors": dict(error_classes)}

    def writer(worker_id: int) -> dict[str, Any]:
        tally = _WriteTally()
        with Mesh(namespace=namespace, agent_id=f"stream-{worker_id}") as client:
            while not stop.is_set():
                index = tally.attempted
                tally.attempted += 1
                try:
                    tally.record(client.learn(
                        f"Streaming benchmark key=stream-{worker_id}-{index}: sequence={index}",
                        memory_type="fact", tags=["stream", "concurrency-test"],
                    ))
                    if tally.failed:
                        stop.set()
                except Exception as exc:
                    tally.failed += 1
                    tally.error_classes[type(exc).__name__] += 1
                    stop.set()
        return {"kind": "writer", "tally": tally}

    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=readers + writers) as executor:
        futures = [executor.submit(reader, worker) for worker in range(readers)]
        futures += [executor.submit(writer, worker) for worker in range(writers)]
        try:
            stop.wait(duration)
        finally:
            stop.set()
        collected, worker_errors = _collect_futures(futures)
    elapsed = time.perf_counter() - started
    read_results = [result for result in collected if result["kind"] == "reader"]
    write_results = [result["tally"] for result in collected if result["kind"] == "writer"]
    read_attempted = sum(result["attempted"] for result in read_results)
    read_completed = sum(result["completed"] for result in read_results)
    read_failed = sum(result["failed"] for result in read_results)
    write_report = _write_phase(
        mesh, sum(result.attempted for result in write_results), before,
        write_results, worker_errors, elapsed,
    )
    passed = write_report["status"] == "PASSED" and read_failed == 0 and read_completed > 0 and write_report["attempted"] > 0
    return {
        "readers": readers, "writers": writers, "requested_duration_seconds": duration,
        "elapsed_seconds": round(elapsed, 6),
        "reads": {"attempted": read_attempted, "completed": read_completed, "failed": read_failed},
        "writes": write_report,
        "completed_reads_per_second": round(read_completed / elapsed, 3),
        "worker_errors": dict(worker_errors), "status": "PASSED" if passed else "FAILED",
    }


def run_concurrency_stress(
    *, threads: int = 16, writes_per_thread: int = 20, processes: int = 4,
    items_per_process: int = 25, interleaved_seconds: float = 2.0,
    model_dir: Path | None = None,
) -> dict[str, Any]:
    if not 2 <= threads <= 64 or not 1 <= processes <= 16:
        raise ValueError("Use 2–64 threads and 1–16 processes.")
    if writes_per_thread < 1 or items_per_process < 1 or not 0 < interleaved_seconds <= 60:
        raise ValueError("Item counts and the bounded interleaved duration must be positive.")

    with isolated_benchmark_home(model_dir) as (temporary_root, models_copied):
        from mesh.memory import Mesh, get_engine_status
        from mesh.version import VERSION

        namespace = f"stress-{uuid.uuid4().hex[:8]}"
        with Mesh(namespace=namespace, agent_id="stress-coordinator") as mesh:
            results: dict[str, Any] = {
                "mesh_version": VERSION,
                "python_version": platform.python_version(),
                "platform": platform.platform(),
                "surface": "guarded_sdk",
                "isolation": "temporary Mesh, home, and agent configuration directories",
                "explicit_models_copied": models_copied,
            }
            duplicate = "Critical config: primary cluster gateway is 10.0.0.1 on interface eth0"
            before = mesh.count()
            started = time.perf_counter()
            with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
                futures = [
                    executor.submit(_learn_items, namespace, f"duplicate-{worker}", 1, lambda index: duplicate)
                    for worker in range(threads)
                ]
                collected, errors = _collect_futures(futures)
            results["dedup_race"] = _write_phase(mesh, threads, before, collected, errors, time.perf_counter() - started, expected_new=1)

            before = mesh.count()
            started = time.perf_counter()
            with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
                futures = [
                    executor.submit(
                        _learn_items, namespace, f"writer-{worker}", writes_per_thread,
                        lambda index, worker=worker: f"Distinct benchmark key=thread-{worker}-{index}: value={worker * 1000 + index}",
                    )
                    for worker in range(threads)
                ]
                collected, errors = _collect_futures(futures)
            results["multi_writer"] = _write_phase(
                mesh, threads * writes_per_thread, before, collected, errors,
                time.perf_counter() - started, expected_new=threads * writes_per_thread,
            )

            readers = max(1, threads // 2)
            results["interleaved"] = _interleaved(mesh, namespace, readers, threads - readers, interleaved_seconds)

            before = mesh.count()
            started = time.perf_counter()
            with concurrent.futures.ProcessPoolExecutor(max_workers=processes, mp_context=mp.get_context("spawn")) as executor:
                futures = [
                    executor.submit(_worker_process_learn, namespace, worker, items_per_process)
                    for worker in range(processes)
                ]
                collected, errors = _collect_futures(futures)
            results["multi_process"] = _write_phase(
                mesh, processes * items_per_process, before, collected, errors,
                time.perf_counter() - started, expected_new=processes * items_per_process,
            )
            results["embedding_engine"] = get_engine_status()
            results["final_verified_records"] = mesh.count()
            phases = ("dedup_race", "multi_writer", "interleaved", "multi_process")
            results["status"] = "PASSED" if all(results[phase]["status"] == "PASSED" for phase in phases) else "FAILED"
            for phase in phases:
                print(f"{phase}: {results[phase]['status']}")
            print(f"Verified stored records: {results['final_verified_records']}; run: {results['status']}")
            return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print the complete report as JSON.")
    parser.add_argument("--output", type=Path, help="Write the complete report, including failures.")
    parser.add_argument("--threads", type=int, default=16)
    parser.add_argument("--writes-per-thread", type=int, default=20)
    parser.add_argument("--processes", type=int, default=4)
    parser.add_argument("--items-per-process", type=int, default=25)
    parser.add_argument("--interleaved-seconds", type=float, default=2.0)
    parser.add_argument("--model-dir", type=Path, help="Explicit local model directory to copy; never read the real Mesh home.")
    arguments = parser.parse_args()
    report = run_concurrency_stress(
        threads=arguments.threads, writes_per_thread=arguments.writes_per_thread,
        processes=arguments.processes, items_per_process=arguments.items_per_process,
        interleaved_seconds=arguments.interleaved_seconds, model_dir=arguments.model_dir,
    )
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if arguments.json:
        print(json.dumps(report, indent=2))
    return 0 if report["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Async transport admission, cancellation, context and stdio isolation."""
from __future__ import annotations

import asyncio
import io
import sys
import threading

import pytest

from mesh.audit import get_mesh_dir, mesh_dir_context
from mesh.execution import BoundedExecutor, CapacityError
from mesh.output import diagnostics_to_stderr


@pytest.mark.asyncio
async def test_offloaded_work_leaves_loop_live_and_copies_context(tmp_path):
    executor = BoundedExecutor(1, 1)
    entered = threading.Event()
    release = threading.Event()
    def blocking():
        entered.set()
        assert release.wait(3)
        return get_mesh_dir()
    try:
        with mesh_dir_context(tmp_path):
            task = asyncio.create_task(executor.run(blocking))
            assert await asyncio.to_thread(entered.wait, 3)
            await asyncio.sleep(0)
            release.set()
            assert await task == tmp_path
    finally:
        release.set()
        executor.close()


@pytest.mark.asyncio
async def test_cancelled_caller_does_not_admit_work_over_running_write():
    executor = BoundedExecutor(1, 1)
    entered = threading.Event()
    release = threading.Event()
    def blocking():
        entered.set()
        assert release.wait(3)
    try:
        task = asyncio.create_task(executor.run(blocking))
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(CapacityError):
            executor.submit(lambda: True)
        release.set()
        deadline = asyncio.get_running_loop().time() + 3
        while True:
            try:
                next_work = executor.submit(lambda: "admitted")
                break
            except CapacityError:
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.001)
        assert await asyncio.wrap_future(next_work) == "admitted"
    finally:
        release.set()
        executor.close()


def test_diagnostic_redirect_cannot_redirect_other_thread_protocol(monkeypatch):
    protocol = io.StringIO()
    diagnostics = io.StringIO()
    monkeypatch.setattr(sys, "stdout", protocol)
    monkeypatch.setattr(sys, "stderr", diagnostics)
    rendezvous = threading.Barrier(2)
    def worker():
        with diagnostics_to_stderr():
            rendezvous.wait(timeout=3)
            print("model diagnostic")
    thread = threading.Thread(target=worker)
    thread.start()
    rendezvous.wait(timeout=3)
    print('{"jsonrpc":"2.0"}')
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert protocol.getvalue() == '{"jsonrpc":"2.0"}\n'
    assert diagnostics.getvalue() == "model diagnostic\n"

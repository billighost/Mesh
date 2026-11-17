"""Cancellation synchronization keeps using the retained coordinator implementation."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace


def test_cancel_close_remains_deterministic_after_group_module_reload(tmp_path, monkeypatch):
    import mesh
    from tests import test_group_commit as group_tests

    coordinator_class = group_tests.GroupCommitCoordinator
    implementation = coordinator_class._next_batch.__globals__
    original_module = sys.modules["mesh.group_commit"]
    spec = importlib.util.spec_from_file_location("mesh.group_commit", Path(implementation["__file__"]))
    replacement = importlib.util.module_from_spec(spec)
    retained_clock = [100.0]
    original_start = coordinator_class.start

    def delayed_start(coordinator):
        # Reproduce a worker scheduled after its 20 ms coalescing window without
        # sleeping or changing Event/Condition wait clocks. A correct test clock
        # override belongs to the implementation, not this advancing clock.
        retained_clock[0] = 101.0
        return original_start(coordinator)

    with monkeypatch.context() as context:
        context.setitem(sys.modules, "mesh.group_commit", replacement)
        context.setattr(mesh, "group_commit", replacement)
        spec.loader.exec_module(replacement)
        assert replacement is not original_module
        assert implementation is not replacement.__dict__
        context.setitem(implementation, "time", SimpleNamespace(monotonic=lambda: retained_clock[0]))
        context.setattr(coordinator_class, "start", delayed_start)
        group_tests.test_cancel_close_wins_before_a_waiting_batch_can_start(tmp_path, context)

    assert sys.modules["mesh.group_commit"] is original_module
    assert coordinator_class.start is original_start

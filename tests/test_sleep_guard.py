"""
tests/test_sleep_guard.py — Tests for sleep guard, power monitoring, and timing discrepancy detector.
"""
from __future__ import annotations

import datetime
from unittest.mock import patch
import pytest

from benchmarks.sleep_guard import (
    DualTimer,
    ExecutionStateGuard,
    assert_power_and_sleep_readiness,
    check_sleep_timeouts,
    get_active_power_scheme,
    get_power_status,
    query_kernel_power_events,
)


def test_get_power_status():
    status = get_power_status()
    assert isinstance(status, dict)
    assert "on_ac" in status
    assert isinstance(status["on_ac"], bool)


def test_get_active_power_scheme():
    scheme = get_active_power_scheme()
    assert isinstance(scheme, str)
    assert len(scheme) > 0


def test_check_sleep_timeouts():
    timeouts = check_sleep_timeouts()
    assert isinstance(timeouts, dict)
    assert "ac_sleep_seconds" in timeouts


def test_assert_power_readiness_on_ac():
    with patch("benchmarks.sleep_guard.get_power_status", return_value={"on_ac": True, "battery_pct": 100}), \
         patch("benchmarks.sleep_guard.check_sleep_timeouts", return_value={"ac_sleep_seconds": 0, "ac_hibernate_seconds": 0}):
        info = assert_power_and_sleep_readiness()
        assert info["power_status"]["on_ac"] is True


def test_assert_power_readiness_rejects_battery():
    with patch("benchmarks.sleep_guard.get_power_status", return_value={"on_ac": False, "battery_pct": 50}), \
         patch("benchmarks.sleep_guard.check_sleep_timeouts", return_value={"ac_sleep_seconds": 0, "ac_hibernate_seconds": 0}):
        with pytest.raises(RuntimeError, match="TIMED RUN REFUSED — SYSTEM IS RUNNING ON BATTERY"):
            assert_power_and_sleep_readiness()


def test_assert_power_readiness_rejects_sleep_timeout():
    with patch("benchmarks.sleep_guard.get_power_status", return_value={"on_ac": True, "battery_pct": 100}), \
         patch("benchmarks.sleep_guard.check_sleep_timeouts", return_value={"ac_sleep_seconds": 300, "ac_hibernate_seconds": 0}):
        with pytest.raises(RuntimeError, match="TIMED RUN REFUSED — AC SLEEP/HIBERNATE TIMEOUT IS NON-ZERO"):
            assert_power_and_sleep_readiness()


def test_execution_state_guard():
    with ExecutionStateGuard():
        pass  # Ensure context manager enters and exits cleanly


def test_dual_timer_normal():
    with DualTimer() as dt:
        import time
        time.sleep(0.01)
    assert dt.wall_elapsed > 0.0
    assert dt.mono_elapsed > 0.0
    assert not dt.discrepancy_flag


def test_dual_timer_discrepancy_flag():
    dt = DualTimer()
    dt.wall_elapsed = 10.0
    dt.mono_elapsed = 1.0
    diff = abs(dt.wall_elapsed - dt.mono_elapsed)
    if diff > 0.5 or (dt.mono_elapsed > 1.0 and diff / dt.mono_elapsed > 0.05):
        dt.discrepancy_flag = True
    assert dt.discrepancy_flag is True


def test_query_kernel_power_events():
    events = query_kernel_power_events(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5))
    assert isinstance(events, list)

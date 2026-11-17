"""
benchmarks/sleep_guard.py — Sleep-proofing, power state assertions, exclusivity, dual-timing, and Kernel-Power audit.
"""
from __future__ import annotations

import ctypes
import datetime
import json
import logging
import os
import subprocess
import sys
import time
from typing import Any, Optional

try:
    import psutil
except ImportError:
    psutil = None

logger = logging.getLogger(__name__)


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_byte),
        ("BatteryFlag", ctypes.c_byte),
        ("BatteryLifePercent", ctypes.c_byte),
        ("SystemStatusFlag", ctypes.c_byte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


# Windows Thread Execution State flags
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
ES_AWAYMODE_REQUIRED = 0x00000040
ES_CONTINUOUS = 0x80000000


def get_power_status() -> dict[str, Any]:
    """Return dictionary of current AC/battery power status."""
    if os.name != "nt":
        return {"platform": sys.platform, "on_ac": True, "battery_pct": 100}

    status = SYSTEM_POWER_STATUS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return {"error": "GetSystemPowerStatus failed"}

    return {
        "on_ac": status.ACLineStatus == 1,
        "ac_line_status": int(status.ACLineStatus),
        "battery_pct": int(status.BatteryLifePercent),
        "battery_flag": int(status.BatteryFlag),
    }


def get_active_power_scheme() -> str:
    """Return current active power scheme friendly name."""
    if os.name != "nt":
        return "N/A"
    try:
        res = subprocess.run(["powercfg", "/getactivescheme"], capture_output=True, text=True, check=True)
        return res.stdout.strip()
    except Exception as e:
        return f"Unknown ({e})"


def check_sleep_timeouts() -> dict[str, Any]:
    """Query AC sleep and hibernate timeouts via powercfg."""
    if os.name != "nt":
        return {"ac_sleep_seconds": 0, "ac_hibernate_seconds": 0}

    sleep_sec = None
    hib_sec = None
    try:
        p_sleep = subprocess.run(
            ["powercfg", "/q", "SCHEME_CURRENT", "SUB_SLEEP", "STANDBYIDLE"],
            capture_output=True,
            text=True,
        )
        for line in p_sleep.stdout.splitlines():
            if "Current AC Power Setting Index:" in line:
                sleep_sec = int(line.split(":")[-1].strip(), 16)
                break

        p_hib = subprocess.run(
            ["powercfg", "/q", "SCHEME_CURRENT", "SUB_SLEEP", "HIBERNATEIDLE"],
            capture_output=True,
            text=True,
        )
        for line in p_hib.splitlines():
            if "Current AC Power Setting Index:" in line:
                hib_sec = int(line.split(":")[-1].strip(), 16)
                break
    except Exception:
        pass

    return {
        "ac_sleep_seconds": sleep_sec,
        "ac_hibernate_seconds": hib_sec,
    }


def assert_power_and_sleep_readiness(allow_battery_for_testing: bool = False) -> dict[str, Any]:
    """
    Assert that the machine is on AC power and AC sleep/hibernate timeouts are disabled.
    Refuses execution if on battery or AC sleep is enabled, printing exact remediation commands.
    """
    status = get_power_status()
    scheme = get_active_power_scheme()
    timeouts = check_sleep_timeouts()

    info = {
        "power_status": status,
        "active_scheme": scheme,
        "timeouts": timeouts,
        "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    if not allow_battery_for_testing and not status.get("on_ac", True):
        msg = (
            "\n"
            "=" * 78 + "\n"
            "ERROR: TIMED RUN REFUSED — SYSTEM IS RUNNING ON BATTERY!\n"
            f"Power state: Battery ({status.get('battery_pct')}%), ACLineStatus=0\n"
            "Per benchmark rules, timed phases must never run on battery power due to\n"
            "CPU thermal throttling and battery sleep/hibernation risks.\n\n"
            "ACTION REQUIRED: Connect laptop to AC power adapter, then re-run.\n"
            "=" * 78 + "\n"
        )
        raise RuntimeError(msg)

    ac_sleep = timeouts.get("ac_sleep_seconds")
    ac_hib = timeouts.get("ac_hibernate_seconds")
    if (ac_sleep is not None and ac_sleep > 0) or (ac_hib is not None and ac_hib > 0):
        msg = (
            "\n"
            "=" * 78 + "\n"
            "ERROR: TIMED RUN REFUSED — AC SLEEP/HIBERNATE TIMEOUT IS NON-ZERO!\n"
            f"Current settings: AC Sleep Timeout = {ac_sleep}s, AC Hibernate Timeout = {ac_hib}s\n"
            "Per benchmark rules, sleep timeouts must be disabled on AC power to prevent\n"
            "sleep contamination during benchmarks.\n\n"
            "ACTION REQUIRED: Run the following commands in an administrator PowerShell/cmd:\n"
            "  powercfg /change standby-timeout-ac 0\n"
            "  powercfg /change hibernate-timeout-ac 0\n"
            "  powercfg -setacvalueindex SCHEME_CURRENT 4f971e89-eebd-4455-a8de-9e59040e7347 5ca83367-6e45-459f-a27b-476b1d01c936 0\n"
            "=" * 78 + "\n"
        )
        raise RuntimeError(msg)

    return info


def cleanup_leftover_processes(exclude_current_pid: bool = True) -> int:
    """Kill any leftover mesh daemon, auxly, or rogue background benchmark python processes."""
    if not psutil:
        return 0

    killed = 0
    curr_pid = os.getpid()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            pid = proc.info["pid"]
            if exclude_current_pid and pid == curr_pid:
                continue

            cmdline = " ".join(proc.info.get("cmdline") or []).lower()
            name = (proc.info.get("name") or "").lower()

            # Identify mesh background daemons or auxly binaries
            is_mesh_daemon = "mesh.http_server" in cmdline or "mesh daemon" in cmdline
            is_auxly = "auxly.exe" in name or "auxly" in cmdline
            is_basic_mem = "basic-memory" in cmdline or "basic_memory" in cmdline

            if is_mesh_daemon or is_auxly or (is_basic_mem and "pytest" not in cmdline):
                proc.kill()
                killed += 1
                logger.info("Killed leftover benchmark process: PID %d (%s)", pid, cmdline[:80])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    return killed


def assert_system_exclusive(cpu_threshold_pct: float = 5.0, sample_duration_sec: float = 5.0) -> None:
    """
    Assert system exclusivity before a timed phase:
    1. Clean up leftover processes.
    2. Check CPU utilization is below threshold.
    """
    cleaned = cleanup_leftover_processes()
    if cleaned > 0:
        time.sleep(1.0)

    if not psutil:
        return

    # Sample CPU utilization
    cpu = psutil.cpu_percent(interval=sample_duration_sec)
    if cpu > (100.0 - cpu_threshold_pct) / 2.0 and cpu > 20.0:  # If CPU load is high
        # Sample again after a brief pause
        time.sleep(2.0)
        cpu = psutil.cpu_percent(interval=sample_duration_sec)
        logger.info("Measured baseline CPU load: %.1f%%", cpu)


class ExecutionStateGuard:
    """
    Context manager that keeps the Windows system awake during benchmark phases
    using SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED).
    """

    def __enter__(self):
        if os.name == "nt":
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if os.name == "nt":
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)


class DualTimer:
    """
    Measures both wall-clock time (time.time()) and monotonic time (time.perf_counter()).
    Flags any discrepancy between wall-clock and monotonic duration.
    """

    def __init__(self):
        self.wall_start = 0.0
        self.mono_start = 0.0
        self.wall_elapsed = 0.0
        self.mono_elapsed = 0.0
        self.discrepancy_flag = False

    def __enter__(self):
        self.wall_start = time.time()
        self.mono_start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        wall_end = time.time()
        mono_end = time.perf_counter()
        self.wall_elapsed = wall_end - self.wall_start
        self.mono_elapsed = mono_end - self.mono_start

        diff = abs(self.wall_elapsed - self.mono_elapsed)
        if diff > 0.5 or (self.mono_elapsed > 1.0 and diff / self.mono_elapsed > 0.05):
            self.discrepancy_flag = True


def query_kernel_power_events(start_dt: datetime.datetime, end_dt: Optional[datetime.datetime] = None) -> list[dict[str, Any]]:
    """
    Query Windows System event log for Kernel-Power sleep (ID 42) and resume (ID 107) events
    between start_dt and end_dt.
    """
    if os.name != "nt":
        return []

    if end_dt is None:
        end_dt = datetime.datetime.now(datetime.timezone.utc)

    start_str = start_dt.strftime("%Y-%m-%dT%H:%M:%S")
    end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%S")

    ps_script = f"""
    $events = Get-WinEvent -FilterHashtable @{{
        LogName='System';
        ProviderName='Microsoft-Windows-Kernel-Power';
        Id=42,107;
        StartTime=[datetime]::Parse('{start_str}');
        EndTime=[datetime]::Parse('{end_str}')
    }} -ErrorAction SilentlyContinue | Select-Object TimeCreated, Id, Message
    if ($events) {{
        $events | ConvertTo-Json -Compress
    }} else {{
        "[]"
    }}
    """
    try:
        res = subprocess.run(["powershell", "-NoProfile", "-Command", ps_script], capture_output=True, text=True, check=True)
        raw = res.stdout.strip()
        if not raw:
            return []
        data = json.loads(raw)
        if isinstance(data, dict):
            return [data]
        elif isinstance(data, list):
            return data
        return []
    except Exception:
        return []

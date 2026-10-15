"""
mesh/daemon.py — Windowless background daemon & OS login auto-start manager for Mesh.

Capabilities:
  - Non-blocking background daemon auto-starter (`ensure_daemon_running`)
  - Automatic first-run OS login registration so Mesh stays ready across reboots
  - Fast health polling and model warmup (`mesh-daemon warmup`)
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from .store import MESH_DIR

DAEMON_PID_FILE = MESH_DIR / "daemon.pid"
DAEMON_LOG_FILE = MESH_DIR / "daemon.log"
AUTOSTART_MARKER_FILE = MESH_DIR / ".autostart_registered"

DEFAULT_PORT = int(os.environ.get("MESH_HTTP_PORT", 7701))
DEFAULT_HOST = os.environ.get("MESH_HTTP_HOST", "127.0.0.1")
DEFAULT_IDLE_TIMEOUT = float(os.environ.get("MESH_IDLE_UNLOAD_SECONDS", "180"))

_WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_WIN_RUN_VALUE_NAME = "MeshContextDaemon"

_SPAWN_LOCK = threading.Lock()
_LAST_SPAWN_TS = 0.0


def _health_url(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> str:
    return f"http://{host}:{port}/health"


def get_daemon_health(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    timeout: float = 0.6,
) -> Optional[dict[str, Any]]:
    """Query the local daemon `/health` endpoint. Returns the JSON dict if alive, else None."""
    try:
        req = urllib.request.Request(_health_url(host, port), method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        pass
    return None


def trigger_daemon_unload(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> Optional[dict[str, Any]]:
    """Ask the running daemon to unload the neural model from RAM immediately."""
    try:
        req = urllib.request.Request(f"http://{host}:{port}/unload", data=b"{}", method="POST")
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        pass
    return None


def _read_pid_file() -> Optional[int]:
    if not DAEMON_PID_FILE.exists():
        return None
    try:
        raw = DAEMON_PID_FILE.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def _write_pid_file(pid: int) -> None:
    MESH_DIR.mkdir(parents=True, exist_ok=True)
    DAEMON_PID_FILE.write_text(str(pid), encoding="utf-8")


def _remove_pid_file() -> None:
    try:
        DAEMON_PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
            ).decode("utf-8", errors="ignore")
            return str(pid) in out
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _resolve_background_python() -> str:
    exe = Path(sys.executable)
    if os.name == "nt" and exe.name.lower() == "python.exe":
        pythonw = exe.with_name("pythonw.exe")
        if pythonw.exists():
            return str(pythonw)
    return str(exe)


def ensure_daemon_running(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    wait: bool = False,
) -> dict[str, Any]:
    """
    Ensure the Mesh background HTTP daemon is running and registered for OS startup.
    When `wait=False`, spawns the daemon in the background and returns in <2ms without blocking.
    """
    global _LAST_SPAWN_TS
    if os.environ.get("MESH_IN_DAEMON", "").strip().lower() in ("1", "true", "yes"):
        return {"running": True, "already_running": True, "pid": os.getpid(), "host": host, "port": port}

    health = get_daemon_health(host=host, port=port, timeout=0.18)
    if health is not None:
        return {
            "running": True,
            "already_running": True,
            "pid": health.get("pid"),
            "host": host,
            "port": port,
            "health": health,
        }

    with _SPAWN_LOCK:
        now = time.monotonic()
        if not wait and (now - _LAST_SPAWN_TS) < 4.0:
            pid = _read_pid_file()
            return {"running": False, "spawning": True, "pid": pid, "host": host, "port": port}
        _LAST_SPAWN_TS = now

    # Auto-register OS login startup once unless explicitly disabled
    if (
        not AUTOSTART_MARKER_FILE.exists()
        and "pytest" not in sys.modules
        and os.environ.get("MESH_NO_AUTO_STARTUP", "").strip().lower() not in ("1", "true", "yes")
    ):
        try:
            MESH_DIR.mkdir(parents=True, exist_ok=True)
            AUTOSTART_MARKER_FILE.write_text("1", encoding="utf-8")
            if not is_startup_installed():
                install_startup(host=host, port=port, idle_timeout=idle_timeout, start_now=False)
        except Exception:
            pass

    return start_daemon(host=host, port=port, idle_timeout=idle_timeout, lazy=False, wait=wait)


def start_daemon(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    lazy: bool = False,
    wait: bool = True,
) -> dict[str, Any]:
    """Start the Mesh background HTTP daemon if not already running."""
    existing_health = get_daemon_health(host=host, port=port, timeout=0.4 if wait else 0.15)
    if existing_health is not None:
        pid = existing_health.get("pid") or _read_pid_file()
        if pid:
            _write_pid_file(int(pid))
        return {
            "running": True,
            "already_running": True,
            "pid": pid,
            "host": host,
            "port": port,
            "health": existing_health,
        }

    MESH_DIR.mkdir(parents=True, exist_ok=True)
    python_exe = _resolve_background_python()
    cmd = [
        python_exe,
        "-m",
        "mesh.http_server",
        "--host",
        host,
        "--port",
        str(port),
        "--idle-timeout",
        str(idle_timeout),
    ]
    if lazy:
        cmd.append("--no-warmup")

    env = os.environ.copy()
    env["MESH_HTTP_HOST"] = host
    env["MESH_HTTP_PORT"] = str(port)
    env["MESH_IDLE_UNLOAD_SECONDS"] = str(idle_timeout)
    env["MESH_IN_DAEMON"] = "1"

    log_handle = open(DAEMON_LOG_FILE, "a", encoding="utf-8")
    try:
        if os.name == "nt":
            creationflags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            )
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
                env=env,
                creationflags=creationflags,
                close_fds=True,
            )
        else:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
                env=env,
                start_new_session=True,
                close_fds=True,
            )
    finally:
        log_handle.close()

    _write_pid_file(proc.pid)

    if not wait:
        return {
            "running": True,
            "already_running": False,
            "spawning": True,
            "pid": proc.pid,
            "host": host,
            "port": port,
            "health": None,
        }

    health: Optional[dict[str, Any]] = None
    for i in range(40):
        time.sleep(0.04 if i < 10 else 0.12)
        health = get_daemon_health(host=host, port=port, timeout=0.35)
        if health is not None:
            break

    return {
        "running": health is not None,
        "already_running": False,
        "pid": proc.pid,
        "host": host,
        "port": port,
        "health": health,
    }


def stop_daemon(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> dict[str, Any]:
    """Stop the running Mesh daemon."""
    health = get_daemon_health(host=host, port=port, timeout=0.5)
    pid = (health.get("pid") if health else None) or _read_pid_file()
    if not pid:
        _remove_pid_file()
        return {"stopped": False, "reason": "not_running", "pid": None}

    pid = int(pid)
    killed = False
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                check=False,
            )
            killed = True
        except Exception:
            killed = False
    else:
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(15):
                if not _is_pid_alive(pid):
                    break
                time.sleep(0.05)
            if _is_pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
            killed = True
        except OSError:
            killed = False

    _remove_pid_file()
    return {"stopped": killed, "pid": pid}


def install_startup(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    start_now: bool = True,
) -> dict[str, Any]:
    """Register the Mesh daemon for OS login auto-start AND start it immediately."""
    python_exe = _resolve_background_python()
    cmd_args = [
        python_exe,
        "-m",
        "mesh.http_server",
        "--host",
        host,
        "--port",
        str(port),
        "--idle-timeout",
        str(idle_timeout),
        "--no-warmup",
    ]

    if os.name == "nt":
        import winreg

        quoted_cmd = subprocess.list2cmdline(cmd_args)
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            _WIN_RUN_KEY,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, _WIN_RUN_VALUE_NAME, 0, winreg.REG_SZ, quoted_cmd)
        loc = f"HKCU\\{_WIN_RUN_KEY}\\{_WIN_RUN_VALUE_NAME}"
        platform_name = "windows"

    elif sys.platform == "darwin":
        plist_dir = Path.home() / "Library" / "LaunchAgents"
        plist_dir.mkdir(parents=True, exist_ok=True)
        plist_path = plist_dir / "com.mesh.daemon.plist"
        args_xml = "\n".join(f"        <string>{a}</string>" for a in cmd_args)
        plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.mesh.daemon</string>
    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{DAEMON_LOG_FILE}</string>
    <key>StandardErrorPath</key>
    <string>{DAEMON_LOG_FILE}</string>
</dict>
</plist>
"""
        plist_path.write_text(plist_content, encoding="utf-8")
        loc = str(plist_path)
        platform_name = "macos"

    else:
        autostart_dir = Path.home() / ".config" / "autostart"
        autostart_dir.mkdir(parents=True, exist_ok=True)
        desktop_path = autostart_dir / "mesh-daemon.desktop"
        exec_line = " ".join(cmd_args)
        desktop_content = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Mesh Context Daemon\n"
            f"Exec={exec_line}\n"
            "Hidden=false\n"
            "NoDisplay=true\n"
            "X-GNOME-Autostart-enabled=true\n"
        )
        desktop_path.write_text(desktop_content, encoding="utf-8")
        loc = str(desktop_path)
        platform_name = "linux"

    if start_now:
        start_daemon(host=host, port=port, idle_timeout=idle_timeout, lazy=True)

    return {
        "installed": True,
        "platform": platform_name,
        "location": loc,
        "command": " ".join(cmd_args),
    }


def remove_startup() -> dict[str, Any]:
    """Remove login auto-start registration."""
    try:
        AUTOSTART_MARKER_FILE.unlink(missing_ok=True)
    except OSError:
        pass

    if os.name == "nt":
        import winreg

        removed = False
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                _WIN_RUN_KEY,
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                winreg.DeleteValue(key, _WIN_RUN_VALUE_NAME)
                removed = True
        except FileNotFoundError:
            removed = False
        return {"removed": removed, "platform": "windows"}

    if sys.platform == "darwin":
        plist_path = Path.home() / "Library" / "LaunchAgents" / "com.mesh.daemon.plist"
        existed = plist_path.exists()
        plist_path.unlink(missing_ok=True)
        return {"removed": existed, "platform": "macos"}

    desktop_path = Path.home() / ".config" / "autostart" / "mesh-daemon.desktop"
    existed = desktop_path.exists()
    desktop_path.unlink(missing_ok=True)
    return {"removed": existed, "platform": "linux"}


def is_startup_installed() -> bool:
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                _WIN_RUN_KEY,
                0,
                winreg.KEY_READ,
            ) as key:
                val, _ = winreg.QueryValueEx(key, _WIN_RUN_VALUE_NAME)
                return bool(val)
        except OSError:
            return False
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "LaunchAgents" / "com.mesh.daemon.plist").exists()
    return (Path.home() / ".config" / "autostart" / "mesh-daemon.desktop").exists()


def daemon_cmd() -> None:
    """Entry point for: mesh-daemon / mesh daemon"""
    parser = argparse.ArgumentParser(description="Manage background Mesh daemon and auto-start.")
    parser.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=[
            "start",
            "stop",
            "restart",
            "status",
            "unload",
            "warmup",
            "install-startup",
            "remove-startup",
        ],
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT)
    parser.add_argument("--lazy", action="store_true")
    args = parser.parse_args()

    if args.action == "start":
        from .memory import ensure_model_downloaded

        ensure_model_downloaded(blocking=False)
        res = start_daemon(
            host=args.host,
            port=args.port,
            idle_timeout=args.idle_timeout,
            lazy=args.lazy,
        )
        if res["already_running"]:
            print(f"[OK] Mesh daemon already running on http://{args.host}:{args.port} (PID: {res['pid']})")
        else:
            print(f"[OK] Started Mesh daemon on http://{args.host}:{args.port} (PID: {res['pid']})")

    elif args.action == "warmup":
        from .memory import ensure_model_downloaded, warmup_model

        print("[Mesh] Ensuring local ONNX embedding model is downloaded...")
        pair = ensure_model_downloaded(blocking=True, timeout=60.0)
        if pair:
            print(f"[OK] ONNX model cached at {pair[0].parent}")
        warmup_model()
        res = start_daemon(
            host=args.host,
            port=args.port,
            idle_timeout=args.idle_timeout,
            lazy=False,
        )
        print(f"[OK] Mesh daemon warm and running on http://{args.host}:{args.port} (PID: {res['pid']})")

    elif args.action == "stop":
        res = stop_daemon(host=args.host, port=args.port)
        if res["stopped"]:
            print(f"[OK] Stopped Mesh daemon (PID: {res['pid']})")
        else:
            print("Mesh daemon is not running.")

    elif args.action == "restart":
        stop_daemon(host=args.host, port=args.port)
        res = start_daemon(
            host=args.host,
            port=args.port,
            idle_timeout=args.idle_timeout,
            lazy=args.lazy,
        )
        print(f"[OK] Restarted Mesh daemon on http://{args.host}:{args.port} (PID: {res['pid']})")

    elif args.action == "unload":
        res = trigger_daemon_unload(host=args.host, port=args.port)
        if res is None:
            print("Mesh daemon is not running.")
        else:
            print("[OK] Unloaded neural model weights from RAM.")

    elif args.action == "install-startup":
        info = install_startup(
            host=args.host,
            port=args.port,
            idle_timeout=args.idle_timeout,
        )
        print(f"[OK] Registered Mesh daemon for OS login and started it immediately ({info['platform']})")
        print(f"     Location: {info['location']}")

    elif args.action == "remove-startup":
        info = remove_startup()
        if info["removed"]:
            print(f"[OK] Removed login auto-start registration ({info['platform']})")
        else:
            print("Login auto-start was not registered.")

    elif args.action == "status":
        health = get_daemon_health(host=args.host, port=args.port)
        auto_start = "enabled" if is_startup_installed() else "disabled"
        if not health:
            print(f"\nMesh Daemon: STOPPED (http://{args.host}:{args.port})")
            print(f"  Login auto-start: {auto_start}\n")
            return
        eng = health.get("engine") or {}
        loaded_str = "LOADED (warm)" if eng.get("model_loaded") else "SLEEPING (~0 MB model RAM)"
        print(f"\nMesh Daemon: RUNNING on http://{args.host}:{args.port}")
        print(f"  PID:              {health.get('pid')}")
        print(f"  Model state:      {loaded_str}")
        print(f"  Engine backend:   {eng.get('backend', 'unloaded')}")
        print(f"  ONNX cached:      {eng.get('onnx_cached_on_disk', False)}")
        print(f"  Login auto-start: {auto_start}\n")


if __name__ == "__main__":
    daemon_cmd()
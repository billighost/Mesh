"""
mesh/daemon.py — Background daemon, authentication token manager, and OS login auto-start manager.

Capabilities:
  - Interactive `y/n` user permission prompts before spawning background processes or modifying OS startup
  - Per-user 256-bit authentication token (`~/.mesh/auth_token`, created atomically with mode 0600)
  - Enforced 0700 `~/.mesh` directory and 0600 `daemon.log` permissions on POSIX
  - Process ownership verification before stopping PIDs
  - XML-escaped macOS LaunchAgent plist & shell-quoted Linux `.desktop` entries
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional
from xml.sax.saxutils import escape as xml_escape

from .audit import MESH_DIR, ensure_secure_mesh_dir, get_mesh_dir, secure_file_permissions


def _pid_file() -> Path:
    return get_mesh_dir() / "daemon.pid"


def _log_file() -> Path:
    return get_mesh_dir() / "daemon.log"


def _auth_token_file() -> Path:
    return get_mesh_dir() / "auth_token"


def _autostart_marker_file() -> Path:
    return get_mesh_dir() / ".autostart_registered"


DAEMON_PID_FILE = _pid_file()
DAEMON_LOG_FILE = _log_file()
AUTH_TOKEN_FILE = _auth_token_file()
AUTOSTART_MARKER_FILE = _autostart_marker_file()

DEFAULT_PORT = int(os.environ.get("MESH_HTTP_PORT", 7701))
DEFAULT_HOST = os.environ.get("MESH_HTTP_HOST", "127.0.0.1")
DEFAULT_IDLE_TIMEOUT = float(os.environ.get("MESH_IDLE_UNLOAD_SECONDS", "180"))

_WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_WIN_RUN_VALUE_NAME = "MeshContextDaemon"

_SPAWN_LOCK = threading.Lock()
_LAST_SPAWN_TS = 0.0

_TOKEN_LOCK = threading.Lock()
_CACHED_TOKEN: Optional[str] = None
_CACHED_TOKEN_PATH: Optional[Path] = None


def _write_private_file(path: Path, content: str) -> None:
    """
    Write `content` to `path` creating the file with 0600 permissions atomically from birth
    so sensitive tokens are never briefly world-readable or visible truncated to concurrent readers.
    """
    ensure_secure_mesh_dir(path.parent)
    tmp_path = path.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
    if os.name == "nt":
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, path)
        return

    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(str(tmp_path), flags, stat.S_IRUSR | stat.S_IWUSR)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
    except (AttributeError, OSError):
        pass
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    secure_file_permissions(tmp_path)
    os.replace(tmp_path, path)
    secure_file_permissions(path)


def _open_private_log_append(path: Path) -> Any:
    """Open `daemon.log` in append mode with 0600 permissions on POSIX."""
    ensure_secure_mesh_dir(path.parent)
    if os.name == "nt":
        return open(path, "a", encoding="utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    fd = os.open(str(path), flags, stat.S_IRUSR | stat.S_IWUSR)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
    except (AttributeError, OSError):
        pass
    return os.fdopen(fd, "a", encoding="utf-8")


def get_or_create_auth_token() -> str:
    """
    Return the local Mesh authentication token stored in `~/.mesh/auth_token` (mode 0600).
    Generates a new 256-bit token if none exists yet.
    """
    global _CACHED_TOKEN, _CACHED_TOKEN_PATH
    env_tok = os.environ.get("MESH_AUTH_TOKEN", "").strip()
    if env_tok:
        return env_tok

    token_file = _auth_token_file()
    if _CACHED_TOKEN and _CACHED_TOKEN_PATH == token_file and token_file.exists():
        return _CACHED_TOKEN

    with _TOKEN_LOCK:
        token_file = _auth_token_file()
        if _CACHED_TOKEN and _CACHED_TOKEN_PATH == token_file and token_file.exists():
            return _CACHED_TOKEN

        ensure_secure_mesh_dir(token_file.parent)
        if token_file.exists():
            try:
                secure_file_permissions(token_file)
                existing = token_file.read_text(encoding="utf-8").strip()
                if len(existing) >= 16:
                    _CACHED_TOKEN = existing
                    _CACHED_TOKEN_PATH = token_file
                    return existing
            except OSError:
                pass

        token = secrets.token_urlsafe(32)
        if os.name != "nt":
            try:
                fd = os.open(str(token_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
                try:
                    os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
                except (AttributeError, OSError):
                    pass
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(token)
                secure_file_permissions(token_file)
                _CACHED_TOKEN = token
                _CACHED_TOKEN_PATH = token_file
                return token
            except FileExistsError:
                try:
                    secure_file_permissions(token_file)
                    existing = token_file.read_text(encoding="utf-8").strip()
                    if len(existing) >= 16:
                        _CACHED_TOKEN = existing
                        _CACHED_TOKEN_PATH = token_file
                        return existing
                except OSError:
                    pass
            except OSError:
                pass
        else:
            try:
                _write_private_file(token_file, token)
            except OSError:
                pass
            if token_file.exists():
                try:
                    existing = token_file.read_text(encoding="utf-8").strip()
                    if len(existing) >= 16:
                        _CACHED_TOKEN = existing
                        _CACHED_TOKEN_PATH = token_file
                        return existing
                except OSError:
                    pass

        _CACHED_TOKEN = token
        _CACHED_TOKEN_PATH = token_file
        return token


def get_auth_headers() -> dict[str, str]:
    """Return HTTP headers containing the local Mesh bearer token."""
    tok = get_or_create_auth_token()
    return {
        "Authorization": f"Bearer {tok}",
        "X-Mesh-Token": tok,
    }


def request_user_permission(action_desc: str, assume_yes: bool = False) -> bool:
    """
    Prompt the user with a `y/n` question before starting a background process,
    downloading model weights, modifying shell config files, or calling external cloud APIs.
    Returns True if approved, or False in non-interactive contexts.
    """
    if assume_yes or os.environ.get("MESH_AUTO_APPROVE", "").strip().lower() in ("1", "true", "yes"):
        return True
    if os.environ.get("MESH_NON_INTERACTIVE", "").strip().lower() in ("1", "true", "yes"):
        return False
    if not (
        hasattr(sys.stdin, "isatty")
        and sys.stdin.isatty()
        and hasattr(sys.stdout, "isatty")
        and sys.stdout.isatty()
    ):
        return False
    try:
        ans = input(f"[Mesh] Allow Mesh to {action_desc}? (y/n): ").strip().lower()
        return ans in ("y", "yes")
    except (EOFError, KeyboardInterrupt, OSError):
        print()
        return False


def _health_url(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> str:
    return f"http://{host}:{port}/health"


def get_daemon_health(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    timeout: float = 0.6,
) -> Optional[dict[str, Any]]:
    """Query the local daemon `/health` endpoint. Returns the JSON dict if alive, else None."""
    try:
        req = urllib.request.Request(
            _health_url(host, port),
            headers={"Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, dict) and data.get("status") == "ok" and isinstance(data.get("engine"), dict):
                    return data
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        pass
    return None


def trigger_daemon_unload(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> Optional[dict[str, Any]]:
    """Ask the running daemon to unload the neural model from RAM immediately."""
    try:
        headers = {"Content-Type": "application/json", **get_auth_headers()}
        req = urllib.request.Request(
            f"http://{host}:{port}/unload",
            data=b"{}",
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        pass
    return None


def _read_pid_file() -> Optional[int]:
    pid_file = _pid_file()
    if not pid_file.exists():
        return None
    try:
        raw = pid_file.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def _write_pid_file(pid: int) -> None:
    try:
        _write_private_file(_pid_file(), str(pid))
    except OSError:
        pass


def _remove_pid_file() -> None:
    try:
        _pid_file().unlink(missing_ok=True)
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


def _is_mesh_daemon_process(pid: int) -> bool:
    """
    Verify that `pid` is actually a Mesh daemon / Python http_server process
    before attempting to terminate it.
    """
    if not _is_pid_alive(pid):
        return False
    if os.name == "nt":
        try:
            out = subprocess.check_output(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}').CommandLine",
                ],
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                timeout=2.0,
            ).decode("utf-8", errors="ignore").lower()
            if out.strip():
                return "mesh" in out or "http_server" in out
            tl = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                timeout=2.0,
            ).decode("utf-8", errors="ignore").lower()
            return "python" in tl or "mesh" in tl
        except Exception:
            return False

    proc_cmdline = Path(f"/proc/{pid}/cmdline")
    if proc_cmdline.exists():
        try:
            raw = proc_cmdline.read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="ignore").lower()
            return "mesh" in raw or "http_server" in raw
        except OSError:
            pass
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            stderr=subprocess.DEVNULL,
            timeout=2.0,
        ).decode("utf-8", errors="ignore").lower()
        return "mesh" in out or "http_server" in out
    except Exception:
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
    interactive_prompt: bool = True,
    assume_yes: bool = False,
) -> dict[str, Any]:
    """
    Ensure the Mesh background HTTP daemon is running.
    Asks the user for `y/n` permission before spawning a background process unless
    already running or `assume_yes=True` / `MESH_AUTO_APPROVE=1`.
    """
    global _LAST_SPAWN_TS
    if os.environ.get("MESH_IN_DAEMON", "").strip().lower() in ("1", "true", "yes"):
        return {
            "running": True,
            "already_running": True,
            "pid": os.getpid(),
            "host": host,
            "port": port,
        }
    if os.environ.get("MESH_NO_AUTO_DAEMON", "").strip().lower() in ("1", "true", "yes"):
        return {"running": False, "declined": True, "host": host, "port": port}

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

    if interactive_prompt and not request_user_permission(
        f"start the background Mesh HTTP daemon on {host}:{port}",
        assume_yes=assume_yes,
    ):
        return {"running": False, "declined": True, "host": host, "port": port}

    return start_daemon(
        host=host,
        port=port,
        idle_timeout=idle_timeout,
        lazy=False,
        wait=wait,
        interactive_prompt=False,
        assume_yes=True,
    )


def start_daemon(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    lazy: bool = False,
    wait: bool = True,
    interactive_prompt: bool = False,
    assume_yes: bool = False,
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

    if interactive_prompt and not request_user_permission(
        f"start the background Mesh HTTP daemon on {host}:{port}",
        assume_yes=assume_yes,
    ):
        return {"running": False, "declined": True, "pid": None, "host": host, "port": port}

    ensure_secure_mesh_dir()
    get_or_create_auth_token()
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
    if assume_yes and not lazy:
        env["MESH_ALLOW_DAEMON_DOWNLOAD"] = "1"

    log_handle = _open_private_log_append(_log_file())
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
        time.sleep(0.05)
        alive = proc.poll() is None
        return {
            "running": alive,
            "already_running": False,
            "spawning": alive,
            "pid": proc.pid if alive else None,
            "host": host,
            "port": port,
            "health": None,
        }

    health: Optional[dict[str, Any]] = None
    for i in range(40):
        if proc.poll() is not None:
            break
        time.sleep(0.04 if i < 10 else 0.12)
        health = get_daemon_health(host=host, port=port, timeout=0.35)
        if health is not None:
            break

    if health is None:
        _remove_pid_file()

    return {
        "running": health is not None,
        "already_running": False,
        "pid": proc.pid if health is not None else None,
        "host": host,
        "port": port,
        "health": health,
    }


def stop_daemon(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> dict[str, Any]:
    """Stop the running Mesh daemon after verifying process ownership."""
    health = get_daemon_health(host=host, port=port, timeout=0.5)
    pid_from_health = int(health["pid"]) if (health and health.get("pid")) else None
    pid_from_file = _read_pid_file()
    pid = pid_from_health or pid_from_file

    if not pid:
        _remove_pid_file()
        return {"stopped": False, "reason": "not_running", "pid": None}

    pid = int(pid)
    if not _is_mesh_daemon_process(pid):
        _remove_pid_file()
        return {"stopped": False, "reason": "pid_not_mesh", "pid": pid}

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
            if _is_pid_alive(pid) and _is_mesh_daemon_process(pid):
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
    interactive_prompt: bool = False,
    assume_yes: bool = False,
) -> dict[str, Any]:
    """Register the Mesh daemon for OS login auto-start (with XML/shell escaping)."""
    if interactive_prompt and not request_user_permission(
        "register the Mesh daemon for OS login auto-start",
        assume_yes=assume_yes,
    ):
        return {"installed": False, "declined": True, "platform": sys.platform, "location": ""}

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
        xml_attr_esc = {'"': "&quot;", "'": "&apos;"}
        args_xml = "\n".join(
            f"        <string>{xml_escape(str(a), xml_attr_esc)}</string>" for a in cmd_args
        )
        escaped_log = xml_escape(str(_log_file()), xml_attr_esc)
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
    <string>{escaped_log}</string>
    <key>StandardErrorPath</key>
    <string>{escaped_log}</string>
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
        exec_line = " ".join(shlex.quote(str(a)) for a in cmd_args)
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

    try:
        _write_private_file(_autostart_marker_file(), "1")
    except OSError:
        pass

    daemon_res = None
    if start_now:
        daemon_res = start_daemon(host=host, port=port, idle_timeout=idle_timeout, lazy=True)

    return {
        "installed": True,
        "platform": platform_name,
        "location": loc,
        "command": " ".join(shlex.quote(str(a)) for a in cmd_args),
        "daemon": daemon_res,
    }


def remove_startup() -> dict[str, Any]:
    """Remove login auto-start registration."""
    try:
        _autostart_marker_file().unlink(missing_ok=True)
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
    parser.add_argument("--yes", "-y", action="store_true", help="Skip interactive y/n permission prompts")
    args = parser.parse_args()

    if args.action == "start":
        if not request_user_permission(
            f"start the background Mesh HTTP daemon on {args.host}:{args.port}",
            assume_yes=args.yes,
        ):
            print("Aborted starting background daemon.")
            return
        from .memory import _has_local_onnx_pair, ensure_model_downloaded

        if _has_local_onnx_pair() is None and request_user_permission(
            "download the ONNX embedding model in the background",
            assume_yes=args.yes,
        ):
            ensure_model_downloaded(blocking=False, assume_yes=True)
        res = start_daemon(
            host=args.host,
            port=args.port,
            idle_timeout=args.idle_timeout,
            lazy=args.lazy,
        )
        if not res.get("running"):
            print(
                f"[ERROR] Failed to start Mesh daemon on http://{args.host}:{args.port}. "
                f"Check {_log_file()} for details.",
                file=sys.stderr,
            )
            sys.exit(1)
        if res.get("already_running"):
            print(f"[OK] Mesh daemon already running on http://{args.host}:{args.port} (PID: {res['pid']})")
        else:
            print(f"[OK] Started Mesh daemon on http://{args.host}:{args.port} (PID: {res['pid']})")

    elif args.action == "warmup":
        if not request_user_permission(
            f"download the ONNX model and start the background Mesh daemon on {args.host}:{args.port}",
            assume_yes=args.yes,
        ):
            print("Aborted warmup.")
            return
        from .memory import ensure_model_downloaded, warmup_model

        print("[Mesh] Ensuring local ONNX embedding model is downloaded...")
        pair = ensure_model_downloaded(blocking=True, timeout=60.0, assume_yes=True)
        if pair:
            print(f"[OK] ONNX model cached at {pair[0].parent}")
        warmup_model(assume_yes=True)
        res = start_daemon(
            host=args.host,
            port=args.port,
            idle_timeout=args.idle_timeout,
            lazy=False,
        )
        if not res.get("running"):
            print(
                f"[ERROR] Failed to start Mesh daemon on http://{args.host}:{args.port}. "
                f"Check {_log_file()} for details.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"[OK] Mesh daemon warm and running on http://{args.host}:{args.port} (PID: {res['pid']})")

    elif args.action == "stop":
        res = stop_daemon(host=args.host, port=args.port)
        if res["stopped"]:
            print(f"[OK] Stopped Mesh daemon (PID: {res['pid']})")
        elif res.get("reason") == "pid_not_mesh":
            print(f"Removed stale PID file (PID {res['pid']} is not a Mesh daemon).")
        else:
            print("Mesh daemon is not running.")

    elif args.action == "restart":
        if not request_user_permission(
            f"restart the background Mesh HTTP daemon on {args.host}:{args.port}",
            assume_yes=args.yes,
        ):
            print("Aborted restart.")
            return
        stop_daemon(host=args.host, port=args.port)
        res = start_daemon(
            host=args.host,
            port=args.port,
            idle_timeout=args.idle_timeout,
            lazy=args.lazy,
        )
        if not res.get("running"):
            print(
                f"[ERROR] Failed to restart Mesh daemon on http://{args.host}:{args.port}. "
                f"Check {_log_file()} for details.",
                file=sys.stderr,
            )
            sys.exit(1)
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
            interactive_prompt=True,
            assume_yes=args.yes,
        )
        if info.get("declined"):
            print("Aborted OS login auto-start registration.")
            return
        daemon_res = info.get("daemon") or {}
        if daemon_res.get("running"):
            print(f"[OK] Registered Mesh daemon for OS login and started it immediately ({info['platform']})")
        else:
            print(f"[OK] Registered Mesh daemon for OS login ({info['platform']})")
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
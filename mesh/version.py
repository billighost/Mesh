"""
mesh/version.py — Package version metadata and opt-in PyPI update checker.

Privacy & Consent:
  - Never phones home to PyPI by default.
  - Only checks PyPI when explicitly requested via `force=True` (e.g. `--check-updates`)
    or when opted in via `MESH_CHECK_UPDATES=1`.
"""
from __future__ import annotations

import os
import re
import sys
import threading
from typing import Optional

VERSION = "0.6.1"
PACKAGE_NAME = "mesh-context-layer"

_VERSION_PART_RE = re.compile(r"\d+")


def parse_version_tuple(version_str: str) -> tuple[int, ...]:
    """Parse a semantic version string (e.g. '0.6.1') into a comparable integer tuple."""
    if not version_str or not isinstance(version_str, str):
        return (0, 0, 0)
    parts = [int(m) for m in _VERSION_PART_RE.findall(version_str)[:4]]
    return tuple(parts) if parts else (0, 0, 0)


def is_version_compatible(file_version: Optional[str], current_version: str = VERSION) -> bool:
    """
    Return True if an exported file's `mesh_version` is compatible with `current_version`.
    Files from newer major versions (or newer 0.x minor versions) return False so callers
    can warn or reject incompatible schemas.
    """
    if not file_version:
        return True
    file_tup = parse_version_tuple(str(file_version))
    curr_tup = parse_version_tuple(current_version)
    if file_tup[0] > curr_tup[0]:
        return False
    if file_tup[0] == 0 and len(file_tup) > 1 and len(curr_tup) > 1 and file_tup[1] > curr_tup[1]:
        return False
    return True


def get_latest_version(timeout: float = 1.5) -> Optional[str]:
    """Query PyPI for the latest published version of `mesh-context-layer`."""
    try:
        import httpx

        url = f"https://pypi.org/pypi/{PACKAGE_NAME}/json"
        response = httpx.get(
            url,
            timeout=timeout,
            headers={"User-Agent": f"{PACKAGE_NAME}/{VERSION}"},
        )
        if response.status_code == 200:
            data = response.json()
            return str(data["info"]["version"]).strip()
    except Exception:
        pass
    return None


def _run_check() -> Optional[str]:
    if os.environ.get("MESH_NO_UPDATE_CHECK", "").strip().lower() in ("1", "true", "yes"):
        return None

    latest = get_latest_version()
    if not latest:
        return None

    try:
        if parse_version_tuple(latest) > parse_version_tuple(VERSION):
            print(
                f"\n[UPDATE] A new version of Mesh is available: {latest} (you have {VERSION})\n"
                f"         Run 'pip install --upgrade {PACKAGE_NAME}' to update.\n",
                file=sys.stderr,
            )
    except Exception:
        if latest != VERSION:
            print(
                f"\n[UPDATE] Version {latest} is available on PyPI (you have {VERSION})\n",
                file=sys.stderr,
            )
    return latest


def check_for_updates(force: bool = False, blocking: bool = False) -> Optional[str]:
    """
    Check PyPI for updates ONLY if `force=True` or `MESH_CHECK_UPDATES=1` is set.
    Never performs background network calls unless explicitly enabled.
    Runs synchronously if `blocking=True` or `force=True`.
    """
    if os.environ.get("MESH_NO_UPDATE_CHECK", "").strip().lower() in ("1", "true", "yes"):
        return None

    opt_in = os.environ.get("MESH_CHECK_UPDATES", "").strip().lower() in ("1", "true", "yes")
    if not force and not opt_in:
        return None

    if blocking or force:
        return _run_check()
    else:
        thread = threading.Thread(target=_run_check, name="mesh-update-check", daemon=True)
        thread.start()
        return None
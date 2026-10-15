"""
mesh/version.py — Package version metadata and non-blocking PyPI update checker.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Optional

VERSION = "0.5.0"
PACKAGE_NAME = "mesh-context-layer"


def get_latest_version() -> Optional[str]:
    """Check PyPI for the latest version of the package."""
    try:
        import httpx

        url = f"https://pypi.org/pypi/{PACKAGE_NAME}/json"
        response = httpx.get(url, timeout=1.5)
        if response.status_code == 200:
            return str(response.json()["info"]["version"])
    except Exception:
        pass
    return None


def _run_check() -> None:
    if os.environ.get("MESH_NO_UPDATE_CHECK", "").strip().lower() in ("1", "true", "yes"):
        return

    latest = get_latest_version()
    if not latest:
        return

    try:
        l_parts = [int(p) for p in latest.split(".")]
        v_parts = [int(p) for p in VERSION.split(".")]
        if l_parts > v_parts:
            print(
                f"\n[UPDATE] A new version of Mesh is available: {latest} (you have {VERSION})\n"
                f"         Run 'pip install --upgrade {PACKAGE_NAME}' to update.\n",
                file=sys.stderr,
            )
    except (ValueError, IndexError):
        if latest != VERSION:
            print(
                f"\n[UPDATE] Version {latest} is available on PyPI (you have {VERSION})\n",
                file=sys.stderr,
            )


def check_for_updates() -> None:
    """Check for updates in a background daemon thread without blocking CLI startup."""
    thread = threading.Thread(target=_run_check, daemon=True)
    thread.start()
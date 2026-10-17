"""
mesh/version.py — Package version metadata, PEP 440 version comparison, and opt-in update checker.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import urllib.request
from typing import Optional

VERSION = "0.6.3"

_PEP440_RE = re.compile(
    r"^\s*v?(?P<release>\d+(?:\.\d+)*)"
    r"(?:[-._]?(?P<pre_type>a|alpha|b|beta|c|rc|pre|preview)[-._]?(?P<pre_num>\d+)?)?"
    r"(?:[-._]?(?:post|rev|r)[-._]?(?P<post_num>\d+)?)?"
    r"(?:[-._]?dev[-._]?(?P<dev_num>\d+)?)?"
    r"(?:\+[A-Za-z0-9._-]+)?\s*$",
    re.IGNORECASE,
)

_PRE_TYPE_RANK = {
    "a": -3,
    "alpha": -3,
    "b": -2,
    "beta": -2,
    "c": -1,
    "rc": -1,
    "pre": -1,
    "preview": -1,
}

_UPDATE_CHECK_DONE = False
_UPDATE_LOCK = threading.Lock()


def parse_version_tuple(version_str: Optional[str]) -> tuple[int, ...]:
    """
    Parse a version string into a PEP 440-aware comparable integer tuple:
      `(major, minor, patch, pre_rank, pre_num, post_rank, post_num, dev_rank, dev_num)`

    Sorting order guarantees:
      `0.6.2.dev1 < 0.6.2a1 < 0.6.2b1 < 0.6.2rc1 < 0.6.2 < 0.6.2.post1 < 0.6.3.dev1 < 0.6.3 < 0.7.0 < 1.0.0`
    The first three elements are always `(major, minor, patch)`.
    """
    if not version_str or not isinstance(version_str, str):
        return (0, 0, 0, 0, 0, 0, 0, 0, 0)

    m = _PEP440_RE.match(version_str)
    if not m:
        nums = [int(x) for x in re.findall(r"\d+", version_str)[:3]]
        while len(nums) < 3:
            nums.append(0)
        return (nums[0], nums[1], nums[2], 0, 0, 0, 0, 0, 0)

    rel_parts = [int(x) for x in m.group("release").split(".")[:3]]
    while len(rel_parts) < 3:
        rel_parts.append(0)
    major, minor, patch = rel_parts[0], rel_parts[1], rel_parts[2]

    pre_type = (m.group("pre_type") or "").lower()
    pre_num_raw = m.group("pre_num")
    post_Spec = m.group("post_num")
    has_post = bool(re.search(r"(?:post|rev|r)", version_str, re.IGNORECASE)) and not pre_type.startswith("pr")
    has_dev = bool(re.search(r"dev", version_str, re.IGNORECASE))
    dev_num_raw = m.group("dev_num")

    if pre_type:
        pre_rank = _PRE_TYPE_RANK.get(pre_type, -1)
        pre_num = int(pre_num_raw) if pre_num_raw is not None else 0
    elif has_dev and not has_post:
        # Pure `.devN` release sorts before alpha (`-3`)
        pre_rank = -4
        pre_num = 0
    else:
        pre_rank = 0
        pre_num = 0

    if has_post:
        post_rank = 1
        post_num = int(post_Spec) if post_Spec is not None else 0
    else:
        post_rank = 0
        post_num = 0

    if has_dev:
        dev_rank = -1
        dev_num = int(dev_num_raw) if dev_num_raw is not None else 0
    else:
        dev_rank = 0
        dev_num = 0

    return (major, minor, patch, pre_rank, pre_num, post_rank, post_num, dev_rank, dev_num)


def is_version_compatible(file_version: Optional[str], current_version: str = VERSION) -> bool:
    """
    Verify export schema compatibility using the release segment `(major, minor, patch)`.
    - Missing/empty file_version is rejected.
    - Major versions must match (`0.x` vs `0.y`, or `1.x` vs `1.y`).
    - During `0.x`, file minor version cannot exceed current minor version (e.g. `0.7.0` file on `0.6.3` fails).
    """
    if not file_version or not isinstance(file_version, str) or not file_version.strip():
        return False
    if not re.search(r"\d", file_version):
        return False
    f_maj, f_min, _ = parse_version_tuple(file_version)[:3]
    c_maj, c_min, _ = parse_version_tuple(current_version)[:3]
    if f_maj != c_maj:
        return False
    if c_maj == 0 and f_min > c_min:
        return False
    return True


def check_for_updates(force: bool = False, blocking: bool = False) -> Optional[str]:
    """
    Strictly opt-in PyPI update check (`force=True` or `MESH_CHECK_UPDATES=1`).
    Never contacts PyPI when `MESH_NO_UPDATE_CHECK=1` or by default.
    """
    global _UPDATE_CHECK_DONE
    if os.environ.get("MESH_NO_UPDATE_CHECK", "").strip().lower() in ("1", "true", "yes"):
        return None
    opt_in = force or os.environ.get("MESH_CHECK_UPDATES", "").strip().lower() in ("1", "true", "yes")
    if not opt_in:
        return None
    with _UPDATE_LOCK:
        if _UPDATE_CHECK_DONE and not force:
            return None
        _UPDATE_CHECK_DONE = True

    def _fetch() -> Optional[str]:
        try:
            req = urllib.request.Request(
                "https://pypi.org/pypi/mesh-context-layer/json",
                headers={"Accept": "application/json", "User-Agent": f"mesh-context-layer/{VERSION}"},
            )
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    latest = str(data.get("info", {}).get("version", "")).strip()
                    if latest and parse_version_tuple(latest) > parse_version_tuple(VERSION):
                        sys.stderr.write(
                            f"[Mesh] Update available: v{VERSION} -> v{latest} "
                            "(run `pip install -U mesh-context-layer`)\n"
                        )
                        return latest
        except Exception:
            pass
        return None

    if blocking:
        return _fetch()
    threading.Thread(target=_fetch, name="mesh-update-check", daemon=True).start()
    return None
"""
mesh/native_cli.py — Automatic compilation helper and launcher for the native C client.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional


def _get_project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def get_native_binary_path() -> Optional[Path]:
    root = _get_project_root()
    ext = ".exe" if os.name == "nt" else ""
    candidate = root / "c_cli" / f"mesh_fast{ext}"
    if candidate.exists() and candidate.is_file():
        return candidate
    return None


def compile_native_cli(force: bool = False) -> Optional[Path]:
    """Compile c_cli/mesh_fast.c into a native binary if a C compiler is available."""
    existing = get_native_binary_path()
    if existing is not None and not force:
        return existing

    root = _get_project_root()
    src = root / "c_cli" / "mesh_fast.c"
    if not src.exists():
        return None

    ext = ".exe" if os.name == "nt" else ""
    out = root / "c_cli" / f"mesh_fast{ext}"

    # Try gcc, clang, or cc
    compilers = ["gcc", "clang", "cc"]
    for comp in compilers:
        comp_path = shutil.which(comp)
        if comp_path:
            cmd = [comp_path, "-O3", "-Wall", "-Wextra", "-Wno-unknown-pragmas", str(src), "-o", str(out)]
            if os.name == "nt":
                cmd.extend(["-lws2_32", "-lbcrypt", "-lshell32"])
            try:
                res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                if res.returncode == 0 and out.exists():
                    return out
            except Exception:
                continue

    # Try MSVC cl if on Windows
    if os.name == "nt":
        cl_path = shutil.which("cl")
        if cl_path:
            cmd = [cl_path, "/O2", "/nologo", str(src), f"/Fe:{out}", "ws2_32.lib", "bcrypt.lib", "shell32.lib"]
            try:
                res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                if res.returncode == 0 and out.exists():
                    return out
            except Exception:
                pass

    return None


def run_native_cli(argv: list[str]) -> Optional[int]:
    """Execute the native client if built; returns its exit code or None if unavailable."""
    bin_path = get_native_binary_path() or compile_native_cli()
    if bin_path is None:
        return None
    try:
        res = subprocess.run([str(bin_path)] + argv, check=False)
        return res.returncode
    except Exception:
        return None

"""
scripts/build_c_core.py — Build mesh/_c_core native shared library.

Usage:
    python scripts/build_c_core.py

Tries (in order):
  1. Python's own MSVC toolchain (Windows, via setuptools)
  2. x86_64-w64-mingw32-gcc (64-bit MinGW cross-compiler)
  3. gcc with -m64 flag
  4. Prints instructions for manual build
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
SRC = ROOT / "c_core" / "mesh_core.c"
MESH_DIR = ROOT / "mesh"

if sys.platform == "win32":
    OUT = MESH_DIR / "_c_core.dll"
elif sys.platform == "darwin":
    OUT = MESH_DIR / "_c_core.dylib"
else:
    OUT = MESH_DIR / "_c_core.so"


def try_setuptools_msvc() -> bool:
    """Try to compile using Python's own MSVC toolchain (Windows only)."""
    if sys.platform != "win32":
        return False
    try:
        from distutils.core import Distribution
        from distutils.extension import Extension
        from distutils.command.build_ext import build_ext

        ext = Extension(
            "_c_core",
            sources=[str(SRC)],
            extra_compile_args=["/O2", "/arch:AVX2", "/D__AVX2__", "/D__FMA__"],
        )
        dist = Distribution(attrs={"name": "_c_core", "ext_modules": [ext]})
        cmd = build_ext(dist)
        cmd.build_lib = str(MESH_DIR)
        cmd.build_temp = str(ROOT / "build" / "tmp")
        cmd.ensure_finalized()
        cmd.run()
        # The pyd file needs to be renamed/copied to _c_core.dll
        import glob
        pyd_files = glob.glob(str(MESH_DIR / "_c_core*.pyd"))
        if pyd_files:
            shutil.copy2(pyd_files[0], OUT)
            print(f"  Built via MSVC: {OUT}")
            return True
    except Exception as exc:
        print(f"  MSVC build failed: {exc}")
    return False


def try_gcc(gcc_cmd: str, extra_flags: list[str]) -> bool:
    """Try to compile with a specific gcc command."""
    if not shutil.which(gcc_cmd):
        return False
    try:
        if sys.platform == "darwin":
            shared_flag = ["-dynamiclib"]
        else:
            shared_flag = ["-shared"]

        cmd = [
            gcc_cmd, "-O3", "-mavx2", "-mfma",
            *shared_flag, "-fPIC",
            "-o", str(OUT),
            str(SRC), "-lm",
            *extra_flags,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"  Built via {gcc_cmd}: {OUT}")
            return True
        else:
            print(f"  {gcc_cmd} failed: {result.stderr[:200]}")
    except Exception as exc:
        print(f"  {gcc_cmd} error: {exc}")
    return False


def main() -> None:
    print(f"Building {OUT} from {SRC}...")

    if not SRC.exists():
        print(f"ERROR: Source file not found: {SRC}")
        sys.exit(1)

    MESH_DIR.mkdir(exist_ok=True)

    # Try compilation strategies in priority order
    strategies = [
        (try_setuptools_msvc, []),
        (try_gcc, ["x86_64-w64-mingw32-gcc", []]),
        (try_gcc, ["gcc", ["-m64"]]),
        (try_gcc, ["gcc", []]),
        (try_gcc, ["cc", []]),
    ]

    for fn, args in strategies:
        if callable(fn) and not args:
            if fn():
                print("Build successful!")
                return
        elif fn is try_gcc:
            if try_gcc(args[0], args[1]):
                print("Build successful!")
                return

    print()
    print("Could not build native library automatically.")
    print("The Python fallback in mesh/c_bridge.py will be used.")
    print()
    print("To build manually on Linux/macOS:")
    print("  gcc -O3 -mavx2 -mfma -shared -fPIC -o mesh/_c_core.so c_core/mesh_core.c -lm")
    print()
    print("To build manually on Windows (64-bit MinGW):")
    print("  x86_64-w64-mingw32-gcc -O3 -mavx2 -mfma -shared -o mesh/_c_core.dll c_core/mesh_core.c")


if __name__ == "__main__":
    main()

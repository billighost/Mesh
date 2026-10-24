"""
mesh/c_bridge.py - ctypes bridge to the native core (mesh/_c_core.dll / .so / .dylib).

Public API (every function has a pure-Python/NumPy fallback, so the package works
without the native library):

  c_guard_prescan(text) -> bool            True  = provably clean ASCII, skip the regex guards
  c_hash_embed_384(text) -> ndarray | None 384-d hash embedding, None = "use the Python path"
  c_cosine_batch_384(matrix, query)        n dot products (unit vectors => cosine)
  c_compute_decay_batch(...)               vectorised TTL decay / staleness
  is_native_available() / native_info()

Build (see build_and_verify.py, which does this for you):
    Windows (MinGW/MSYS2):  gcc -O3 -std=c11 -shared -static-libgcc -o mesh/_c_core.dll c_core/mesh_core.c -lm
    Linux / macOS:          gcc -O3 -std=c11 -shared -fPIC -o mesh/_c_core.so c_core/mesh_core.c -lm

A library older than ABI version 2 is rejected on purpose: earlier builds had a
prescan that could wave through secrets such as "the admin key is ..." unredacted.
Set MESH_DISABLE_NATIVE=1 to force the Python fallbacks, MESH_C_CORE_PATH to point
at a specific library file.
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
from pathlib import Path
from typing import Optional

import numpy as np

_REQUIRED_ABI = 2
_LIB: Optional[ctypes.CDLL] = None
_LIB_LOAD_LOCK = threading.Lock()
_LIB_LOAD_ATTEMPTED = False
_LIB_PATH: Optional[Path] = None
_HAS_AVX2 = False


def _candidate_paths() -> list[Path]:
    override = (os.environ.get("MESH_C_CORE_PATH") or "").strip()
    if override:
        return [Path(override)]
    mesh_dir = Path(__file__).parent
    if sys.platform == "win32":
        names = ["_c_core.dll"]
    elif sys.platform == "darwin":
        names = ["_c_core.dylib", "_c_core.so"]
    else:
        names = ["_c_core.so"]
    return [mesh_dir / n for n in names]


def _load_lib() -> Optional[ctypes.CDLL]:
    global _LIB, _LIB_LOAD_ATTEMPTED, _LIB_PATH, _HAS_AVX2
    with _LIB_LOAD_LOCK:
        if _LIB_LOAD_ATTEMPTED:
            return _LIB
        _LIB_LOAD_ATTEMPTED = True
        if os.environ.get("MESH_DISABLE_NATIVE", "").strip().lower() in ("1", "true", "yes"):
            return None
        for path in _candidate_paths():
            if not path.exists():
                continue
            try:
                lib = ctypes.CDLL(str(path))
                lib.mesh_c_version.argtypes = []
                lib.mesh_c_version.restype = ctypes.c_int
                if int(lib.mesh_c_version()) < _REQUIRED_ABI:
                    continue

                lib.mesh_c_cpu_has_avx2.argtypes = []
                lib.mesh_c_cpu_has_avx2.restype = ctypes.c_int

                lib.mesh_c_guard_prescan.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
                lib.mesh_c_guard_prescan.restype = ctypes.c_int

                lib.mesh_c_hash_embed_384.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p]
                lib.mesh_c_hash_embed_384.restype = ctypes.c_int

                lib.mesh_c_cosine_batch_384.argtypes = [
                    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                ]
                lib.mesh_c_cosine_batch_384.restype = None

                lib.mesh_c_compute_decay_batch.argtypes = [
                    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                    ctypes.c_double, ctypes.c_float, ctypes.c_void_p, ctypes.c_void_p,
                ]
                lib.mesh_c_compute_decay_batch.restype = None

                _HAS_AVX2 = bool(lib.mesh_c_cpu_has_avx2())
                _LIB = lib
                _LIB_PATH = path
                return lib
            except Exception:
                continue
        return None


_load_lib()


# ---------------------------------------------------------------------------
# Guard prescan
# ---------------------------------------------------------------------------
# Must stay identical to PRESCAN_TRIGGERS in c_core/mesh_core.c (build_and_verify.py
# fuzzes both implementations against each other and against the real regexes).
_PRESCAN_TRIGGERS: tuple[str, ...] = (
    "sk-", "ghp", "gho", "ghu", "ghs", "ghr", "github", "glpat", "akia", "asia", "aiza", "xox",
    "eyj", "pypi", "sg.", "npm", "hf_", "rk_", "demo_fake", "demo", "password", "passwd",
    "passphrase", "passcode", "pwd", "pin", "secret", "token", "api_key", "apikey", "api key",
    "api-key", "key", "phrase", "mnemonic", "seed", "credential", "private", "code", "auth",
    "bearer", "basic", "hunter2", "vault", "locker", "login", "recovery", "aws", "ignor",
    "disregard", "forget", "override", "bypass", "system", "inst", "instruct", "prompt",
    "directive", "guideline", "constraint", "guardrail", "policies", "follow", "obey",
    "adhere", "mode", "jailbreak", "unrestricted", "developer", "mesh", "context", "important",
    "critical", "urgent", "anweisung", "instrucc", "regeln", "reglas", "regles", "instruc",
    "dan",
)

_PRESCAN_STRUCTURAL = ("<", "=", "_", "*", "`", "~", "[", "]", "--", "://")


def _py_guard_prescan(text: str) -> bool:
    """Pure-Python prescan, semantically identical to mesh_c_guard_prescan."""
    if not text or len(text) > 16384:
        return False
    for ch in text:
        c = ord(ch)
        if (c < 32 and c != 10 and c != 9) or c >= 127:
            return False
    for s in _PRESCAN_STRUCTURAL:
        if s in text:
            return False
    low = text.lower()
    for trigger in _PRESCAN_TRIGGERS:
        if trigger in low:
            return False
    return True


def c_guard_prescan(text: str) -> bool:
    """True = text is provably clean ASCII (skip the regex guards); False = run the full pipeline."""
    lib = _LIB
    if lib is not None:
        try:
            if not text.isascii():
                return False
            return lib.mesh_c_guard_prescan(text.encode("ascii"), len(text)) == 0
        except Exception:
            pass
    return _py_guard_prescan(text)


# ---------------------------------------------------------------------------
# Hash embedding
# ---------------------------------------------------------------------------
def c_hash_embed_384(text: str) -> Optional[np.ndarray]:
    """Native memory._hash_embed_384; returns None when the caller must use the Python path."""
    lib = _LIB
    if lib is None:
        return None
    try:
        if not text.isascii() or len(text) > 8192:
            return None
        out = np.empty(384, dtype=np.float32)
        rc = lib.mesh_c_hash_embed_384(text.encode("ascii"), len(text), out.ctypes.data)
        if rc != 0:
            return None
        return out
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Cosine / decay
# ---------------------------------------------------------------------------
def c_cosine_batch_384(matrix: np.ndarray, query: np.ndarray) -> np.ndarray:
    """Dot product of every row of `matrix` (N x 384 float32) with `query` (384 float32)."""
    n = int(matrix.shape[0])
    if n == 0:
        return np.empty(0, dtype=np.float32)
    mat_c = matrix if (matrix.dtype == np.float32 and matrix.flags.c_contiguous) else np.ascontiguousarray(matrix, dtype=np.float32)
    q_flat = query.reshape(-1)
    q_c = q_flat if (q_flat.dtype == np.float32 and q_flat.flags.c_contiguous and q_flat.shape[0] == 384) else np.ascontiguousarray(q_flat[:384], dtype=np.float32)
    lib = _LIB
    if lib is not None and mat_c.shape[1] == 384 and q_c.shape[0] == 384:
        out = np.empty(n, dtype=np.float32)
        try:
            lib.mesh_c_cosine_batch_384(mat_c.ctypes.data, q_c.ctypes.data, n, out.ctypes.data)
            return out
        except Exception:
            pass
    return mat_c @ q_c


def c_compute_decay_batch(
    confidences: np.ndarray,
    ttl_days: np.ndarray,
    decay_starts: np.ndarray,
    now_epoch: float,
    stale_threshold: float,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Vectorised TTL decay. `ttl_days` / `decay_starts` use NaN for "no TTL" / "unknown start".
    Returns (effective_confidence float32[n], is_stale bool[n]).
    """
    conf = np.ascontiguousarray(confidences, dtype=np.float32)
    ttl = np.ascontiguousarray(ttl_days, dtype=np.float32)
    start = np.ascontiguousarray(decay_starts, dtype=np.float64)
    n = int(conf.shape[0])
    lib = _LIB
    if lib is not None and n > 0:
        eff = np.empty(n, dtype=np.float32)
        stale = np.empty(n, dtype=np.uint8)
        try:
            lib.mesh_c_compute_decay_batch(
                conf.ctypes.data, ttl.ctypes.data, start.ctypes.data, n,
                float(now_epoch), float(stale_threshold), eff.ctypes.data, stale.ctypes.data,
            )
            return eff, stale.astype(bool)
        except Exception:
            pass
    c64 = conf.astype(np.float64)
    t64 = ttl.astype(np.float64)
    no_decay = ~(t64 > 0.0) | np.isnan(start)
    elapsed = np.maximum(0.0, (now_epoch - np.where(np.isnan(start), now_epoch, start)) / 86400.0)
    factor = np.clip(1.0 - elapsed / np.where(t64 > 0.0, t64, 1.0), 0.0, 1.0)
    eff64 = np.round(np.where(no_decay, c64, c64 * factor), 4)
    thr32 = np.float32(stale_threshold)
    stale_arr = np.where(no_decay, c64.astype(np.float32) < thr32, eff64.astype(np.float32) < thr32)
    return eff64.astype(np.float32), stale_arr.astype(bool)


def is_native_available() -> bool:
    """True if the native library was loaded."""
    return _LIB is not None


def native_info() -> dict:
    return {
        "available": _LIB is not None,
        "path": str(_LIB_PATH) if _LIB_PATH else None,
        "avx2_kernel": _HAS_AVX2,
        "abi": _REQUIRED_ABI,
    }

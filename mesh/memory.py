"""
mesh/memory.py — Core semantic & hybrid memory orchestration for Mesh.

Performance & Security Architecture:
  - Non-Blocking 384-dim `all-MiniLM-L6-v2` Embedding Engine:
      1. Direct `onnxruntime` + `tokenizers` (Zero-network local ONNX, ~80ms cold start, ~2ms inference)
      2. SHA-256 verified canonical ONNX model downloads with interactive `y/n` user permission
         and persistent status/error reporting in `~/.mesh/model_status.json`
      3. Deterministic 384-dim semantic fallback (<0.05ms) when offline, warming up, or declined
  - Read-Only `float32` NumPy `_EMBED_CACHE` (configurable via `MESH_EMBED_CACHE_SIZE`, default 1024),
    cleared on `unload_model()` so idle memory drops to ~0 MB.
  - Bounded `_PENDING_REEMBED_QUEUE` (capped at 10,000 `(namespace, memory_id)` entries, drop-oldest)
    and single-flight per-namespace background healing threads.
  - Built-in SDK Audit Trail & Privacy Redaction:
      Logs `learn`, `recall`, `update`, and `forget` with automatic redaction for `local_only=True`
      memories in both audit logs and returned conflict payloads.
"""
from __future__ import annotations

import difflib
import gc
import hashlib
import json
import math
import os
import re
import shutil
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

import numpy as np

from .audit import (
    REDACTED_CONTENT_PLACEHOLDER,
    ensure_secure_mesh_dir,
    get_mesh_dir,
    log_forget,
    log_learn,
    log_recall,
    log_update,
    secure_file_permissions,
    validate_namespace,
)
from .store import (
    EMBEDDING_DIM,
    MESH_DIR,
    STALE_THRESHOLD,
    MeshStore,
    NamespaceNotFoundError,
    _extract_search_tokens,
    _is_mixed_space,
    _is_same_known_space,
    _parse_iso_utc,
    _utc_now,
    _utc_now_iso,
    expand_query_tokens,
    namespace_write_lock,
)

_MODEL_LOCK = threading.RLock()
_model: Optional[Any] = None
_torch: Optional[Any] = None
_model_backend: Optional[str] = None
_last_model_use_ts: float = 0.0
_watchdog_started: bool = False
_MODEL_NAME = "all-MiniLM-L6-v2"


def _get_onnx_model_dir() -> Path:
    return get_mesh_dir() / "models" / "all-MiniLM-L6-v2-onnx"


def _get_model_status_path() -> Path:
    return get_mesh_dir() / "model_status.json"


_ONNX_MODEL_DIR = _get_onnx_model_dir()

# Canonical unquantized ONNX model & tokenizer mirrors + pinned SHA-256 checksums
_GCS_TARBALL_MIRRORS = [
    "https://storage.googleapis.com/qdrant-fastembed/sentence-transformers-all-MiniLM-L6-v2.tar.gz",
]
_TOKENIZER_MIRRORS = [
    "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/tokenizer.json",
    "https://hf-mirror.com/sentence-transformers/all-MiniLM-L6-v2/resolve/main/tokenizer.json",
]
_ONNX_MODEL_MIRRORS = [
    "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/onnx/model.onnx",
    "https://hf-mirror.com/sentence-transformers/all-MiniLM-L6-v2/resolve/main/onnx/model.onnx",
]

# Verified 64-hex-char SHA-256 digests for sentence-transformers/all-MiniLM-L6-v2 & Qdrant FastEmbed ONNX
_ALLOWED_MODEL_SHA256 = {
    "6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452",  # HF sentence-transformers/all-MiniLM-L6-v2 onnx/model.onnx
    "bbd7b466f6d58e646fdc2bd5fd67b2f5e93c0b687011bd4548c420f7bd46f0c5",  # Qdrant / FastEmbed GCS model_optimized.onnx
    "913d76d8d49b80d8f2857b42b231bf6d0f3c2d8f02840f97fc0254a028cb731f",  # Qdrant / FastEmbed HF revision model_optimized.onnx
}
_ALLOWED_TOKENIZER_SHA256 = {
    "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037",  # HF sentence-transformers/all-MiniLM-L6-v2 tokenizer.json
    "d21f0ef96a71e80b2a47a7482d2ef183a098a706980260d8fb4a8a5e0fa69606",  # Qdrant / FastEmbed GCS tarball tokenizer.json
}
_MAX_TAR_EXTRACT_BYTES = 200 * 1024 * 1024  # 200 MB cap per extracted file

# Background download state & session permission memoization
_DOWNLOAD_LOCK = threading.Lock()
_download_thread: Optional[threading.Thread] = None
_download_in_progress: bool = False
_download_complete_event = threading.Event()
_download_permission_decided: bool = False
_download_permission_granted: bool = False
_bootstrap_triggered: bool = False

# In-flight namespace healing thread registry (Phase 3.2)
_HEALING_IN_FLIGHT_LOCK = threading.Lock()
_HEALING_IN_FLIGHT: set[str] = set()

# Bounded pending re-embed queue keyed by (namespace, memory_id), capped at 10,000 entries (Phase 3.2)
_PENDING_REEMBED_MAXSIZE = 10000
_PENDING_REEMBED_LOCK = threading.Lock()


class _BoundedReembedQueue:
    """
    Bounded queue of `(namespace, memory_id, content)` items awaiting ONNX re-embedding.
    Deduplicates by `(namespace, memory_id)` so the newest content wins and drops the
    oldest entry when capacity exceeds `maxsize` (default 10,000).
    """

    def __init__(self, maxsize: int = _PENDING_REEMBED_MAXSIZE) -> None:
        self.maxsize = max(1, int(maxsize))
        self._data: OrderedDict[tuple[str, str], str] = OrderedDict()

    def put(self, namespace: str, memory_id: str, content: str) -> None:
        key = (str(namespace), str(memory_id))
        self._data[key] = str(content)
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)

    def append(self, item: tuple[str, str, str]) -> None:
        ns, mid, content = item
        self.put(ns, mid, content)

    def pop_all(self) -> list[tuple[str, str, str]]:
        items = [(ns, mid, text) for (ns, mid), text in self._data.items()]
        self._data.clear()
        return items

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)

    def __iter__(self) -> Iterator[tuple[str, str, str]]:
        return iter([(ns, mid, text) for (ns, mid), text in self._data.items()])

    def __getitem__(self, index: Any) -> Any:
        as_list = [(ns, mid, text) for (ns, mid), text in self._data.items()]
        return as_list[index]

    def __bool__(self) -> bool:
        return bool(self._data)


_PENDING_REEMBED_QUEUE = _BoundedReembedQueue(_PENDING_REEMBED_MAXSIZE)

# Unload neural model weights after N seconds of inactivity (default: 180s / 3 minutes).
_IDLE_UNLOAD_SECONDS: float = float(os.environ.get("MESH_IDLE_UNLOAD_SECONDS", "180"))

# Maps cleaned_text -> (readonly_float32_ndarray, model_tag) (Phase 3.1)
_EMBED_CACHE_LOCK = threading.Lock()


def _get_embed_cache_maxsize() -> int:
    raw = os.environ.get("MESH_EMBED_CACHE_SIZE", "").strip()
    if not raw:
        return 1024
    try:
        return max(0, int(raw))
    except ValueError:
        return 1024


_EMBED_CACHE_MAXSIZE = _get_embed_cache_maxsize()
_EMBED_CACHE: OrderedDict[str, tuple[np.ndarray, str]] = OrderedDict()

# Maximum allowed characters in a single memory content string (16 KB)
MAX_CONTENT_LENGTH = int(os.environ.get("MESH_MAX_CONTENT_LENGTH", "16384"))

# Similarity threshold above which two memories are checked for contradiction
CONTRADICTION_THRESHOLD = 0.82

# Similarity threshold above which identical/near-identical facts are deduplicated
DEDUP_THRESHOLD = 0.985

# Unified set of valid memory types across MCP, CLI, HTTP, Passive Capture, and Dashboard
VALID_MEMORY_TYPES = {
    "fact",
    "preference",
    "context",
    "result",
    "instruction",
    "process",
    "decision",
    "error",
    "general",
}

# Extracts standalone numbers AND numbers embedded inside identifiers (e.g. "db1", "v2", "ipv6")
_NUMBER_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?")
_ALNUM_ID_WITH_DIGITS_RE = re.compile(r"\b[a-z0-9_.-]*\d+[a-z0-9_.-]*\b")
# Preserves internal apostrophes so contractions like "isn't", "doesn't", "won't" stay intact
_WORD_TOKEN_RE = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?")

_NEGATION_TOKENS = {
    "not",
    "never",
    "no",
    "dont",
    "don't",
    "doesnt",
    "doesn't",
    "didnt",
    "didn't",
    "isnt",
    "isn't",
    "arent",
    "aren't",
    "wasnt",
    "wasn't",
    "werent",
    "weren't",
    "wont",
    "won't",
    "wouldnt",
    "wouldn't",
    "shouldnt",
    "shouldn't",
    "cannot",
    "cant",
    "can't",
    "havent",
    "haven't",
    "hasnt",
    "hasn't",
    "hadnt",
    "hadn't",
    "neither",
    "nor",
    "without",
    "disabled",
    "false",
    "off",
    "disallow",
    "forbidden",
    "none",
}

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "in", "on", "at", "to", "for", "of", "with", "by", "from", "about",
    "as", "into", "like", "through", "after", "over", "between", "out",
    "against", "during", "before", "under", "around", "among",
    "what", "where", "when", "who", "whom", "which", "how", "why",
    "do", "does", "did", "have", "has", "had", "having", "can", "could",
    "should", "would", "may", "might", "must", "shall", "will", "that",
    "this", "these", "those", "there", "their", "they", "them", "it",
    "its", "we", "our", "you", "your", "i", "me", "my", "and", "or",
    "but", "if", "then", "else", "any", "all", "some", "such", "very",
}

_COLOR_TOKENS = {
    "blue", "red", "green", "yellow", "black", "white", "purple", "orange",
    "pink", "brown", "gray", "grey", "cyan", "magenta", "silver", "gold",
}

# Semantic concept clusters for instant zero-wait local embedding before ONNX finishes downloading
_CONCEPT_CLUSTERS: list[tuple[str, set[str]]] = [
    ("@c_theme", {"dark", "light", "mode", "theme", "visual", "color", "colour", "appearance", "editor", "editors", "ui", *_COLOR_TOKENS}),
    ("@c_pref", {"prefer", "prefers", "preference", "preferences", "like", "likes", "favorite", "favourite", "want", "strongly", "love", "loves"}),
    ("@c_media", {"anime", "manga", "movie", "movies", "show", "series", "book", "music", "song", "game"}),
    ("@c_item", {"pen", "pencil", "notebook", "desk", "laptop", "phone", "car", "bag", "watch", "chair", "keyboard"}),
    ("@c_cred", {"password", "credential", "credentials", "secret", "secrets", "vault", "apikey", "key", "private"}),
    ("@c_auth", {"auth", "authentication", "login", "session", "jwt", "token", "tokens", "cookie", "cookies", "httponly", "localstorage", "management"}),
    ("@c_db", {"database", "db", "postgres", "postgresql", "sql", "sqlite", "index", "indexing", "migration", "schema", "pgbouncer", "pooling", "redis"}),
    ("@c_err", {"error", "errors", "bug", "bugs", "issue", "issues", "problem", "problems", "failed", "fails", "incident", "timeout", "quorum"}),
    ("@c_limit", {"rate", "limit", "limits", "constraint", "constraints", "quota", "requests", "minute", "ip", "api"}),
    ("@c_lang", {"typescript", "javascript", "python", "rust", "go", "java", "language", "languages", "program", "programming", "code", "coding", "file", "files"}),
    ("@c_pet", {"cat", "cats", "dog", "dogs", "pet", "pets"}),
    ("@c_env", {"staging", "production", "prod", "dev", "environment", "server", "url", "port", "deploy", "script", "host"}),
    ("@c_mem", {"agent", "agents", "coordinate", "shared", "memory", "memories", "recall", "learn", "mesh"}),
]


# ==============================================================================
# Persistent Model Bootstrap Status (`~/.mesh/model_status.json`, Phase 3.3)
# ==============================================================================

_STATUS_LOCK = threading.Lock()
_LAST_MODEL_STATUS: dict[str, Any] = {
    "state": "uninitialized",
    "backend": "unloaded",
    "last_error": None,
    "last_attempt": None,
    "checksum_expected": None,
    "checksum_actual": None,
    "hint": None,
}


def _record_model_status(
    state: str,
    backend: Optional[str] = None,
    last_error: Optional[str] = None,
    checksum_expected: Optional[list[str]] = None,
    checksum_actual: Optional[str] = None,
    hint: Optional[str] = None,
) -> dict[str, Any]:
    """Persist ONNX model bootstrap state and errors to `~/.mesh/model_status.json`."""
    now_iso = _utc_now_iso()
    with _STATUS_LOCK:
        payload: dict[str, Any] = {
            "state": state,
            "backend": backend or (_model_backend or "unloaded"),
            "last_error": last_error,
            "last_attempt": now_iso,
            "checksum_expected": checksum_expected,
            "checksum_actual": checksum_actual,
        }
        if hint:
            payload["hint"] = hint
        _LAST_MODEL_STATUS.update(payload)
        try:
            status_path = _get_model_status_path()
            ensure_secure_mesh_dir(status_path.parent)
            tmp_path = status_path.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
            tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            secure_file_permissions(tmp_path)
            os.replace(tmp_path, status_path)
            secure_file_permissions(status_path)
        except OSError:
            pass
        return dict(payload)


def get_model_status() -> dict[str, Any]:
    """Read `~/.mesh/model_status.json` or return in-memory bootstrap status."""
    status_path = _get_model_status_path()
    if status_path.exists():
        try:
            data = json.loads(status_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    with _STATUS_LOCK:
        return dict(_LAST_MODEL_STATUS)


def _make_readonly_f32(vec: Any) -> np.ndarray:
    arr = np.asarray(vec, dtype=np.float32).reshape(-1).copy()
    arr.setflags(write=False)
    return arr


def _normalize_rows(arr: np.ndarray) -> np.ndarray:
    """L2-normalize a 1D or 2D float32 numpy array."""
    a = np.asarray(arr, dtype=np.float32)
    if a.ndim == 1:
        norm = float(np.linalg.norm(a))
        return a / norm if norm > 1e-12 else a
    norms = np.linalg.norm(a, axis=1, keepdims=True)
    norms = np.where(norms > 1e-12, norms, 1.0)
    return a / norms


def _normalize_text_for_tokens(text: str) -> str:
    """Lowercase and strip possessive "'s" while preserving contractions like "isn't"."""
    s = text.strip().lower().replace("\u2019", "'").replace("\u2018", "'").replace("`", "'")
    return re.sub(r"(?<!n)'s\b", "", s)


def _compute_file_sha256(path: Path) -> str:
    """Return hex SHA-256 digest of a file on disk."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest().lower()


def _verify_sha256(
    path: Path,
    allowed_hashes: set[str],
    env_override_key: str,
    source_label: str = "",
) -> bool:
    """
    Verify that `path` matches one of `allowed_hashes` (or custom hash in `env_override_key`),
    unless `MESH_SKIP_CHECKSUM=1` is set.
    Logs any mismatch loudly to stderr and to `~/.mesh/model_status.json` (Phase 3.3).
    """
    if not path.exists():
        return False
    if os.environ.get("MESH_SKIP_CHECKSUM", "").strip().lower() in ("1", "true", "yes"):
        return True
    custom = os.environ.get(env_override_key, "").strip().lower()
    expected = {custom} if custom else {h.lower() for h in allowed_hashes}
    if not expected:
        return True
    try:
        actual = _compute_file_sha256(path)
        if actual in expected:
            return True
        expected_list = sorted(expected)
        src_info = f" from {source_label}" if source_label else ""
        err_msg = (
            f"SHA-256 checksum mismatch for '{path.name}'{src_info}: "
            f"expected one of {expected_list}, got '{actual}'"
        )
        _emit_warning(err_msg)
        _record_model_status(
            state="checksum_mismatch",
            backend=_model_backend or "hash-fallback",
            last_error=err_msg,
            checksum_expected=expected_list,
            checksum_actual=actual,
        )
        return False
    except OSError as exc:
        err_msg = f"Failed reading '{path}' for SHA-256 verification: {exc}"
        _emit_warning(err_msg)
        _record_model_status(
            state="io_error",
            backend=_model_backend or "hash-fallback",
            last_error=err_msg,
        )
        return False


def _is_interactive_tty() -> bool:
    if os.environ.get("MESH_NON_INTERACTIVE", "").strip().lower() in ("1", "true", "yes"):
        return False
    return bool(
        hasattr(sys.stdin, "isatty")
        and sys.stdin.isatty()
        and hasattr(sys.stdout, "isatty")
        and sys.stdout.isatty()
    )


def _confirm_background_action(
    action_description: str,
    env_disable_key: Optional[str] = None,
    assume_yes: bool = False,
) -> bool:
    """
    Ask the user for explicit `y/n` permission before starting a background process
    or downloading weights. Never runs silently in non-interactive library usage.
    """
    if env_disable_key and os.environ.get(env_disable_key, "").strip().lower() in ("1", "true", "yes"):
        return False
    from .daemon import request_user_permission

    return request_user_permission(action_description, assume_yes=assume_yes)


def _request_model_download_permission(assume_yes: bool = False) -> bool:
    """
    Prompt the user once per session for `y/n` permission before downloading the ~90MB ONNX model.
    In non-TTY contexts (such as the MCP stdio server), records `state="download_skipped_non_interactive"`
    with hint `"run mesh setup"` in `~/.mesh/model_status.json` (Phase 3.3).
    """
    global _download_permission_decided, _download_permission_granted
    if os.environ.get("MESH_NO_MODEL_DOWNLOAD", "").strip().lower() in ("1", "true", "yes"):
        _record_model_status(
            state="download_disabled",
            backend="hash-fallback",
            last_error="ONNX download disabled via MESH_NO_MODEL_DOWNLOAD=1",
            hint="Unset MESH_NO_MODEL_DOWNLOAD and run mesh setup",
        )
        return False
    if assume_yes or os.environ.get("MESH_AUTO_APPROVE", "").strip().lower() in ("1", "true", "yes"):
        _download_permission_decided = True
        _download_permission_granted = True
        return True
    if _download_permission_decided:
        return _download_permission_granted

    if not _is_interactive_tty():
        _download_permission_decided = True
        _download_permission_granted = False
        _record_model_status(
            state="download_skipped_non_interactive",
            backend="hash-fallback",
            last_error="ONNX model not downloaded and current process is non-interactive (no TTY).",
            hint="run mesh setup",
        )
        return False

    granted = _confirm_background_action(
        f"download the ~90MB ONNX embedding model into {_get_onnx_model_dir()}",
        env_disable_key="MESH_NO_MODEL_DOWNLOAD",
        assume_yes=assume_yes,
    )
    _download_permission_decided = True
    _download_permission_granted = granted
    if not granted:
        _record_model_status(
            state="download_declined",
            backend="hash-fallback",
            last_error="User declined ONNX model download prompt.",
            hint="run mesh setup",
        )
    return granted


def _split_long_text_windows(text: str, max_words: int = 180, stride: int = 140) -> list[str]:
    """
    Split texts longer than ~256 subword tokens into overlapping windows so information
    beyond the first 256 tokens is represented in the pooled embedding vector.
    """
    words = text.split()
    if len(words) <= max_words:
        return [text]
    windows: list[str] = []
    for start in range(0, len(words), stride):
        chunk = " ".join(words[start : start + max_words])
        if chunk:
            windows.append(chunk)
        if start + max_words >= len(words):
            break
        if len(windows) >= 8:
            break
    return windows or [text]


class _DirectOnnxEmbedding:
    """
    Ultra-fast, zero-network local ONNX Runtime + HuggingFace Tokenizers engine
    for 384-dim `all-MiniLM-L6-v2` embeddings (~80ms cold load, ~2ms inference).
    Supports automatic multi-window mean pooling for texts exceeding 256 tokens.
    """

    def __init__(self, model_path: Path, tokenizer_path: Path):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.model_path = model_path
        self.tokenizer_path = tokenizer_path
        self.tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self.tokenizer.enable_truncation(max_length=256)
        self.tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")

        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        cpu_count = os.cpu_count() or 4
        sess_options.intra_op_num_threads = min(4, max(1, cpu_count))
        sess_options.inter_op_num_threads = 1
        sess_options.log_severity_level = 3

        self.session = ort.InferenceSession(
            str(model_path),
            sess_options=sess_options,
            providers=["CPUExecutionProvider"],
        )
        self.input_names = {inp.name for inp in self.session.get_inputs()}

    def _encode_raw_batches(self, texts: list[str], batch_size: int = 32) -> np.ndarray:
        if not texts:
            return np.empty((0, EMBEDDING_DIM), dtype=np.float32)
        all_batches: list[np.ndarray] = []
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            encodings = self.tokenizer.encode_batch(batch)
            input_ids = np.asarray([e.ids for e in encodings], dtype=np.int64)
            attention_mask = np.asarray([e.attention_mask for e in encodings], dtype=np.int64)

            ort_inputs: dict[str, Any] = {}
            if "input_ids" in self.input_names:
                ort_inputs["input_ids"] = input_ids
            if "attention_mask" in self.input_names:
                ort_inputs["attention_mask"] = attention_mask
            if "token_type_ids" in self.input_names:
                ort_inputs["token_type_ids"] = np.zeros_like(input_ids, dtype=np.int64)

            outputs = self.session.run(None, ort_inputs)
            token_embeddings = np.asarray(outputs[0], dtype=np.float32)
            if token_embeddings.ndim == 3:
                mask = np.expand_dims(attention_mask, axis=-1).astype(np.float32)
                summed = np.sum(token_embeddings * mask, axis=1)
                counts = np.clip(mask.sum(axis=1), a_min=1e-9, a_max=None)
                pooled = summed / counts
            else:
                pooled = token_embeddings
            all_batches.append(_normalize_rows(pooled))
        if len(all_batches) == 1:
            return all_batches[0]
        return np.vstack(all_batches)

    def encode(self, texts: list[str], batch_size: int = 32) -> np.ndarray:
        if not texts:
            return np.empty((0, EMBEDDING_DIM), dtype=np.float32)
        if all(len(t) <= 900 for t in texts):
            return self._encode_raw_batches(texts, batch_size=batch_size)

        flat_windows: list[str] = []
        window_ranges: list[tuple[int, int]] = []
        for t in texts:
            wins = _split_long_text_windows(t)
            start_idx = len(flat_windows)
            flat_windows.extend(wins)
            window_ranges.append((start_idx, len(flat_windows)))

        win_vecs = self._encode_raw_batches(flat_windows, batch_size=batch_size)
        doc_vecs: list[np.ndarray] = []
        for start_idx, end_idx in window_ranges:
            if end_idx - start_idx == 1:
                doc_vecs.append(win_vecs[start_idx])
            else:
                mean_vec = np.mean(win_vecs[start_idx:end_idx], axis=0)
                doc_vecs.append(_normalize_rows(mean_vec))
        return np.vstack(doc_vecs)


def _stem_word(w: str) -> str:
    if len(w) <= 3 or "'" in w:
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("ing") and len(w) > 5:
        base = w[:-3]
        return base[:-1] if len(base) >= 3 and base[-1] == base[-2] else base
    if w.endswith("ed") and len(w) > 4:
        return w[:-2]
    if w.endswith("es") and len(w) > 4:
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


def _hash_embed_384(text: str) -> np.ndarray:
    """
    Concept-aware deterministic 384-dim semantic feature-hash embedding (<0.05ms).
    Reserves indices 368..383 == 0.0 as a deterministic watermark alongside the SQLite
    `embedding_model = 'hash-384'` column so warm-up vectors are always upgraded once ONNX loads.
    """
    vec = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    cleaned = _normalize_text_for_tokens(text)
    raw_words = _WORD_TOKEN_RE.findall(cleaned)
    nums = _NUMBER_TOKEN_RE.findall(cleaned)

    content_words = [w for w in raw_words if w not in _STOPWORDS or w in _NEGATION_TOKENS]
    if not content_words and not nums:
        content_words = raw_words or ["empty"]

    stemmed_words = [_stem_word(w) for w in content_words]
    has_non_framing = any(sw not in {"user", "agent", "system"} for sw in stemmed_words)
    word_set = set(raw_words) | set(stemmed_words)

    features: list[tuple[str, float]] = []
    for w, sw in zip(content_words, stemmed_words):
        w_weight = 0.25 if (has_non_framing and sw in {"user", "agent", "system"}) else 1.35
        features.append((f"w:{sw}", w_weight))
        if w != sw:
            features.append((f"raw:{w}", w_weight * 0.45))

    for n in nums:
        features.append((f"num:{n}", 1.6))

    for i in range(len(stemmed_words) - 1):
        features.append((f"bi:{stemmed_words[i]}_{stemmed_words[i + 1]}", 0.65))

    for cid, cwords in _CONCEPT_CLUSTERS:
        overlap = len(word_set & cwords)
        if overlap > 0:
            features.append((cid, 1.5 + min(1.2, overlap * 0.4)))

    for sw in stemmed_words:
        if len(sw) >= 4:
            for j in range(len(sw) - 2):
                features.append((f"c3:{sw[j : j + 3]}", 0.22))

    for feat, weight in features:
        digest = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
        h = int.from_bytes(digest, "little")
        idx1 = h % 368
        sign1 = 1.0 if ((h >> 9) & 1) == 0 else -1.0
        idx2 = (h >> 16) % 368
        sign2 = 1.0 if ((h >> 25) & 1) == 0 else -1.0
        idx3 = (h >> 32) % 368
        sign3 = 1.0 if ((h >> 41) & 1) == 0 else -1.0
        vec[idx1] += sign1 * weight
        vec[idx2] += sign2 * (weight * 0.6)
        vec[idx3] += sign3 * (weight * 0.3)

    return _normalize_rows(vec)


def _has_local_onnx_pair() -> Optional[tuple[Path, Path]]:
    """
    O(1) hot-path check (<0.02ms) for `~/.mesh/models/all-MiniLM-L6-v2-onnx/{model.onnx, tokenizer.json}`.
    """
    model_dir = _get_onnx_model_dir()
    target_model = model_dir / "model.onnx"
    target_tok = model_dir / "tokenizer.json"
    if target_model.exists() and target_tok.exists():
        try:
            if target_model.stat().st_size > 80_000_000 and target_tok.stat().st_size > 100_000:
                return target_model, target_tok
        except OSError:
            pass
    return None


def _discover_local_onnx_pair() -> Optional[tuple[Path, Path]]:
    """
    Check `_get_onnx_model_dir()` first; if missing, scan local HuggingFace / FastEmbed caches
    for canonical unquantized `model.onnx` and `tokenizer.json`.
    """
    fast_pair = _has_local_onnx_pair()
    if fast_pair is not None:
        return fast_pair

    model_dir = _get_onnx_model_dir()
    target_model = model_dir / "model.onnx"
    target_tok = model_dir / "tokenizer.json"

    home = Path.home()
    hf_hub = Path(os.environ.get("HF_HOME", str(home / ".cache" / "huggingface"))) / "hub"
    search_roots = [
        hf_hub / "models--sentence-transformers--all-MiniLM-L6-v2",
        hf_hub / "models--qdrant--all-MiniLM-L6-v2-onnx",
        Path(tempfile.gettempdir()) / "fastembed_cache",
        home / ".cache" / "fastembed",
    ]

    found_model: Optional[Path] = None
    found_tok: Optional[Path] = None
    for root in search_roots:
        if not root.exists():
            continue
        try:
            for candidate in root.rglob("*"):
                if not candidate.is_file():
                    continue
                name_lower = candidate.name.lower()
                if name_lower == "tokenizer.json" and found_tok is None:
                    if candidate.stat().st_size > 100_000 and _verify_sha256(
                        candidate,
                        _ALLOWED_TOKENIZER_SHA256,
                        "MESH_TOKENIZER_SHA256",
                        source_label=str(candidate),
                    ):
                        found_tok = candidate
                elif name_lower in ("model.onnx", "model_optimized.onnx") and found_model is None:
                    if candidate.stat().st_size > 80_000_000 and _verify_sha256(
                        candidate,
                        _ALLOWED_MODEL_SHA256,
                        "MESH_MODEL_SHA256",
                        source_label=str(candidate),
                    ):
                        found_model = candidate
                if found_model is not None and found_tok is not None:
                    break
        except OSError:
            continue

    if found_tok is not None and not target_tok.exists():
        try:
            ensure_secure_mesh_dir(model_dir)
            shutil.copy2(found_tok, target_tok)
        except OSError:
            pass
    if found_model is not None and not target_model.exists():
        try:
            ensure_secure_mesh_dir(model_dir)
            shutil.copy2(found_model, target_model)
        except OSError:
            pass

    return _has_local_onnx_pair()


def _download_file_from_mirrors(
    urls: list[str],
    dest: Path,
    min_bytes: int,
    allowed_sha256: Optional[set[str]] = None,
    sha256_env_key: str = "",
) -> bool:
    """
    Download a single file from the first responsive mirror, verify its SHA-256 checksum,
    and log any rejection or HTTP failure loudly to stderr and `model_status.json` (Phase 3.3).
    """
    if dest.exists():
        try:
            if dest.stat().st_size >= min_bytes:
                if not allowed_sha256 or _verify_sha256(
                    dest, allowed_sha256, sha256_env_key, source_label=str(dest)
                ):
                    return True
        except OSError:
            pass

    ensure_secure_mesh_dir(_get_onnx_model_dir())
    headers = {"User-Agent": "mesh-context-layer/0.7.0"}
    hf_token = (
        os.environ.get("HF_TOKEN")
        or os.environ.get("HUGGING_FACE_HUB_TOKEN")
        or os.environ.get("HUGGINGFACE_TOKEN")
    )
    try:
        import httpx

        timeout_cfg = httpx.Timeout(connect=3.5, read=15.0, write=5.0, pool=5.0)
        for url in urls:
            req_headers = dict(headers)
            if hf_token and "huggingface.co" in url:
                req_headers["Authorization"] = f"Bearer {hf_token.strip()}"
            tmp_path = dest.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
            try:
                with httpx.Client(follow_redirects=True, timeout=timeout_cfg, headers=req_headers) as client:
                    with client.stream("GET", url) as resp:
                        if resp.status_code != 200:
                            err_msg = f"Download from {url} failed with HTTP status {resp.status_code}"
                            _emit_warning(err_msg)
                            _record_model_status(
                                state="download_http_error",
                                backend=_model_backend or "hash-fallback",
                                last_error=err_msg,
                            )
                            continue
                        written = 0
                        with open(tmp_path, "wb") as f:
                            for chunk in resp.iter_bytes(chunk_size=65536):
                                if chunk:
                                    written += len(chunk)
                                    if written > _MAX_TAR_EXTRACT_BYTES:
                                        raise ValueError("Download exceeded maximum allowed size")
                                    f.write(chunk)
                if tmp_path.exists() and tmp_path.stat().st_size >= min_bytes:
                    if allowed_sha256 and not _verify_sha256(
                        tmp_path, allowed_sha256, sha256_env_key, source_label=url
                    ):
                        tmp_path.unlink(missing_ok=True)
                        continue
                    tmp_path.replace(dest)
                    return True
                else:
                    actual_sz = tmp_path.stat().st_size if tmp_path.exists() else 0
                    err_msg = f"Downloaded file from {url} was truncated ({actual_sz} bytes < {min_bytes} bytes)"
                    _emit_warning(err_msg)
                    _record_model_status(
                        state="download_truncated",
                        backend=_model_backend or "hash-fallback",
                        last_error=err_msg,
                    )
            except Exception as exc:
                err_msg = f"Download error from {url}: {exc}"
                _emit_warning(err_msg)
                _record_model_status(
                    state="download_error",
                    backend=_model_backend or "hash-fallback",
                    last_error=err_msg,
                )
            finally:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
    except Exception as outer_exc:
        err_msg = f"HTTP client initialization failed during model download: {outer_exc}"
        _emit_warning(err_msg)
        _record_model_status(
            state="download_error",
            backend=_model_backend or "hash-fallback",
            last_error=err_msg,
        )
    return False


def _safe_copy_limited(src: Any, dst: Any, max_bytes: int = _MAX_TAR_EXTRACT_BYTES) -> bool:
    """Copy at most `max_bytes` from `src` to `dst` to prevent archive decompression bombs."""
    total = 0
    while True:
        buf = src.read(65536)
        if not buf:
            return True
        total += len(buf)
        if total > max_bytes:
            return False
        dst.write(buf)


def _download_from_gcs_tarball() -> bool:
    """
    Download from Google Cloud Storage `.tar.gz` bundle with size-bounded extraction
    and SHA-256 verification on extracted files.
    """
    if _has_local_onnx_pair() is not None:
        return True
    model_dir = _get_onnx_model_dir()
    target_model = model_dir / "model.onnx"
    target_tok = model_dir / "tokenizer.json"
    ensure_secure_mesh_dir(model_dir)
    tmp_tar = model_dir / f"bundle.{os.getpid()}.{threading.get_ident()}.tar.gz"
    try:
        if not _download_file_from_mirrors(_GCS_TARBALL_MIRRORS, tmp_tar, min_bytes=20_000_000):
            return False
        with tarfile.open(tmp_tar, "r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile() or member.size <= 0 or member.size > _MAX_TAR_EXTRACT_BYTES:
                    continue
                base_name = Path(member.name).name.lower()
                if base_name in ("model.onnx", "model_optimized.onnx") and not target_model.exists():
                    extracted = tar.extractfile(member)
                    if extracted is not None:
                        tmp_out = target_model.with_suffix(f".tmp.{os.getpid()}")
                        with open(tmp_out, "wb") as out_f:
                            ok = _safe_copy_limited(extracted, out_f, _MAX_TAR_EXTRACT_BYTES)
                        if ok and _verify_sha256(
                            tmp_out,
                            _ALLOWED_MODEL_SHA256,
                            "MESH_MODEL_SHA256",
                            source_label=f"{_GCS_TARBALL_MIRRORS[0]}:{member.name}",
                        ):
                            tmp_out.replace(target_model)
                        else:
                            tmp_out.unlink(missing_ok=True)
                elif base_name == "tokenizer.json" and not target_tok.exists():
                    extracted = tar.extractfile(member)
                    if extracted is not None:
                        tmp_out = target_tok.with_suffix(f".tmp.{os.getpid()}")
                        with open(tmp_out, "wb") as out_f:
                            ok = _safe_copy_limited(extracted, out_f, 20 * 1024 * 1024)
                        if ok and _verify_sha256(
                            tmp_out,
                            _ALLOWED_TOKENIZER_SHA256,
                            "MESH_TOKENIZER_SHA256",
                            source_label=f"{_GCS_TARBALL_MIRRORS[0]}:{member.name}",
                        ):
                            tmp_out.replace(target_tok)
                        else:
                            tmp_out.unlink(missing_ok=True)
        return _has_local_onnx_pair() is not None
    except Exception as exc:
        err_msg = f"Failed extracting ONNX tarball: {exc}"
        _emit_warning(err_msg)
        _record_model_status(
            state="tar_extract_error",
            backend=_model_backend or "hash-fallback",
            last_error=err_msg,
        )
        return False
    finally:
        try:
            tmp_tar.unlink(missing_ok=True)
        except OSError:
            pass


def ensure_model_downloaded(
    blocking: bool = False,
    timeout: float = 15.0,
    interactive_prompt: bool = True,
    assume_yes: bool = False,
) -> Optional[tuple[Path, Path]]:
    """
    Ensure the `all-MiniLM-L6-v2` ONNX model and tokenizer are present on local disk.
    Asks the user for `y/n` permission before starting the download unless `assume_yes=True`.
    """
    existing = _has_local_onnx_pair() or _discover_local_onnx_pair()
    if existing is not None:
        _download_complete_event.set()
        _record_model_status(state="ready", backend=_model_backend or "onnx", last_error=None)
        return existing
    if interactive_prompt and not _request_model_download_permission(assume_yes=assume_yes):
        return None
    _start_background_model_download()
    if not blocking:
        return None
    _download_complete_event.wait(timeout=timeout)
    return _has_local_onnx_pair()


def _background_download_worker() -> None:
    """Download `tokenizer.json` and canonical `model.onnx` in the background and hot-swap the ONNX engine."""
    global _download_in_progress, _model, _model_backend
    try:
        _record_model_status(state="downloading", backend=_model_backend or "onnx-warming")
        pair = _discover_local_onnx_pair()
        if pair is None:
            _download_from_gcs_tarball()
            pair = _has_local_onnx_pair()
        if pair is None:
            model_dir = _get_onnx_model_dir()
            tok_dest = model_dir / "tokenizer.json"
            model_dest = model_dir / "model.onnx"
            if not tok_dest.exists():
                _download_file_from_mirrors(
                    _TOKENIZER_MIRRORS,
                    tok_dest,
                    min_bytes=100_000,
                    allowed_sha256=_ALLOWED_TOKENIZER_SHA256,
                    sha256_env_key="MESH_TOKENIZER_SHA256",
                )
            if not model_dest.exists():
                _download_file_from_mirrors(
                    _ONNX_MODEL_MIRRORS,
                    model_dest,
                    min_bytes=80_000_000,
                    allowed_sha256=_ALLOWED_MODEL_SHA256,
                    sha256_env_key="MESH_MODEL_SHA256",
                )
            pair = _has_local_onnx_pair()

        if pair is not None:
            try:
                onnx_eng = _DirectOnnxEmbedding(pair[0], pair[1])
                with _MODEL_LOCK:
                    _model = onnx_eng
                    _model_backend = "onnx"
                    _ensure_idle_watchdog()
                _record_model_status(state="ready", backend="onnx", last_error=None)
                _backfill_pending_embeddings()
            except Exception as exc:
                err_msg = f"ONNX session initialization failed: {exc}"
                _emit_warning(err_msg)
                _record_model_status(state="onnx_init_failed", backend="hash-fallback", last_error=err_msg)
    finally:
        with _DOWNLOAD_LOCK:
            _download_in_progress = False
        _download_complete_event.set()


def _start_background_model_download() -> None:
    global _download_thread, _download_in_progress
    if _has_local_onnx_pair() is not None:
        _download_complete_event.set()
        return
    with _DOWNLOAD_LOCK:
        if _download_in_progress:
            return
        _download_in_progress = True
        _download_complete_event.clear()
        _download_thread = threading.Thread(
            target=_background_download_worker,
            name="mesh-onnx-downloader",
            daemon=True,
        )
        _download_thread.start()


def _backfill_pending_embeddings() -> None:
    """
    Re-embed any memories that were stored while the ONNX model was downloading in the background,
    verifying that the memory still exists with the same content and preserving `accessed_at`.
    """
    with _PENDING_REEMBED_LOCK:
        pending = _PENDING_REEMBED_QUEUE.pop_all()

    with _EMBED_CACHE_LOCK:
        _EMBED_CACHE.clear()

    if not pending:
        return

    by_ns: dict[str, list[tuple[str, str]]] = {}
    for ns, mid, text in pending:
        by_ns.setdefault(ns, []).append((mid, text))

    for ns, items in by_ns.items():
        try:
            store = MeshStore(namespace=ns, create=False)
            valid_items: list[tuple[str, str]] = []
            for mid, queued_text in items:
                current_row = store.get(mid)
                if current_row is not None and current_row.get("content") == queued_text:
                    valid_items.append((mid, queued_text))
            if not valid_items:
                continue
            texts = [t for _, t in valid_items]
            vecs, tag = _encode_texts_with_tag(texts)
            if tag != "onnx-minilm-l6-v2":
                continue
            for (mid, text), vec in zip(valid_items, vecs):
                latest_row = store.get(mid)
                if latest_row is not None and latest_row.get("content") == text:
                    store.update(
                        memory_id=mid,
                        content=None,
                        embedding=vec.tolist(),
                        embedding_model="onnx-minilm-l6-v2",
                        preserve_accessed_at=True,
                    )
        except Exception:
            pass


def _trigger_background_bootstrap() -> None:
    """
    Never spawns background daemons or modifies OS login startup silently on `Mesh()` instantiation.
    Only starts background daemon if explicitly opted into via `MESH_AUTO_START_DAEMON=1` and
    confirmed by the user with `y/n`.
    """
    global _bootstrap_triggered
    if _bootstrap_triggered:
        return
    _bootstrap_triggered = True

    if (
        os.environ.get("MESH_AUTO_START_DAEMON", "").strip().lower() in ("1", "true", "yes")
        and os.environ.get("MESH_NO_AUTO_DAEMON", "").strip().lower() not in ("1", "true", "yes")
        and os.environ.get("MESH_IN_DAEMON", "").strip().lower() not in ("1", "true", "yes")
    ):
        if _confirm_background_action("start the background Mesh HTTP daemon", "MESH_NO_AUTO_DAEMON"):
            try:
                from .daemon import ensure_daemon_running

                ensure_daemon_running(wait=False, interactive_prompt=False, assume_yes=True)
            except Exception:
                pass


def set_idle_unload_seconds(seconds: float) -> None:
    """Configure the automatic idle model unload timeout in seconds (0 disables unloading)."""
    global _IDLE_UNLOAD_SECONDS
    _IDLE_UNLOAD_SECONDS = max(0.0, float(seconds))
    if _IDLE_UNLOAD_SECONDS > 0:
        _ensure_idle_watchdog()


def unload_model() -> bool:
    """
    Explicitly unload the embedding model from memory, clear `_EMBED_CACHE`,
    and trigger garbage collection so idle memory drops to ~0 MB (Phase 3.1).
    Returns True if a loaded model was unloaded.
    """
    global _model, _torch, _model_backend
    with _EMBED_CACHE_LOCK:
        _EMBED_CACHE.clear()
    with _MODEL_LOCK:
        if _model is None:
            return False
        _model = None
        _model_backend = None
        torch_ref = _torch
        _torch = None
    try:
        if torch_ref is not None and hasattr(torch_ref, "cuda") and torch_ref.cuda.is_available():
            torch_ref.cuda.empty_cache()
    except Exception:
        pass
    gc.collect()
    return True


def get_engine_status() -> dict[str, Any]:
    """Return current embedding engine, cache memory usage, and bootstrap status in O(1) time."""
    with _MODEL_LOCK:
        loaded = _model is not None
        backend = _model_backend or "unloaded"
        idle_for = (
            round(max(0.0, time.monotonic() - _last_model_use_ts), 2)
            if _last_model_use_ts > 0
            else None
        )
    with _EMBED_CACHE_LOCK:
        cache_size = len(_EMBED_CACHE)
        cache_bytes = sum(
            int(arr.nbytes) + sys.getsizeof(k) + 64
            for k, (arr, _) in _EMBED_CACHE.items()
        )
    maxsize = _get_embed_cache_maxsize()
    onnx_on_disk = _has_local_onnx_pair() is not None
    status_info = get_model_status()
    degraded = backend in ("onnx-warming", "hash-fallback") or (
        backend == "unloaded" and not onnx_on_disk
    )
    model_tag = (
        "onnx-minilm-l6-v2"
        if (backend in ("onnx", "fastembed", "sentence-transformers") or (backend == "unloaded" and onnx_on_disk))
        else "hash-384"
    )
    return {
        "model_loaded": loaded,
        "backend": backend,
        "model_name": _MODEL_NAME,
        "model_tag": model_tag,
        "embedding_degraded": degraded,
        "idle_seconds": idle_for,
        "idle_unload_timeout_seconds": _IDLE_UNLOAD_SECONDS,
        "embed_cache_entries": cache_size,
        "embed_cache_size": cache_size,
        "embed_cache_bytes": cache_bytes,
        "embed_cache_maxsize": maxsize,
        "onnx_cached_on_disk": onnx_on_disk,
        "download_in_progress": _download_in_progress,
        "last_error": status_info.get("last_error"),
        "model_status": status_info,
    }


def _idle_watchdog_loop() -> None:
    """Background daemon that unloads the embedding model when idle to keep RAM near zero."""
    while True:
        sleep_step = 15.0
        if _IDLE_UNLOAD_SECONDS > 0:
            sleep_step = max(2.0, min(15.0, _IDLE_UNLOAD_SECONDS / 3.0))
        time.sleep(sleep_step)
        if _IDLE_UNLOAD_SECONDS <= 0:
            continue
        with _MODEL_LOCK:
            if _model is None or _last_model_use_ts <= 0:
                continue
            elapsed = time.monotonic() - _last_model_use_ts
            if elapsed < _IDLE_UNLOAD_SECONDS:
                continue
        unload_model()


def _ensure_idle_watchdog() -> None:
    global _watchdog_started
    if _watchdog_started or _IDLE_UNLOAD_SECONDS <= 0:
        return
    with _MODEL_LOCK:
        if not _watchdog_started and _IDLE_UNLOAD_SECONDS > 0:
            _watchdog_started = True
            t = threading.Thread(
                target=_idle_watchdog_loop,
                name="mesh-idle-model-watchdog",
                daemon=True,
            )
            t.start()


def _get_model() -> tuple[Any, str]:
    """
    Thread-safe model loader:
      1. Direct local ONNX (`_DirectOnnxEmbedding`) — checks `_ONNX_MODEL_DIR` and local caches
      2. Optional PyTorch `SentenceTransformer` when `MESH_FORCE_PYTORCH=1`
      3. Instant `<0.05ms` `_hash_embed_384` fallback while background downloader fetches ONNX
         (only after asking the user for `y/n` permission).
    """
    global _model, _torch, _model_backend, _last_model_use_ts
    _last_model_use_ts = time.monotonic()

    if _model is not None and _model_backend is not None:
        if _model_backend != "onnx-warming" or _has_local_onnx_pair() is None:
            return _model, _model_backend

    with _MODEL_LOCK:
        _last_model_use_ts = time.monotonic()
        onnx_pair = _has_local_onnx_pair() or _discover_local_onnx_pair()
        if _model is not None and _model_backend is not None:
            if _model_backend != "onnx-warming" or onnx_pair is None:
                return _model, _model_backend

        force_pytorch = os.environ.get("MESH_FORCE_PYTORCH", "").strip().lower() in ("1", "true", "yes")

        if not force_pytorch and onnx_pair is not None:
            try:
                if _verify_sha256(
                    onnx_pair[0], _ALLOWED_MODEL_SHA256, "MESH_MODEL_SHA256", source_label=str(onnx_pair[0])
                ) and _verify_sha256(
                    onnx_pair[1], _ALLOWED_TOKENIZER_SHA256, "MESH_TOKENIZER_SHA256", source_label=str(onnx_pair[1])
                ):
                    was_warming = _model_backend == "onnx-warming"
                    _model = _DirectOnnxEmbedding(onnx_pair[0], onnx_pair[1])
                    _model_backend = "onnx"
                    _record_model_status(state="ready", backend="onnx", last_error=None)
                    if was_warming:
                        with _EMBED_CACHE_LOCK:
                            _EMBED_CACHE.clear()
                    _ensure_idle_watchdog()
                    return _model, _model_backend
            except Exception as exc:
                err_msg = f"Failed loading local ONNX model: {exc}"
                _emit_warning(err_msg)
                _record_model_status(state="onnx_load_error", backend="hash-fallback", last_error=err_msg)

        force_fastembed = os.environ.get("MESH_FORCE_FASTEMBED", "").strip().lower() in ("1", "true", "yes")
        if force_fastembed:
            try:
                from fastembed import TextEmbedding

                _model = TextEmbedding(model_name="sentence-transformers/all-MiniLM-L6-v2")
                _model_backend = "fastembed"
                _record_model_status(state="ready", backend="fastembed", last_error=None)
                _ensure_idle_watchdog()
                return _model, _model_backend
            except Exception:
                pass

        if force_pytorch:
            try:
                import torch as torch_mod

                _torch = torch_mod
                from sentence_transformers import SentenceTransformer

                _model = SentenceTransformer(_MODEL_NAME)
                _model_backend = "sentence-transformers"
                _record_model_status(state="ready", backend="sentence-transformers", last_error=None)
                _ensure_idle_watchdog()
                return _model, _model_backend
            except Exception:
                pass

        if _request_model_download_permission(assume_yes=False):
            _start_background_model_download()
        _model = _hash_embed_384
        _model_backend = "onnx-warming"
        _ensure_idle_watchdog()
        return _model, _model_backend


def _backend_to_model_tag(backend: Optional[str], model_obj: Any) -> str:
    if backend in ("onnx-warming", "hash-fallback", None) or model_obj is _hash_embed_384:
        return "hash-384"
    return "onnx-minilm-l6-v2"


def _current_embedding_model_tag() -> str:
    """Return the canonical storage tag for the active embedding backend."""
    model, backend = _get_model()
    return _backend_to_model_tag(backend, model)


def _encode_uncached_texts_with_tag(texts: list[str]) -> tuple[np.ndarray, str]:
    """Encode uncached strings into L2-normalized float32 rows and return `(vectors, model_tag)` atomically."""
    global _last_model_use_ts
    model, backend = _get_model()
    _last_model_use_ts = time.monotonic()
    tag = _backend_to_model_tag(backend, model)

    if isinstance(model, _DirectOnnxEmbedding):
        return model.encode(texts, batch_size=min(32, max(1, len(texts)))), "onnx-minilm-l6-v2"

    if tag == "hash-384":
        vecs = np.vstack([_hash_embed_384(t) for t in texts])
        return _normalize_rows(vecs), "hash-384"

    if backend == "fastembed":
        gen = model.embed(texts, batch_size=min(32, max(1, len(texts))))
        vecs = np.asarray(list(gen), dtype=np.float32)
        return _normalize_rows(vecs), "onnx-minilm-l6-v2"

    try:
        if _torch is not None:
            with _torch.inference_mode():
                vecs = model.encode(
                    texts if len(texts) > 1 else texts[0],
                    batch_size=32,
                    show_progress_bar=False,
                    normalize_embeddings=True,
                    convert_to_numpy=True,
                )
        else:
            vecs = model.encode(
                texts if len(texts) > 1 else texts[0],
                batch_size=32,
                show_progress_bar=False,
                normalize_embeddings=True,
                convert_to_numpy=True,
            )
    except TypeError:
        vecs = model.encode(
            texts if len(texts) > 1 else texts[0],
            show_progress_bar=False,
        )
    arr = np.asarray(vecs, dtype=np.float32)
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    return _normalize_rows(arr), "onnx-minilm-l6-v2"


def _store_in_embed_cache_locked(cleaned_text: str, vec: np.ndarray, model_tag: str) -> np.ndarray:
    maxsize = _get_embed_cache_maxsize()
    arr_ro = _make_readonly_f32(vec)
    if maxsize <= 0:
        _EMBED_CACHE.clear()
        return arr_ro
    _EMBED_CACHE[cleaned_text] = (arr_ro, model_tag)
    _EMBED_CACHE.move_to_end(cleaned_text)
    while len(_EMBED_CACHE) > maxsize:
        _EMBED_CACHE.popitem(last=False)
    return arr_ro


def _encode_texts_with_tag(texts: list[str]) -> tuple[np.ndarray, str]:
    """
    Encode a list of strings into a 2D array of L2-normalized float32 embeddings
    and return `(array, model_tag)` atomically.
    """
    if not texts:
        return np.empty((0, EMBEDDING_DIM), dtype=np.float32), _current_embedding_model_tag()

    active_tag = _current_embedding_model_tag()
    cleaned_list = [" ".join(str(t).strip().split()) for t in texts]
    results: list[Optional[np.ndarray]] = [None] * len(cleaned_list)
    tags_seen: list[str] = []
    missing_by_text: dict[str, list[int]] = {}

    with _EMBED_CACHE_LOCK:
        for idx, ctext in enumerate(cleaned_list):
            cached = _EMBED_CACHE.get(ctext)
            if cached is not None and (cached[1] == active_tag or active_tag == "hash-384"):
                _EMBED_CACHE.move_to_end(ctext)
                results[idx] = cached[0]
                tags_seen.append(cached[1])
            else:
                missing_by_text.setdefault(ctext, []).append(idx)

    if missing_by_text:
        unique_misses = list(missing_by_text.keys())
        vecs, miss_tag = _encode_uncached_texts_with_tag(unique_misses)
        tags_seen.append(miss_tag)
        with _EMBED_CACHE_LOCK:
            for ctext, vec in zip(unique_misses, vecs):
                arr_ro = _store_in_embed_cache_locked(ctext, vec, miss_tag)
                for target_idx in missing_by_text[ctext]:
                    results[target_idx] = arr_ro

    final_tag = "hash-384" if "hash-384" in tags_seen else (tags_seen[0] if tags_seen else active_tag)
    return np.vstack(results).astype(np.float32, copy=False), final_tag


def _encode_texts(texts: list[str]) -> np.ndarray:
    """Encode a list of strings into a 2D array of L2-normalized float32 embeddings."""
    arr, _ = _encode_texts_with_tag(texts)
    return arr


def warmup_model(
    namespace: Optional[str] = None,
    blocking: bool = False,
    assume_yes: bool = False,
) -> None:
    """
    Pre-load the embedding engine and (ONLY if the target namespace database already
    exists on disk) warm its vector cache and heal pending warm-up embeddings (Phase 3.4).
    Never creates an empty `mesh_shared.db` file on disk.
    """
    try:
        ensure_model_downloaded(blocking=blocking, timeout=10.0, assume_yes=assume_yes)
        _embed_cached_with_tag("warmup")
        ns = validate_namespace((namespace or os.environ.get("MESH_NAMESPACE", "shared")).strip() or "shared")
        db_file = ensure_secure_mesh_dir().resolve() / f"mesh_{ns}.db"
        if db_file.exists():
            mesh = Mesh(namespace=ns, agent_id="warmup", create=False)
            mesh._heal_namespace_embeddings_if_needed()
    except Exception:
        pass


def _embed_cached_with_tag(cleaned_text: str) -> tuple[np.ndarray, str]:
    """O(1) LRU-cached embedding helper returning `(readonly_float32_ndarray, model_tag)` atomically."""
    with _EMBED_CACHE_LOCK:
        cached = _EMBED_CACHE.get(cleaned_text)
        if cached is not None:
            if _model is not None:
                tag = _backend_to_model_tag(_model_backend, _model)
                if cached[1] != tag and tag != "hash-384":
                    cached = None
            if cached is not None:
                _EMBED_CACHE.move_to_end(cleaned_text)
                return cached

    arr, model_tag = _encode_uncached_texts_with_tag([cleaned_text])
    with _EMBED_CACHE_LOCK:
        arr_ro = _store_in_embed_cache_locked(cleaned_text, arr[0], model_tag)
    return arr_ro, model_tag


def _embed_cached(cleaned_text: str) -> np.ndarray:
    vec_arr, _ = _embed_cached_with_tag(cleaned_text)
    return vec_arr


def _embed_with_tag(text: str) -> tuple[list[float], str]:
    """Embed a single text string and return `(vector_list, embedding_model_tag)` atomically."""
    cleaned = " ".join(text.strip().split())
    vec_arr, model_tag = _embed_cached_with_tag(cleaned)
    return vec_arr.tolist(), model_tag


def _embed(text: str) -> list[float]:
    """Embed a single text string into a normalized float vector."""
    vec, _ = _embed_with_tag(text)
    return vec


def _embed_query_with_tag(query_text: str) -> tuple[list[float], str]:
    """
    Embed a search query, softly blending domain synonyms for short queries,
    and return (vector_list, query_embedding_model_tag).
    """
    cleaned = " ".join(query_text.strip().split())
    base_arr, tag = _embed_cached_with_tag(cleaned)
    base_vec = np.asarray(base_arr, dtype=np.float32)

    _, content_tokens, _ = _extract_search_tokens(cleaned)
    if not content_tokens or len(content_tokens) > 5:
        return base_vec.tolist(), tag

    syn_map = expand_query_tokens(content_tokens)
    added_syns: list[str] = []
    for tok in content_tokens:
        for s in sorted(syn_map.get(tok, set()))[:3]:
            if s not in added_syns:
                added_syns.append(s)

    if not added_syns:
        return base_vec.tolist(), tag

    expanded_text = f"{cleaned} {' '.join(added_syns[:8])}"
    exp_arr, _ = _embed_cached_with_tag(expanded_text)
    exp_vec = np.asarray(exp_arr, dtype=np.float32)
    blended = _normalize_rows(0.85 * base_vec + 0.15 * exp_vec)
    return blended.tolist(), tag


def _embed_query(query_text: str) -> list[float]:
    """
    Embed a search query, softly blending domain synonyms for short queries.
    """
    vec, _ = _embed_query_with_tag(query_text)
    return vec


def _embed_batch_with_tag(texts: list[str]) -> tuple[list[list[float]], str]:
    if not texts:
        return [], _current_embedding_model_tag()
    if len(texts) == 1:
        vec, tag = _embed_with_tag(texts[0])
        return [vec], tag
    arr, tag = _encode_texts_with_tag(texts)
    return [row.tolist() for row in arr], tag


def _embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed multiple texts in a single batch forward pass."""
    vecs, _ = _embed_batch_with_tag(texts)
    return vecs


# ==============================================================================
# Edit Distance & Namespace Sprawl Helpers (Phase 3.4)
# ==============================================================================


def _levenshtein_distance(s1: str, s2: str) -> int:
    if s1 == s2:
        return 0
    if len(s1) < len(s2):
        s1, s2 = s2, s1
    if not s2:
        return len(s1)
    prev_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = prev_row[j + 1] + 1
            deletions = curr_row[j] + 1
            substitutions = prev_row[j] + (c1 != c2)
            curr_row.append(min(insertions, deletions, substitutions))
        prev_row = curr_row
    return prev_row[-1]


def _list_existing_namespace_names() -> list[str]:
    mesh_dir = get_mesh_dir()
    if not mesh_dir.exists():
        return []
    names: list[str] = []
    for p in mesh_dir.glob("mesh_*.db"):
        if not (p.is_file() and not p.name.endswith(("-wal", "-shm"))):
            continue
        stem = p.stem
        ns = stem[5:] if stem.startswith("mesh_") else stem
        try:
            validate_namespace(ns)
            names.append(ns)
        except ValueError:
            continue
    return sorted(set(names))


def find_similar_namespaces(target_ns: str, max_distance: int = 2) -> list[str]:
    """Return existing namespaces within edit distance <= `max_distance` of `target_ns`."""
    t_low = (target_ns or "").strip().lower()
    if not t_low:
        return []
    similar: list[str] = []
    for existing in _list_existing_namespace_names():
        if existing == target_ns:
            continue
        dist = _levenshtein_distance(t_low, existing.lower())
        if 0 < dist <= max_distance:
            similar.append(existing)
    return similar


def suggest_did_you_mean_namespaces(target_ns: str, n: int = 3) -> list[str]:
    """Return close matches for a missing namespace using `difflib.get_close_matches` + edit distance."""
    existing = _list_existing_namespace_names()
    if not existing:
        return []
    close = difflib.get_close_matches(target_ns, existing, n=n, cutoff=0.55)
    by_dist = find_similar_namespaces(target_ns, max_distance=2)
    return list(dict.fromkeys([*by_dist, *close]))[:n]


def _compute_decay_metrics(
    ttl_days: Optional[float],
    decay_start_at: Optional[str],
    now: Optional[datetime] = None,
) -> tuple[float, Optional[int]]:
    """Compute (decay_factor, days_until_stale) in a single timestamp parse pass."""
    if ttl_days is None or not decay_start_at:
        return 1.0, None
    try:
        ttl = float(ttl_days)
        if ttl <= 0.0:
            return 0.0, 0
        start = _parse_iso_utc(decay_start_at)
        if start is None:
            return 1.0, None
        ref_now = now if now is not None else _utc_now()
        days_elapsed = max(0.0, (ref_now - start).total_seconds() / 86400.0)
        decay_factor = max(0.0, min(1.0, 1.0 - (days_elapsed / ttl)))
        stale_threshold_day = ttl * (1.0 - STALE_THRESHOLD)
        days_until_stale = max(0, round(stale_threshold_day - days_elapsed))
        return decay_factor, days_until_stale
    except Exception:
        return 1.0, None


def _compute_decay_factor(
    ttl_days: Optional[float],
    decay_start_at: Optional[str],
    now: Optional[datetime] = None,
) -> float:
    factor, _ = _compute_decay_metrics(ttl_days, decay_start_at, now=now)
    return factor


def _compute_days_until_stale(
    ttl_days: Optional[float],
    decay_start_at: Optional[str],
    now: Optional[datetime] = None,
) -> Optional[int]:
    _, days_left = _compute_decay_metrics(ttl_days, decay_start_at, now=now)
    return days_left


def _parse_tags(raw_tags: Any) -> list[str]:
    """Safely parse tags from a list, JSON string, or comma-separated string."""
    if not raw_tags:
        return []
    if isinstance(raw_tags, list):
        return [str(t).strip() for t in raw_tags if str(t).strip()]
    if isinstance(raw_tags, str):
        s = raw_tags.strip()
        if not s or s == "[]":
            return []
        if s.startswith("["):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return [str(t).strip() for t in parsed if str(t).strip()]
            except Exception:
                pass
        return [t.strip() for t in s.split(",") if t.strip()]
    return []


def _extract_numeric_and_alnum_signatures(norm_text: str) -> tuple[list[str], set[str]]:
    """
    Extract all digit sequences (including digits attached to letters like `db1`, `v2`, `sha256`)
    and alphanumeric identifier tokens containing digits.
    """
    cleaned = _normalize_text_for_tokens(norm_text)
    nums = _NUMBER_TOKEN_RE.findall(cleaned)
    alnum_ids = set(_ALNUM_ID_WITH_DIGITS_RE.findall(cleaned))
    return nums, alnum_ids


def _is_true_duplicate(new_text: str, existing_text: str, similarity: float) -> bool:
    """
    Determine whether `new_text` is a semantic duplicate of `existing_text`
    rather than a conflicting update or a distinct long note sharing the same opening.
    """
    norm_new = " ".join(new_text.strip().lower().split())
    norm_old = " ".join(existing_text.strip().lower().split())
    if norm_new == norm_old:
        return True
    if similarity < DEDUP_THRESHOLD:
        return False

    nums_new, alnum_new = _extract_numeric_and_alnum_signatures(norm_new)
    nums_old, alnum_old = _extract_numeric_and_alnum_signatures(norm_old)
    if nums_new != nums_old or alnum_new != alnum_old:
        return False

    words_new_list = _WORD_TOKEN_RE.findall(_normalize_text_for_tokens(norm_new))
    words_old_list = _WORD_TOKEN_RE.findall(_normalize_text_for_tokens(norm_old))
    words_new = set(words_new_list)
    words_old = set(words_old_list)
    if bool(words_new & _NEGATION_TOKENS) != bool(words_old & _NEGATION_TOKENS):
        return False

    # Guard against long notes sharing an opening paragraph deduplicating into each other
    if len(norm_new) > 350 or len(norm_old) > 350:
        max_len = max(len(norm_new), len(norm_old), 1)
        min_len = min(len(norm_new), len(norm_old))
        if min_len / max_len < 0.88:
            return False
        union_words = words_new | words_old
        if union_words and (len(words_new & words_old) / len(union_words)) < 0.88:
            return False

    return True


_SUBJECT_VALUE_RE = re.compile(
    r"^(.+?)\s*(?:\b(?:is|are|was|equals)\b|(?<![<>=!])=(?!=)|:(?=\s|$))\s*(.+)$"
)


def _extract_subject_value_pair(norm_text: str) -> Optional[tuple[str, str]]:
    """
    Extract `(subject_phrase, value_phrase)` for copula or key-value statements (Phase 2.6):
      - "the user's pen is blue" -> ("user pen", "blue")
      - "staging redis port is 6380" -> ("staging redi port", "6380")
      - "port = 5432" / "port=5432" / "port: 5432" -> ("port", "5432")
      - "url is http://a:80/x" -> ("url", "http a 80 x")
      - "a == b" / "a <= b" / "10:30" -> None
    """
    cleaned = _normalize_text_for_tokens(norm_text)
    m = _SUBJECT_VALUE_RE.match(cleaned)
    if not m:
        return None
    subj_tokens = [
        _stem_word(w)
        for w in _WORD_TOKEN_RE.findall(m.group(1))
        if w not in _STOPWORDS and w not in _NEGATION_TOKENS
    ]
    val_tokens = [
        _stem_word(w)
        for w in _WORD_TOKEN_RE.findall(m.group(2))
        if w not in _STOPWORDS or w in _NEGATION_TOKENS
    ]
    if not subj_tokens or not val_tokens:
        return None
    return (" ".join(subj_tokens), " ".join(val_tokens))


def _extract_number_contexts(norm_text: str) -> dict[str, set[str]]:
    """
    Map each content anchor word or prefix attached to a number to the set of numbers
    associated with it (e.g. "postgresql 16 on port 5432" -> {"postgresql": {"16"}, "port": {"5432"}},
    and "db1" vs "db2" -> {"alnum:db": {"1"}} vs {"alnum:db": {"2"}}).
    """
    cleaned = _normalize_text_for_tokens(norm_text)
    tokens = _WORD_TOKEN_RE.findall(cleaned)
    slots: dict[str, set[str]] = {}
    for tok in tokens:
        m_alnum = re.fullmatch(r"([a-z_]+)(\d+(?:\.\d+)?)([a-z_]*)", tok)
        if m_alnum:
            prefix, num_part, suffix = m_alnum.groups()
            slots.setdefault(f"alnum:{prefix}_{suffix}", set()).add(num_part)

    for idx, tok in enumerate(tokens):
        if not _NUMBER_TOKEN_RE.fullmatch(tok):
            continue
        prev_words = [
            _stem_word(tokens[j])
            for j in range(max(0, idx - 2), idx)
            if not _NUMBER_TOKEN_RE.fullmatch(tokens[j]) and tokens[j] not in _STOPWORDS
        ]
        next_words = [
            _stem_word(tokens[j])
            for j in range(idx + 1, min(len(tokens), idx + 3))
            if not _NUMBER_TOKEN_RE.fullmatch(tokens[j]) and tokens[j] not in _STOPWORDS
        ]
        if prev_words:
            slots.setdefault(f"prev:{prev_words[-1]}", set()).add(tok)
        elif next_words:
            slots.setdefault(f"next:{next_words[0]}", set()).add(tok)
        else:
            slots.setdefault("unanchored", set()).add(tok)
    return slots


def _has_numeric_slot_conflict(norm_new: str, norm_old: str) -> bool:
    """
    Return True only if `norm_new` and `norm_old` assign conflicting numbers to the
    same contextual slot (e.g. "port 5432" vs "port 5433", or "db1" vs "db2").
    Elaborations that add a version number while agreeing on shared slots
    (e.g. "PostgreSQL 16 runs on port 5432" vs "PostgreSQL runs on port 5432") return False.
    """
    nums_new, alnum_new = _extract_numeric_and_alnum_signatures(norm_new)
    nums_old, alnum_old = _extract_numeric_and_alnum_signatures(norm_old)
    if nums_new == nums_old and alnum_new == alnum_old:
        return False
    if not nums_new or not nums_old:
        return False

    slots_new = _extract_number_contexts(norm_new)
    slots_old = _extract_number_contexts(norm_old)
    shared_keys = set(slots_new.keys()) & set(slots_old.keys())
    if shared_keys:
        for k in shared_keys:
            if slots_new[k] != slots_old[k]:
                return True
        set_new, set_old = set(nums_new), set(nums_old)
        if set_new.issubset(set_old) or set_old.issubset(set_new):
            return False

    if len(nums_new) == len(nums_old) and set(nums_new) != set(nums_old):
        return True
    if not (set(nums_new) & set(nums_old)):
        return True
    return False


def _is_true_contradiction(new_text: str, existing_text: str, similarity: float) -> bool:
    """
    Determine whether `new_text` genuinely conflicts with `existing_text`
    rather than elaborating on the same compatible fact.
    """
    if _is_true_duplicate(new_text, existing_text, similarity):
        return False

    norm_new = " ".join(new_text.strip().lower().split())
    norm_old = " ".join(existing_text.strip().lower().split())
    if norm_new in norm_old or norm_old in norm_new:
        return False

    tokens_new = set(_WORD_TOKEN_RE.findall(_normalize_text_for_tokens(norm_new)))
    tokens_old = set(_WORD_TOKEN_RE.findall(_normalize_text_for_tokens(norm_old)))
    neg_new = bool(tokens_new & _NEGATION_TOKENS)
    neg_old = bool(tokens_old & _NEGATION_TOKENS)

    # 1. Check direct subject-value slot conflict ("the user's pen is blue" vs "the user's pen is red")
    sv_new = _extract_subject_value_pair(norm_new)
    sv_old = _extract_subject_value_pair(norm_old)
    if sv_new is not None and sv_old is not None:
        if sv_new[0] == sv_old[0] and sv_new[1] != sv_old[1]:
            # A shared subject that assigns different numbers to the same slot is
            # structural evidence, so it must not depend on embedding similarity:
            # the hash-384 fallback cannot produce the cosine the gate expects.
            if _has_numeric_slot_conflict(norm_new, norm_old):
                return True
            if similarity >= 0.70:
                if neg_new != neg_old:
                    return True
                val_words_new = set(sv_new[1].split())
                val_words_old = set(sv_old[1].split())
                if not (val_words_new.issubset(val_words_old) or val_words_old.issubset(val_words_new)):
                    return True

    if similarity < CONTRADICTION_THRESHOLD:
        return False

    # 2. Negation polarity flip
    if neg_new != neg_old:
        return True

    # 3. Contextual numeric slot conflict
    if _has_numeric_slot_conflict(norm_new, norm_old):
        return True

    # 4. Exclusive color conflict on highly similar sentences
    colors_new = tokens_new & _COLOR_TOKENS
    colors_old = tokens_old & _COLOR_TOKENS
    if colors_new and colors_old and colors_new != colors_old:
        return True

    return False


def _resolve_sdk_include_private(include_private: Optional[bool]) -> bool:
    if include_private is not None:
        return bool(include_private)
    env_val = os.environ.get("MESH_INCLUDE_PRIVATE", "").strip().lower()
    if env_val in ("0", "false", "no"):
        return False
    return True


def _check_agent_ownership(agent_id: str, existing_agent: Optional[str]) -> None:
    if os.environ.get("MESH_ENFORCE_AGENT_OWNERSHIP", "").strip().lower() in ("1", "true", "yes"):
        if existing_agent and existing_agent != agent_id and agent_id not in ("human-cli", "dashboard", "cli"):
            raise ValueError(
                f"Agent '{agent_id}' is not authorized to modify memory owned by '{existing_agent}'."
            )


class Mesh:
    """
    Shared semantic & hybrid memory for AI agents.

    Agent ID defaults:
      - Python SDK (`Mesh`): `agent_id` param -> `MESH_AGENT_ID` env var -> `"agent"`
      - MCP server (`mesh-server`): `MESH_AGENT_ID` env var -> `"mcp-agent"`
      - Store fallback (`MeshStore`): `"unknown"`
    """

    def __init__(
        self,
        namespace: Optional[str] = None,
        agent_id: Optional[str] = None,
        create: bool = True,
    ):
        raw_ns = (namespace or os.environ.get("MESH_NAMESPACE", "shared")).strip() or "shared"
        self.namespace = validate_namespace(raw_ns)
        resolved_agent = (
            agent_id
            if (agent_id is not None and str(agent_id).strip())
            else (os.environ.get("MESH_AGENT_ID", "").strip() or "agent")
        )
        clean_agent = re.sub(r"[\x00-\x1f\x7f]+", "", str(resolved_agent)).strip()[:64]
        self.agent_id = clean_agent or "agent"
        db_file = ensure_secure_mesh_dir().resolve() / f"mesh_{self.namespace}.db"
        self._ns_existed_before = db_file.exists()
        self.store = MeshStore(self.namespace, create=create)
        _trigger_background_bootstrap()

    def _heal_namespace_embeddings_if_needed(self, max_batch: int = 8) -> None:
        """
        O(1) check via `cache.needs_healing_ids`: if a real neural model (`onnx`, `fastembed`, or
        `sentence-transformers`) is active and the namespace contains any rows embedded
        with `hash-384` or `needs-reembed`, re-embed at most `max_batch` (default 8) rows
        synchronously and spawn at most one background healing thread per namespace (Phase 3.2).
        Never runs inside `namespace_write_lock`.
        """
        if not self.store.db_path.exists():
            return
        with _HEALING_IN_FLIGHT_LOCK:
            if self.namespace in _HEALING_IN_FLIGHT:
                return

        model_obj, backend = _get_model()
        if _backend_to_model_tag(backend, model_obj) != "onnx-minilm-l6-v2":
            return

        try:
            cache = self.store._get_vector_cache()
            if not cache.needs_healing_ids:
                return
            stale_ids = list(cache.needs_healing_ids)
            sync_ids = stale_ids[: max(0, int(max_batch))]
            remaining_ids = stale_ids[max(0, int(max_batch)) :]

            def _reembed_ids(id_list: list[str]) -> None:
                to_reembed_ids: list[str] = []
                to_reembed_texts: list[str] = []
                for mid in id_list:
                    row = self.store.get(mid)
                    if row and row.get("content"):
                        to_reembed_ids.append(mid)
                        to_reembed_texts.append(row["content"])
                if not to_reembed_texts:
                    return
                new_vecs, tag = _encode_texts_with_tag(to_reembed_texts)
                if tag != "onnx-minilm-l6-v2":
                    return
                for mid, text, vec in zip(to_reembed_ids, to_reembed_texts, new_vecs):
                    latest = self.store.get(mid)
                    if latest and latest.get("content") == text:
                        self.store.update(
                            memory_id=mid,
                            content=None,
                            embedding=vec.tolist(),
                            embedding_model="onnx-minilm-l6-v2",
                            preserve_accessed_at=True,
                        )

            if sync_ids:
                _reembed_ids(sync_ids)
            if remaining_ids:
                with _HEALING_IN_FLIGHT_LOCK:
                    if self.namespace in _HEALING_IN_FLIGHT:
                        return
                    _HEALING_IN_FLIGHT.add(self.namespace)

                def _bg_heal_worker(ids_to_heal: list[str], ns_name: str) -> None:
                    try:
                        _reembed_ids(ids_to_heal)
                    except Exception:
                        pass
                    finally:
                        with _HEALING_IN_FLIGHT_LOCK:
                            _HEALING_IN_FLIGHT.discard(ns_name)

                threading.Thread(
                    target=_bg_heal_worker,
                    args=(remaining_ids, self.namespace),
                    name=f"mesh-healing-{self.namespace}",
                    daemon=True,
                ).start()
        except Exception:
            pass

    def learn(
        self,
        content: str,
        memory_type: str = "fact",
        confidence: float = 1.0,
        tags: Optional[list[str]] = None,
        ttl_days: Optional[float] = None,
        local_only: bool = False,
        deduplicate: bool = True,
    ) -> dict[str, Any]:
        if not content or not content.strip():
            raise ValueError("content cannot be empty")
        clean_content = content.strip()
        if len(clean_content) > MAX_CONTENT_LENGTH:
            raise ValueError(
                f"content length ({len(clean_content)} chars) exceeds maximum allowed length ({MAX_CONTENT_LENGTH} chars)."
            )

        memory_type = (memory_type or "fact").strip().lower()
        if memory_type not in VALID_MEMORY_TYPES:
            raise ValueError(
                f"memory_type must be one of {sorted(VALID_MEMORY_TYPES)}, got '{memory_type}'"
            )

        confidence = max(0.0, min(1.0, float(confidence)))
        clean_tags = _parse_tags(tags)
        ttl_val = float(ttl_days) if ttl_days is not None and float(ttl_days) > 0 else None
        now = _utc_now_iso()

        self._heal_namespace_embeddings_if_needed()
        # Compute embedding BEFORE taking the namespace write lock (Phase 2.2)
        embedding, emb_model_tag = _embed_with_tag(clean_content)
        _, active_backend = _get_model()
        is_degraded = emb_model_tag != "onnx-minilm-l6-v2"
        memory_id = str(uuid.uuid4())

        # Check whether this learn() is creating a brand-new namespace (Phase 3.4)
        was_new_namespace = not self._ns_existed_before and self.store.count() == 0
        similar_existing = (
            find_similar_namespaces(self.namespace, max_distance=2)
            if was_new_namespace
            else []
        )

        # --- Atomic Deduplication, Contradiction Detection & Write (Phase 2.2) ---
        conflict: Optional[dict[str, Any]] = None
        with namespace_write_lock(self.namespace):
            # Force vector cache sync check inside the write lock so cross-process writes are visible
            self.store._get_vector_cache()
            if self.store.count() > 0:
                raw = self.store.query(embedding, n=3, query_embedding_model=emb_model_tag)
                docs = raw.get("documents", [[]])[0]
                distances = raw.get("distances", [[]])[0]
                ids = raw.get("ids", [[]])[0]
                metadatas = raw.get("metadatas", [[]])[0]

                # Pass 1: Check if any top candidate is a true duplicate (preserves original source_agent & memory_type)
                if deduplicate:
                    for i, doc in enumerate(docs):
                        meta = metadatas[i] if metadatas and i < len(metadatas) else {}
                        if not _is_same_known_space(meta.get("embedding_model"), emb_model_tag):
                            continue
                        similarity = round(1.0 - float(distances[i]), 4)
                        existing_id = ids[i] if ids and i < len(ids) else None
                        existing_is_priv = bool(meta.get("local_only"))
                        if existing_is_priv and not local_only:
                            continue
                        if existing_id and _is_true_duplicate(clean_content, doc, similarity):
                            existing_tags = _parse_tags(meta.get("tags"))
                            merged_tags = list(dict.fromkeys([*existing_tags, *clean_tags]))
                            updated_conf = max(confidence, float(meta.get("confidence", 1.0)))
                            content_changed = clean_content != doc
                            is_priv = bool(local_only or meta.get("local_only"))
                            self.store.update(
                                memory_id=existing_id,
                                content=clean_content if content_changed else None,
                                embedding=embedding if content_changed else None,
                                memory_type=None,   # Preserve original memory_type
                                confidence=updated_conf,
                                tags=merged_tags,
                                ttl_days=ttl_val,
                                reset_ttl=ttl_val is not None,
                                local_only=is_priv,
                                source_agent=None,  # Preserve original author provenance
                                embedding_model=emb_model_tag if content_changed else None,
                            )
                            if content_changed and emb_model_tag == "hash-384":
                                with _PENDING_REEMBED_LOCK:
                                    _PENDING_REEMBED_QUEUE.put(self.namespace, existing_id, clean_content)
                            dedup_res: dict[str, Any] = {
                                "memory_id": existing_id,
                                "status": "stored",
                                "deduplicated": True,
                                "conflict": None,
                                "embedding_backend": active_backend or "hash-fallback",
                                "embedding_degraded": is_degraded,
                            }
                            log_learn(
                                content=clean_content,
                                namespace=self.namespace,
                                agent_id=self.agent_id,
                                result=dedup_res,
                                local_only=is_priv,
                            )
                            return dedup_res

                # Pass 2: Check for genuine semantic contradiction against the closest existing memories
                for i, doc in enumerate(docs):
                    meta = metadatas[i] if metadatas and i < len(metadatas) else {}
                    if not _is_same_known_space(meta.get("embedding_model"), emb_model_tag):
                        continue
                    similarity = round(1.0 - float(distances[i]), 4)
                    if _is_true_contradiction(clean_content, doc, similarity):
                        existing_id = ids[i] if ids and i < len(ids) else None
                        existing_is_private = bool(meta.get("local_only"))
                        safe_existing_doc = (
                            REDACTED_CONTENT_PLACEHOLDER if existing_is_private else doc
                        )
                        contradiction_id = self.store.log_contradiction(
                            new_memory_id=memory_id,
                            new_content=clean_content,
                            existing_memory_id=existing_id or "",
                            existing_content=doc,
                            similarity=similarity,
                            local_only=bool(local_only),
                            existing_local_only=existing_is_private,
                        )
                        conflict = {
                            "existing_memory_id": existing_id,
                            "existing_content": safe_existing_doc,
                            "existing_memory": safe_existing_doc,
                            "existing_local_only": existing_is_private,
                            "similarity": similarity,
                            "stored_by": meta.get("source_agent", "unknown"),
                            "stored_at": meta.get("created_at", ""),
                            "contradiction_id": contradiction_id,
                        }
                        break

            decay_start_at = now if ttl_val is not None else None
            self.store.upsert(
                memory_id=memory_id,
                content=clean_content,
                embedding=embedding,
                metadata={
                    "memory_type": memory_type,
                    "confidence": confidence,
                    "source_agent": self.agent_id,
                    "tags": clean_tags,
                    "created_at": now,
                    "updated_at": now,
                    "accessed_at": now,
                    "access_count": 0,
                    "ttl_days": ttl_val,
                    "decay_start_at": decay_start_at,
                    "embedding_model": emb_model_tag,
                },
                local_only=bool(local_only),
                embedding_model=emb_model_tag,
            )
            self._ns_existed_before = True

        if emb_model_tag == "hash-384":
            with _PENDING_REEMBED_LOCK:
                _PENDING_REEMBED_QUEUE.put(self.namespace, memory_id, clean_content)

        result: dict[str, Any] = {
            "memory_id": memory_id,
            "status": "stored_with_conflict" if conflict else "stored",
            "deduplicated": False,
            "conflict": conflict,
            "embedding_backend": active_backend or "hash-fallback",
            "embedding_degraded": is_degraded,
        }
        if was_new_namespace:
            result["namespace_created"] = True
            result["similar_existing"] = similar_existing

        log_learn(
            content=clean_content,
            namespace=self.namespace,
            agent_id=self.agent_id,
            result=result,
            local_only=bool(local_only),
        )
        return result

    def recall(
        self,
        query: str,
        n: int = 5,
        min_similarity: float = 0.0,
        min_confidence: float = 0.0,
        include_stale: bool = True,
        memory_type: Optional[str] = None,
        tags: Optional[list[str]] = None,
        source_agent: Optional[str] = None,
        include_private: Optional[bool] = None,
        record_telemetry: bool = True,
        _precomputed_embedding: Optional[list[float]] = None,
        _precomputed_model_tag: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        if not query or not query.strip():
            raise ValueError("query cannot be empty")
        clean_query = query.strip()
        actual_include_private = _resolve_sdk_include_private(include_private)

        self._heal_namespace_embeddings_if_needed()
        query_model_tag: Optional[str] = None
        if _precomputed_embedding is not None:
            embedding = _precomputed_embedding
            query_model_tag = _precomputed_model_tag
        else:
            embedding, query_model_tag = _embed_query_with_tag(clean_query)
        now = _utc_now()
        candidate_limit = max(n * 4, 25)
        candidates = self.store.hybrid_query(
            query_text=clean_query,
            embedding=embedding,
            query_embedding_model=query_model_tag,
            n=candidate_limit,
            memory_type=memory_type,
            tags=_parse_tags(tags) if tags else None,
            source_agent=source_agent,
            include_private=actual_include_private,
            include_stale=include_stale,
            min_confidence=min_confidence,
            now=now,
        )

        memories: list[dict[str, Any]] = []
        best_similarity: Optional[float] = None
        max_lexical_in_candidates = max(
            (float(r.get("lexical_score", 0.0)) for r in candidates),
            default=0.0,
        )

        for row in candidates:
            similarity = float(row.get("similarity", 0.0))
            sem_sim = float(row.get("semantic_similarity", similarity))
            lex_score = float(row.get("lexical_score", 0.0))

            if best_similarity is None or similarity > best_similarity:
                best_similarity = similarity
            if similarity < min_similarity:
                continue

            if (
                min_similarity <= 0.0
                and max_lexical_in_candidates >= 0.55
                and lex_score <= 0.05
                and sem_sim < 0.35
                and similarity < (best_similarity or 0.0) * 0.42
            ):
                continue

            stored_confidence = float(row.get("confidence", 1.0))
            ttl_days_raw = row.get("ttl_days")
            ttl_days = float(ttl_days_raw) if ttl_days_raw is not None else None
            decay_start = row.get("decay_start_at") or (
                row.get("created_at") if ttl_days is not None else None
            )

            decay_factor, days_until_stale = _compute_decay_metrics(
                ttl_days, decay_start, now=now
            )
            effective_confidence = round(stored_confidence * decay_factor, 4)
            is_stale = effective_confidence < STALE_THRESHOLD

            if not include_stale and is_stale:
                continue
            if effective_confidence < min_confidence:
                continue

            mem_tags = (
                row["tags_list"]
                if "tags_list" in row
                else _parse_tags(row.get("tags"))
            )
            base_access = int(row.get("access_count") or 0)
            access_count = base_access + (1 if record_telemetry else 0)
            usage_boost = min(1.0, math.log1p(access_count) / 5.0)
            composite_score = round(
                0.72 * similarity + 0.22 * effective_confidence + 0.06 * usage_boost,
                4,
            )

            memories.append(
                {
                    "id": row.get("id"),
                    "content": row["content"],
                    "snippet": row.get("snippet") or row["content"][:110],
                    "match_reason": row.get("match_reason", "hybrid"),
                    "similarity": similarity,
                    "semantic_similarity": sem_sim,
                    "lexical_score": lex_score,
                    "composite_score": composite_score,
                    "source_agent": row.get("source_agent", "unknown"),
                    "memory_type": row.get("memory_type", "fact"),
                    "tags": mem_tags,
                    "confidence": stored_confidence,
                    "effective_confidence": effective_confidence,
                    "decay_factor": round(decay_factor, 4),
                    "is_stale": is_stale,
                    "days_until_stale": days_until_stale,
                    "ttl_days": ttl_days,
                    "access_count": access_count,
                    "local_only": bool(row.get("local_only", False)),
                    "created_at": row.get("created_at", ""),
                    "accessed_at": row.get("accessed_at", ""),
                }
            )

        memories.sort(
            key=lambda x: (
                x["composite_score"],
                x["similarity"],
                x["effective_confidence"],
                x["access_count"],
            ),
            reverse=True,
        )
        memories = memories[:n]

        if record_telemetry and self.store.db_path.exists():
            returned_ids = [m["id"] for m in memories if m.get("id")]
            is_priv_query = bool(
                include_private is True or any(m.get("local_only") for m in memories)
            )
            try:
                self.store.record_recall_telemetry(
                    memory_ids=returned_ids,
                    query_text=clean_query,
                    agent_id=self.agent_id,
                    best_similarity=best_similarity,
                    result_count=len(memories),
                    private_query=is_priv_query,
                )
            except Exception:
                pass
            log_recall(
                query=clean_query,
                namespace=self.namespace,
                agent_id=self.agent_id,
                results=memories,
            )
        return memories

    def recall_with_context(
        self,
        query: str,
        n: int = 5,
        min_similarity: float = 0.0,
        min_confidence: float = 0.0,
        include_stale: bool = True,
        memory_type: Optional[str] = None,
        tags: Optional[list[str]] = None,
        source_agent: Optional[str] = None,
        max_other_possible: int = 5,
        include_private: Optional[bool] = None,
    ) -> dict[str, Any]:
        """
        Execute `recall()` and also surface `other_possible_memories`, computing
        `_embed_query_with_tag()` only once across both stages (Phase 2.1).
        """
        clean_query = (query or "").strip()
        if not clean_query:
            raise ValueError("query cannot be empty")

        actual_include_private = _resolve_sdk_include_private(include_private)
        query_embedding, query_model_tag = _embed_query_with_tag(clean_query)
        _, active_backend = _get_model()

        # Pass `include_private` as received so `recall()` knows whether `include_private=True` was explicit
        results = self.recall(
            query=clean_query,
            n=n,
            min_similarity=min_similarity,
            min_confidence=min_confidence,
            include_stale=include_stale,
            memory_type=memory_type,
            tags=tags,
            source_agent=source_agent,
            include_private=include_private,
            _precomputed_embedding=query_embedding,
            _precomputed_model_tag=query_model_tag,
        )

        exclude_ids = {m["id"] for m in results if m.get("id")}
        other_possible: list[dict[str, Any]] = []
        if max_other_possible > 0 and hasattr(self.store, "discover_other_possible"):
            other_possible = self.store.discover_other_possible(
                query_text=clean_query,
                embedding=query_embedding,
                exclude_ids=exclude_ids,
                limit=max_other_possible,
                active_memory_type=memory_type,
                active_tags=_parse_tags(tags) if tags else None,
                include_private=actual_include_private,
                query_embedding_model=query_model_tag,
            )

        filter_rescued = [
            item
            for item in other_possible
            if str(item.get("reason", "")).startswith("matched_in_other_type_or_tag")
        ]
        hint: Optional[str] = None
        if not results and filter_rescued:
            hint = (
                "No memories matched your strict memory_type/tags filter, but strong matches "
                "were found under other types/tags in `other_possible_memories`."
            )

        return {
            "query": clean_query,
            "namespace": self.namespace,
            "count": len(results),
            "total_in_namespace": self.count(),
            "stale_count": sum(1 for m in results if m.get("is_stale")),
            "results": results,
            "other_possible_memories": other_possible,
            "filter_hint": hint,
            "embedding_backend": active_backend or "hash-fallback",
            "embedding_degraded": query_model_tag != "onnx-minilm-l6-v2",
        }

    def update(
        self,
        memory_id: str,
        content: Optional[str] = None,
        memory_type: Optional[str] = None,
        confidence: Optional[float] = None,
        tags: Optional[list[str]] = None,
        ttl_days: Optional[float] = None,
        reset_ttl: bool = False,
        clear_ttl: bool = False,
        local_only: Optional[bool] = None,
        source_agent: Optional[str] = None,
    ) -> dict[str, Any]:
        if not memory_id or not memory_id.strip():
            raise ValueError("memory_id cannot be empty")

        resolved_id = self.store.resolve_id_prefix(memory_id)
        if not resolved_id:
            raise ValueError(f"Memory ID '{memory_id}' not found in namespace '{self.namespace}'")

        existing = self.store.get(resolved_id)
        if not existing:
            raise ValueError(f"Memory '{resolved_id}' not found in namespace '{self.namespace}'")
        _check_agent_ownership(self.agent_id, existing.get("source_agent"))
        old_content = str(existing.get("content", ""))

        new_embedding: Optional[list[float]] = None
        emb_model_tag: Optional[str] = None
        clean_content: Optional[str] = None
        if content is not None:
            if not content.strip():
                raise ValueError("content cannot be empty")
            clean_content = content.strip()
            if len(clean_content) > MAX_CONTENT_LENGTH:
                raise ValueError(
                    f"content length ({len(clean_content)} chars) exceeds maximum allowed length ({MAX_CONTENT_LENGTH} chars)."
                )
            if clean_content != old_content:
                new_embedding, emb_model_tag = _embed_with_tag(clean_content)

        clean_type: Optional[str] = None
        if memory_type is not None:
            clean_type = memory_type.strip().lower()
            if clean_type not in VALID_MEMORY_TYPES:
                raise ValueError(
                    f"memory_type must be one of {sorted(VALID_MEMORY_TYPES)}, got '{memory_type}'"
                )

        clean_conf: Optional[float] = None
        if confidence is not None:
            clean_conf = max(0.0, min(1.0, float(confidence)))

        clean_tags = _parse_tags(tags) if tags is not None else None
        should_clear_ttl = bool(clear_ttl) or (ttl_days is not None and float(ttl_days) <= 0)
        ttl_val = (
            None
            if should_clear_ttl
            else (float(ttl_days) if ttl_days is not None and float(ttl_days) > 0 else None)
        )

        updated = self.store.update(
            memory_id=resolved_id,
            content=clean_content,
            embedding=new_embedding,
            memory_type=clean_type,
            confidence=clean_conf,
            tags=clean_tags,
            ttl_days=ttl_val,
            reset_ttl=reset_ttl or (ttl_val is not None),
            clear_ttl=should_clear_ttl,
            local_only=local_only,
            source_agent=source_agent,  # Preserve original source_agent unless explicitly overridden
            embedding_model=emb_model_tag,
        )
        if not updated:
            raise ValueError(f"Failed to update memory '{resolved_id}'")

        if clean_content and emb_model_tag == "hash-384":
            with _PENDING_REEMBED_LOCK:
                _PENDING_REEMBED_QUEUE.put(self.namespace, resolved_id, clean_content)

        is_priv = bool(updated.get("local_only", False))
        log_update(
            memory_id=resolved_id,
            content=clean_content,
            namespace=self.namespace,
            agent_id=self.agent_id,
            local_only=is_priv,
            old_content=old_content,
        )
        return {
            "memory_id": resolved_id,
            "status": "updated",
            "memory": self._enrich_memory_row(updated),
        }

    def get(self, memory_id: str) -> Optional[dict[str, Any]]:
        resolved_id = self.store.resolve_id_prefix(memory_id) or memory_id
        row = self.store.get(resolved_id)
        if not row:
            return None
        return self._enrich_memory_row(row)

    def _enrich_memory_row(
        self,
        row: dict[str, Any],
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        stored_conf = float(row.get("confidence", 1.0))
        ttl_raw = row.get("ttl_days")
        ttl_days = float(ttl_raw) if ttl_raw is not None else None
        decay_start = row.get("decay_start_at") or (
            row.get("created_at") if ttl_days is not None else None
        )
        decay_factor, days_until_stale = _compute_decay_metrics(
            ttl_days, decay_start, now=now
        )
        effective_conf = round(stored_conf * decay_factor, 4)
        is_stale = effective_conf < STALE_THRESHOLD

        enriched = dict(row)
        enriched["confidence"] = stored_conf
        enriched["effective_confidence"] = effective_conf
        enriched["decay_factor"] = round(decay_factor, 4)
        enriched["is_stale"] = is_stale
        enriched["days_until_stale"] = days_until_stale
        enriched["local_only"] = bool(row.get("local_only", False))
        enriched["tags_list"] = (
            row["tags_list"] if "tags_list" in row else _parse_tags(row.get("tags"))
        )
        return enriched

    def forget(self, memory_id: str) -> bool:
        resolved_id = self.store.resolve_id_prefix(memory_id) or memory_id
        existing = self.store.get(resolved_id)
        if existing:
            _check_agent_ownership(self.agent_id, existing.get("source_agent"))
        purged_content = existing.get("content") if existing else None
        deleted = self.store.delete(resolved_id)
        if deleted:
            log_forget(
                memory_id=resolved_id,
                namespace=self.namespace,
                agent_id=self.agent_id,
                purged_content=purged_content,
            )
        return deleted

    def patterns(self, include_private: bool = False) -> dict[str, Any]:
        return self.store.get_patterns(include_private=include_private)

    def contradictions(self, resolved: bool = False) -> list[dict[str, Any]]:
        return self.store.get_contradictions(resolved=resolved)

    def resolve_contradiction(self, contradiction_id: str) -> bool:
        return self.store.resolve_contradiction(contradiction_id)

    def inspect(
        self,
        limit: int = 20,
        memory_type: Optional[str] = None,
        tag: Optional[str] = None,
        tags: Optional[list[str]] = None,
        include_private: Optional[bool] = None,
        include_stale: bool = True,
        min_confidence: float = 0.0,
    ) -> list[dict[str, Any]]:
        actual_include_private = _resolve_sdk_include_private(include_private)
        rows = self.store.list_all(
            limit=limit,
            memory_type=memory_type,
            tag=tag,
            tags=tags,
            include_private=actual_include_private,
            include_stale=include_stale,
            min_confidence=min_confidence,
        )
        now = _utc_now()
        return [self._enrich_memory_row(r, now=now) for r in rows]

    def clear(self) -> None:
        self.store.clear_namespace()

    def count(self) -> int:
        return self.store.count()

    def close(self) -> None:
        """Flush any pending telemetry and release pooled resources for this namespace."""
        if hasattr(self, "store") and self.store is not None:
            self.store.flush_telemetry()

    def __enter__(self) -> "Mesh":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"Mesh(namespace='{self.namespace}', "
            f"agent_id='{self.agent_id}', "
            f"memories={self.count()})"
        )


def _emit_warning(msg: Any) -> None:
    """User-facing warning on stderr, styled like the rest of the CLI (plain fallback if ui fails)."""
    try:
        from . import ui

        ui.warn(" ".join(str(msg).split()))
    except Exception:
        sys.stderr.write(f"[Mesh WARNING] {msg}\n")

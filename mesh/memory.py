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
  - Security & Provenance Controls:
      - First-class `secret_ref` memory type and optional automatic keyring vaulting (Finding S1)
      - Per-agent access scopes (`MESH_AGENT_SCOPES` & `MESH_READ_ONLY`, Finding S6)
      - Provenance tiers (`trust_tier`: 'user' | 'agent' | 'log_captured'), quarantine filtering
        on `recall()`, and `Mesh.approve()` (Finding S8)
      - Full ingestion & update guard enforcement on content, tags, and visibility changes (Finding S11)
      - Recall query secret redaction and side-channel protection (Finding S12, N8)
      - Linear-time subject-value copula extraction preventing ReDoS inside write locks (Finding N3)
"""
from __future__ import annotations

import contextvars
import difflib
import gc
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Optional

if TYPE_CHECKING:
    import numpy as np
else:
    class _LazyNumPy:
        def __init__(self) -> None:
            self._lock = threading.Lock()

        def __getattr__(self, name: str) -> Any:
            with self._lock:
                import numpy as _np

                globals()["np"] = _np
                return getattr(_np, name)

    np = _LazyNumPy()


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
from .envutil import env_bool, env_float, env_int
from .secrets import (
    SecretLocator,
    convert_text_secrets_to_refs,
    is_credential,
    normalize_secret_ref,
    redact_secrets_local,
)
from .store import (
    EMBEDDING_DIM,
    STALE_THRESHOLD,
    MeshStore,
    _extract_search_tokens,
    _is_same_known_space,
    _normalize_trust_tier,
    _parse_iso_utc,
    _utc_now,
    _utc_now_iso,
    expand_query_tokens,
    namespace_write_lock,
)
from .version import VERSION

_MODEL_LOCK = threading.RLock()
_WARNED_MESSAGES: set[str] = set()
_WARNED_LOCK = threading.Lock()
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
# Note: Deprecated GCS bucket (storage.googleapis.com/qdrant-fastembed) was removed (Issue B-003)
_GCS_TARBALL_MIRRORS: list[str] = []
_HF_MODEL_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
_TOKENIZER_MIRRORS = [
    f"https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/{_HF_MODEL_REVISION}/tokenizer.json",
    f"https://hf-mirror.com/sentence-transformers/all-MiniLM-L6-v2/resolve/{_HF_MODEL_REVISION}/tokenizer.json",
]
_ONNX_MODEL_MIRRORS = [
    f"https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/{_HF_MODEL_REVISION}/onnx/model.onnx",
    f"https://hf-mirror.com/sentence-transformers/all-MiniLM-L6-v2/resolve/{_HF_MODEL_REVISION}/onnx/model.onnx",
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
_download_failed_until: float = 0.0
_DOWNLOAD_RETRY_SECONDS = 300.0
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
        _notify_reembed_worker()

    def append(self, item: tuple[str, str, str]) -> None:
        ns, mid, content = item
        self.put(ns, mid, content)

    def pop_some(self, count: int) -> list[tuple[str, str, str]]:
        items: list[tuple[str, str, str]] = []
        while self._data and len(items) < max(1, int(count)):
            (ns, mid), text = self._data.popitem(last=False)
            items.append((ns, mid, text))
        return items

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
_IDLE_UNLOAD_SECONDS: float = env_float("MESH_IDLE_UNLOAD_SECONDS", 180.0, minimum=0.0)

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
_EMBED_CACHE_BYTES: int = 0

_NORM_QUERY_CACHE: OrderedDict[str, tuple[list[float], str]] = OrderedDict()
_NORM_QUERY_CACHE_LOCK = threading.Lock()
_NORM_QUERY_MAXSIZE = 1024

# Maximum allowed characters in a single memory content string (16 KB)
MAX_CONTENT_LENGTH = env_int("MESH_MAX_CONTENT_LENGTH", 16384, minimum=1)

# Similarity threshold above which two memories are checked for contradiction
CONTRADICTION_THRESHOLD = 0.82

# Similarity threshold above which identical/near-identical facts are deduplicated
DEDUP_THRESHOLD = 0.985

# Absolute minimum similarity floor when min_similarity is not explicitly set (> 0).
RECALL_ABSOLUTE_SIMILARITY_FLOOR = 0.05
HASH_SEMANTIC_ONLY_FLOOR = 0.18

# Finding S1: Unified set of valid memory types including first-class `secret_ref`
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
    "secret_ref",
}

_NUMBER_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?")
_ALNUM_ID_WITH_DIGITS_RE = re.compile(r"\b[a-z0-9_.-]*\d+[a-z0-9_.-]*\b")
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
            try:
                st = status_path.stat()
                global _MODEL_STATUS_CACHE
                _MODEL_STATUS_CACHE = (status_path, int(st.st_mtime_ns), int(st.st_size), dict(payload))
            except OSError:
                pass
        except OSError:
            pass
        return dict(payload)


_MODEL_STATUS_CACHE: Optional[tuple[Path, int, int, dict[str, Any]]] = None


def get_model_status() -> dict[str, Any]:
    """Read `~/.mesh/model_status.json` or return in-memory bootstrap status."""
    global _MODEL_STATUS_CACHE
    status_path = _get_model_status_path()
    try:
        st = status_path.stat()
        cache = _MODEL_STATUS_CACHE
        if (
            cache is not None
            and cache[0] == status_path
            and cache[1] == st.st_mtime_ns
            and cache[2] == st.st_size
        ):
            return dict(cache[3])
        data = json.loads(status_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            with _STATUS_LOCK:
                _MODEL_STATUS_CACHE = (status_path, int(st.st_mtime_ns), int(st.st_size), data)
            return dict(data)
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


_SHA256_CACHE: dict[tuple[str, int, int], str] = {}
_SHA256_CACHE_LOCK = threading.Lock()


def _compute_file_sha256_cached(path: Path) -> str:
    try:
        st = path.stat()
    except OSError:
        return ""
    key = (str(path.resolve()), int(st.st_size), int(st.st_mtime_ns))
    with _SHA256_CACHE_LOCK:
        cached = _SHA256_CACHE.get(key)
    if cached is not None:
        return cached
    actual = _compute_file_sha256(path)
    with _SHA256_CACHE_LOCK:
        if len(_SHA256_CACHE) >= 16:
            _SHA256_CACHE.clear()
        _SHA256_CACHE[key] = actual
    return actual


def _verify_sha256(
    path: Path,
    allowed_hashes: set[str],
    env_override_key: str,
    source_label: str = "",
) -> bool:
    if not path.exists():
        return False
    if os.environ.get("MESH_SKIP_CHECKSUM", "").strip().lower() in ("1", "true", "yes"):
        return True
    custom = os.environ.get(env_override_key, "").strip().lower()
    expected = {custom} if custom else {h.lower() for h in allowed_hashes}
    if not expected:
        return True
    try:
        actual = _compute_file_sha256_cached(path)
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
    if env_disable_key and os.environ.get(env_disable_key, "").strip().lower() in ("1", "true", "yes"):
        return False
    from .daemon import request_user_permission

    return request_user_permission(action_description, assume_yes=assume_yes)


def _request_model_download_permission(assume_yes: bool = False) -> bool:
    global _download_permission_decided, _download_permission_granted
    if os.environ.get("MESH_NO_MODEL_DOWNLOAD", "").strip().lower() in ("1", "true", "yes"):
        if not _download_permission_decided:
            _download_permission_decided = True
            _download_permission_granted = False
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
        sess_options.intra_op_num_threads = min(2, max(1, cpu_count // 2))
        sess_options.inter_op_num_threads = 1
        sess_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        try:
            sess_options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        except Exception:
            pass
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


_LOCAL_PAIR_CACHE: Optional[tuple[Path, int, int, Path, int, int, Optional[tuple[Path, Path]]]] = None
_LOCAL_PAIR_LOCK = threading.Lock()


_PAIR_TTL_CACHE: Optional[tuple[Path, float, Optional[tuple[Path, Path]]]] = None


def _pair_recheck_seconds() -> float:
    raw = os.environ.get("MESH_MODEL_PAIR_RECHECK_SECONDS")
    if raw is None:
        return 1.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 1.0


def _has_local_onnx_pair() -> Optional[tuple[Path, Path]]:
    """Cheap front for _has_local_onnx_pair_impl: re-stat the model files at most once per TTL."""
    global _PAIR_TTL_CACHE
    ttl = _pair_recheck_seconds()
    if ttl <= 0.0:
        return _has_local_onnx_pair_impl()
    model_dir = _get_onnx_model_dir()
    ent = _PAIR_TTL_CACHE
    now = time.monotonic()
    if ent is not None and ent[0] == model_dir and (now - ent[1]) < ttl:
        return ent[2]
    res = _has_local_onnx_pair_impl()
    # Only a FOUND pair is memoised: a missing pair costs one failed stat and must be noticed the
    # moment the background download worker installs it.
    _PAIR_TTL_CACHE = (model_dir, now, res) if res is not None else None
    return res


def _has_local_onnx_pair_impl() -> Optional[tuple[Path, Path]]:
    global _LOCAL_PAIR_CACHE
    model_dir = _get_onnx_model_dir()
    target_model = model_dir / "model.onnx"
    target_tok = model_dir / "tokenizer.json"
    try:
        st_m = target_model.stat()
        st_t = target_tok.stat()
    except OSError:
        return None

    cache = _LOCAL_PAIR_CACHE
    if (
        cache is not None
        and cache[0] == target_model
        and cache[1] == st_m.st_mtime_ns
        and cache[2] == st_m.st_size
        and cache[3] == target_tok
        and cache[4] == st_t.st_mtime_ns
        and cache[5] == st_t.st_size
    ):
        return cache[6]

    res = None
    if st_m.st_size > 80_000_000 and st_t.st_size > 100_000:
        if _verify_sha256(
            target_model,
            _ALLOWED_MODEL_SHA256,
            "MESH_MODEL_SHA256",
            source_label=str(target_model),
        ) and _verify_sha256(
            target_tok,
            _ALLOWED_TOKENIZER_SHA256,
            "MESH_TOKENIZER_SHA256",
            source_label=str(target_tok),
        ):
            res = (target_model, target_tok)

    with _LOCAL_PAIR_LOCK:
        _LOCAL_PAIR_CACHE = (
            target_model,
            int(st_m.st_mtime_ns),
            int(st_m.st_size),
            target_tok,
            int(st_t.st_mtime_ns),
            int(st_t.st_size),
            res,
        )
    return res


_DISCOVER_NEG_UNTIL: float = 0.0


def _discover_negative_ttl() -> float:
    raw = os.environ.get("MESH_MODEL_DISCOVERY_TTL", "").strip()
    if not raw:
        return 120.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 120.0


def _discover_local_onnx_pair() -> Optional[tuple[Path, Path]]:
    global _DISCOVER_NEG_UNTIL
    fast_pair = _has_local_onnx_pair()
    if fast_pair is not None:
        return fast_pair
    # A failed search walks several cache trees with rglob (thousands of is_file() calls,
    # ~12 ms on Windows) and _get_model() used to repeat it on EVERY learn()/recall().
    # Remember the miss; a pair that appears later is still picked up by the
    # _has_local_onnx_pair() fast check above.
    if time.monotonic() < _DISCOVER_NEG_UNTIL:
        return None

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

    target_tok_ok = target_tok.exists() and _verify_sha256(
        target_tok, _ALLOWED_TOKENIZER_SHA256, "MESH_TOKENIZER_SHA256", source_label=str(target_tok)
    )
    if found_tok is not None and not target_tok_ok:
        try:
            ensure_secure_mesh_dir(model_dir)
            shutil.copy2(found_tok, target_tok)
        except OSError:
            pass
    target_model_ok = target_model.exists() and _verify_sha256(
        target_model, _ALLOWED_MODEL_SHA256, "MESH_MODEL_SHA256", source_label=str(target_model)
    )
    if found_model is not None and not target_model_ok:
        try:
            ensure_secure_mesh_dir(model_dir)
            shutil.copy2(found_model, target_model)
        except OSError:
            pass

    _pair = _has_local_onnx_pair()
    if _pair is None:
        _ttl = _discover_negative_ttl()
        if _ttl > 0.0:
            _DISCOVER_NEG_UNTIL = time.monotonic() + _ttl
    return _pair


def _download_file_from_mirrors(
    urls: list[str],
    dest: Path,
    min_bytes: int,
    allowed_sha256: Optional[set[str]] = None,
    sha256_env_key: str = "",
) -> bool:
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
    headers = {"User-Agent": f"mesh-context-layer/{VERSION}"}
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
    if not _GCS_TARBALL_MIRRORS:
        return False
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
                        source_lbl = f"{_GCS_TARBALL_MIRRORS[0]}:{member.name}" if _GCS_TARBALL_MIRRORS else member.name
                        if ok and _verify_sha256(
                            tmp_out,
                            _ALLOWED_MODEL_SHA256,
                            "MESH_MODEL_SHA256",
                            source_label=source_lbl,
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
                        source_lbl = f"{_GCS_TARBALL_MIRRORS[0]}:{member.name}" if _GCS_TARBALL_MIRRORS else member.name
                        if ok and _verify_sha256(
                            tmp_out,
                            _ALLOWED_TOKENIZER_SHA256,
                            "MESH_TOKENIZER_SHA256",
                            source_label=source_lbl,
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
    detached: bool = False,
) -> Optional[tuple[Path, Path]]:
    existing = _has_local_onnx_pair() or _discover_local_onnx_pair()
    if existing is not None:
        _download_complete_event.set()
        _record_model_status(state="ready", backend=_model_backend or "onnx", last_error=None)
        return existing
    if interactive_prompt and not _request_model_download_permission(assume_yes=assume_yes):
        return None
    if detached and not blocking:
        if _spawn_detached_model_download():
            return None
    _start_background_model_download()
    if not blocking:
        return None
    _download_complete_event.wait(timeout=timeout)
    return _has_local_onnx_pair()


def _spawn_detached_model_download() -> bool:
    try:
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([sys.executable, "-m", "mesh._model_download"], **kwargs)
        _record_model_status(state="downloading", backend=_model_backend or "onnx-warming")
        return True
    except Exception as exc:  # pragma: no cover
        _emit_warning(f"Could not start detached model download: {exc}")
        return False


def _background_download_worker() -> None:
    global _download_in_progress, _model, _model_backend, _download_failed_until
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
            need_tok = not (
                tok_dest.exists()
                and _verify_sha256(
                    tok_dest,
                    _ALLOWED_TOKENIZER_SHA256,
                    "MESH_TOKENIZER_SHA256",
                    source_label=str(tok_dest),
                )
            )
            if need_tok:
                _download_file_from_mirrors(
                    _TOKENIZER_MIRRORS,
                    tok_dest,
                    min_bytes=100_000,
                    allowed_sha256=_ALLOWED_TOKENIZER_SHA256,
                    sha256_env_key="MESH_TOKENIZER_SHA256",
                )
            need_model = not (
                model_dest.exists()
                and _verify_sha256(
                    model_dest,
                    _ALLOWED_MODEL_SHA256,
                    "MESH_MODEL_SHA256",
                    source_label=str(model_dest),
                )
            )
            if need_model:
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
            except Exception as exc:
                err_msg = f"ONNX session initialization failed: {exc}"
                _emit_warning(err_msg)
                _record_model_status(state="onnx_init_failed", backend="hash-fallback", last_error=err_msg)
            else:
                try:
                    _backfill_pending_embeddings()
                except Exception as exc:
                    err_msg = f"Embedding backfill failed after ONNX load: {exc}"
                    _emit_warning(err_msg)
                    _record_model_status(state="backfill_failed", backend="onnx", last_error=err_msg)
        else:
            _download_failed_until = time.monotonic() + _DOWNLOAD_RETRY_SECONDS
            with _MODEL_LOCK:
                if _model is None or _model is _hash_embed_384:
                    _model = _hash_embed_384
                    _model_backend = "hash-fallback"
            _record_model_status(
                state="download_failed",
                backend="hash-fallback",
                last_error="ONNX model is unavailable; using the hash-384 fallback",
            )
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


_FG_TS: float = 0.0  # monotonic time of the last FOREGROUND embed (learn / recall / update); the worker yields to it
_REEMBED_EVENT = threading.Event()
_REEMBED_IDLE = threading.Event()
_REEMBED_IDLE.set()
_REEMBED_WORKER_LOCK = threading.Lock()
_REEMBED_WORKER: Optional[threading.Thread] = None
_REEMBED_DRAINING = 0
_REEMBED_INFLIGHT = 0  # items popped from the queue but not finished yet (guarded by _PENDING_REEMBED_LOCK)
_REEMBED_STORES: "OrderedDict[str, MeshStore]" = OrderedDict()
_REEMBED_ATEXIT_REGISTERED = False


def _bg_onnx_enabled() -> bool:
    """Stage 2 only runs when a real ONNX pair is installed and the person has not opted out."""
    if os.environ.get("MESH_BG_ONNX", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    if os.environ.get("MESH_FORCE_HASH_EMBED", "").strip().lower() in ("1", "true", "yes"):
        return False
    if not _should_use_fast_stage1():
        return False
    return _has_local_onnx_pair() is not None


def _reembed_idle_gap() -> float:
    raw = os.environ.get("MESH_BG_ONNX_IDLE_MS")
    try:
        return max(0.0, float(raw) / 1000.0) if raw is not None else 0.15
    except ValueError:
        return 0.15


def _notify_reembed_worker() -> None:
    """Called whenever a hash-384 row is queued: wake (or lazily start) the stage-2 worker. Never blocks."""
    global _REEMBED_WORKER, _REEMBED_ATEXIT_REGISTERED
    if not _bg_onnx_enabled():
        return
    _REEMBED_IDLE.clear()
    _REEMBED_EVENT.set()
    worker = _REEMBED_WORKER
    if worker is not None and worker.is_alive():
        return
    with _REEMBED_WORKER_LOCK:
        if _REEMBED_WORKER is not None and _REEMBED_WORKER.is_alive():
            return
        _REEMBED_WORKER = threading.Thread(target=_reembed_worker_loop, name="mesh-onnx-stage2", daemon=True)
        _REEMBED_WORKER.start()
        if not _REEMBED_ATEXIT_REGISTERED:
            import atexit

            atexit.register(_reembed_atexit)
            _REEMBED_ATEXIT_REGISTERED = True


def _reembed_store(ns: str) -> "MeshStore":
    store = _REEMBED_STORES.get(ns)
    if store is None:
        store = MeshStore(namespace=ns, create=False)
        _REEMBED_STORES[ns] = store
        while len(_REEMBED_STORES) > 8:
            _REEMBED_STORES.popitem(last=False)
    else:
        _REEMBED_STORES.move_to_end(ns)
    return store


def _process_reembed_batch(items: list[tuple[str, str, str]]) -> None:
    """Swap the real ONNX vector in for rows that were stored with the fast hash vector.

    Safe against concurrent edits: a row is only rewritten if its content still equals the queued text
    (checked before AND after the forward pass); `preserve_accessed_at` keeps recall statistics intact.
    """
    by_ns: dict[str, list[tuple[str, str]]] = {}
    for ns, mid, text in items:
        by_ns.setdefault(ns, []).append((mid, text))
    for ns, group in by_ns.items():
        try:
            store = _reembed_store(ns)
        except Exception:
            _REEMBED_STORES.pop(ns, None)
            continue
        valid: list[tuple[str, str]] = []
        for mid, text in group:
            row = store.get(mid)
            if row is not None and row.get("content") == text and row.get("embedding_model") == "hash-384":
                valid.append((mid, text))
        if not valid:
            continue
        vecs, tag = _encode_texts_with_tag([t for _, t in valid])
        if tag != "onnx-minilm-l6-v2":
            continue
        for (mid, text), vec in zip(valid, vecs):
            latest = store.get(mid)
            if latest is not None and latest.get("content") == text and latest.get("embedding_model") == "hash-384":
                store.update(
                    memory_id=mid,
                    content=None,
                    embedding=vec.tolist(),
                    embedding_model="onnx-minilm-l6-v2",
                    preserve_accessed_at=True,
                )


def _reembed_worker_loop() -> None:
    global _REEMBED_INFLIGHT
    while True:
        _REEMBED_EVENT.wait(timeout=60.0)
        with _PENDING_REEMBED_LOCK:
            backlog = len(_PENDING_REEMBED_QUEUE)
        if backlog == 0:
            _REEMBED_EVENT.clear()
            with _PENDING_REEMBED_LOCK:
                if len(_PENDING_REEMBED_QUEUE) == 0 and _REEMBED_INFLIGHT == 0:
                    _REEMBED_IDLE.set()
                else:
                    _REEMBED_EVENT.set()
            if len(_PENDING_REEMBED_QUEUE) == 0 and not _REEMBED_IDLE.is_set():
                time.sleep(0.005)  # another worker is mid-batch: do not spin, do not declare idle
            continue
        if not _bg_onnx_enabled():
            _REEMBED_EVENT.clear()
            _REEMBED_IDLE.set()
            continue
        if not _REEMBED_DRAINING:
            gap = _reembed_idle_gap() - (time.monotonic() - _FG_TS)
            if gap > 0.0:
                time.sleep(min(gap, 0.25))  # a foreground call just ran: stay out of its way
                continue
        with _PENDING_REEMBED_LOCK:
            batch = _PENDING_REEMBED_QUEUE.pop_some(8)
            _REEMBED_INFLIGHT += len(batch)
        try:
            _process_reembed_batch(batch)
        except Exception:
            time.sleep(0.25)  # transient (locked db, model warming): rows stay hash-tagged and are healed later
        finally:
            with _PENDING_REEMBED_LOCK:
                _REEMBED_INFLIGHT -= len(batch)


def drain_reembed_worker(timeout: float = 10.0) -> bool:
    """Block until every queued row has been upgraded (or `timeout`). True when the queue is empty."""
    global _REEMBED_DRAINING
    if not _bg_onnx_enabled():
        return True
    with _PENDING_REEMBED_LOCK:
        if len(_PENDING_REEMBED_QUEUE) == 0 and _REEMBED_INFLIGHT == 0 and _REEMBED_IDLE.is_set():
            return True
    _REEMBED_DRAINING += 1
    try:
        _notify_reembed_worker()
        return _REEMBED_IDLE.wait(timeout=max(0.0, float(timeout)))
    finally:
        _REEMBED_DRAINING -= 1


def _drain_policy_allows() -> bool:
    """close()/exit drain: always when forced (MESH_BG_ONNX_DRAIN_ON_EXIT=1); by default only if the model is
    already resident, so a one-shot CLI call never pays a model load just to upgrade a vector on its way out."""
    mode = os.environ.get("MESH_BG_ONNX_DRAIN_ON_EXIT", "auto").strip().lower()
    if mode in ("0", "false", "no", "off"):
        return False
    if mode in ("1", "true", "yes", "on"):
        return True
    return _model is not None and _model_backend not in (None, "onnx-warming", "hash-fallback")


def _drain_reembed_for_close() -> None:
    try:
        if _drain_policy_allows() and len(_PENDING_REEMBED_QUEUE):
            drain_reembed_worker(timeout=env_float("MESH_BG_ONNX_DRAIN_TIMEOUT", 10.0, minimum=0.0))
    except Exception:
        pass


def _reembed_atexit() -> None:
    _drain_reembed_for_close()


def _trigger_background_bootstrap() -> None:
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
    global _IDLE_UNLOAD_SECONDS
    _IDLE_UNLOAD_SECONDS = max(0.0, float(seconds))
    if _IDLE_UNLOAD_SECONDS > 0:
        _ensure_idle_watchdog()


def unload_model() -> bool:
    global _model, _torch, _model_backend, _EMBED_CACHE_BYTES
    with _EMBED_CACHE_LOCK:
        _EMBED_CACHE.clear()
        _EMBED_CACHE_BYTES = 0
    with _NORM_QUERY_CACHE_LOCK:
        _NORM_QUERY_CACHE.clear()
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
        cache_bytes = _EMBED_CACHE_BYTES
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
    global _model, _torch, _model_backend, _last_model_use_ts
    _last_model_use_ts = time.monotonic()

    if _model is not None and _model_backend is not None:
        if _model_backend not in ("onnx-warming", "hash-fallback"):
            return _model, _model_backend
        if _has_local_onnx_pair() is None:
            if _download_in_progress or time.monotonic() < _download_failed_until:
                return _model, _model_backend

    with _MODEL_LOCK:
        _last_model_use_ts = time.monotonic()
        onnx_pair = _has_local_onnx_pair() or _discover_local_onnx_pair()
        if _model is not None and _model_backend is not None:
            if _model_backend not in ("onnx-warming", "hash-fallback"):
                return _model, _model_backend
            if onnx_pair is None and (
                _download_in_progress or time.monotonic() < _download_failed_until
            ):
                return _model, _model_backend

        force_hash = os.environ.get("MESH_FORCE_HASH_EMBED", "").strip().lower() in ("1", "true", "yes")
        if force_hash:
            _model = _hash_embed_384
            _model_backend = "hash-fallback"
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
                from fastembed import TextEmbedding  # pyright: ignore[reportMissingImports]

                _model = TextEmbedding(model_name="sentence-transformers/all-MiniLM-L6-v2")
                _model_backend = "fastembed"
                _record_model_status(state="ready", backend="fastembed", last_error=None)
                _ensure_idle_watchdog()
                return _model, _model_backend
            except Exception:
                pass

        if force_pytorch:
            try:
                import torch as torch_mod  # pyright: ignore[reportMissingImports]

                _torch = torch_mod
                from sentence_transformers import SentenceTransformer  # pyright: ignore[reportMissingImports]

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
        _model_backend = "onnx-warming" if _download_in_progress else "hash-fallback"
        _ensure_idle_watchdog()
        return _model, _model_backend


def _backend_to_model_tag(backend: Optional[str], model_obj: Any) -> str:
    if backend in ("onnx-warming", "hash-fallback", None) or model_obj is _hash_embed_384:
        return "hash-384"
    return "onnx-minilm-l6-v2"


def _current_embedding_model_tag() -> str:
    model, backend = _get_model()
    return _backend_to_model_tag(backend, model)


def _encode_uncached_texts_with_tag(texts: list[str]) -> tuple[np.ndarray, str]:
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
    global _EMBED_CACHE_BYTES
    maxsize = _get_embed_cache_maxsize()
    arr_ro = _make_readonly_f32(vec)
    if maxsize <= 0:
        _EMBED_CACHE.clear()
        _EMBED_CACHE_BYTES = 0
        return arr_ro
    old_entry = _EMBED_CACHE.pop(cleaned_text, None)
    if old_entry is not None:
        _EMBED_CACHE_BYTES -= int(old_entry[0].nbytes) + sys.getsizeof(cleaned_text) + 64
    _EMBED_CACHE[cleaned_text] = (arr_ro, model_tag)
    _EMBED_CACHE_BYTES += int(arr_ro.nbytes) + sys.getsizeof(cleaned_text) + 64
    _EMBED_CACHE.move_to_end(cleaned_text)
    while len(_EMBED_CACHE) > maxsize:
        k, (old_arr, _) = _EMBED_CACHE.popitem(last=False)
        _EMBED_CACHE_BYTES -= int(old_arr.nbytes) + sys.getsizeof(k) + 64
    if _EMBED_CACHE_BYTES < 0:
        _EMBED_CACHE_BYTES = 0
    return arr_ro


def _encode_texts_with_tag(texts: list[str]) -> tuple[np.ndarray, str]:
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
    clean_results = [r for r in results if r is not None]
    return np.vstack(clean_results).astype(np.float32, copy=False), final_tag


def _encode_texts(texts: list[str]) -> np.ndarray:
    arr, _ = _encode_texts_with_tag(texts)
    return arr


def warmup_model(
    namespace: Optional[str] = None,
    blocking: bool = False,
    assume_yes: bool = False,
) -> dict[str, Any]:
    ns = "shared"
    try:
        ns = validate_namespace((namespace or os.environ.get("MESH_NAMESPACE", "shared")).strip() or "shared")
        ensure_model_downloaded(blocking=blocking, timeout=10.0, assume_yes=assume_yes)
        _embed_cached_with_tag("warmup")
        drain_reembed_worker(timeout=env_float("MESH_BG_ONNX_DRAIN_TIMEOUT", 10.0, minimum=0.0))
        db_file = ensure_secure_mesh_dir().resolve() / f"mesh_{ns}.db"
        if db_file.exists():
            mesh = Mesh(namespace=ns, agent_id="warmup", create=False)
            mesh.store._get_vector_cache()
            mesh._heal_namespace_embeddings_if_needed()
            return {"status": "ok", "namespace": ns, "namespace_exists": True, "error": None}
        return {"status": "ok", "namespace": ns, "namespace_exists": False, "error": None}
    except Exception as exc:
        return {"status": "error", "namespace": ns, "namespace_exists": False, "error": str(exc)}


def _should_use_fast_stage1() -> bool:
    return os.environ.get("MESH_SYNC_ONNX", "").strip().lower() not in ("1", "true", "yes")


def _embed_cached_with_tag(cleaned_text: str) -> tuple[np.ndarray, str]:
    global _last_model_use_ts, _FG_TS
    _FG_TS = time.monotonic()
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

    # Keep model alive and responsive for idle-watchdog and engine status checks
    _get_model()
    _last_model_use_ts = time.monotonic()

    if _should_use_fast_stage1():
        vec_arr = _hash_embed_384(cleaned_text)
        with _EMBED_CACHE_LOCK:
            arr_ro = _store_in_embed_cache_locked(cleaned_text, vec_arr, "hash-384")
        return arr_ro, "hash-384"

    arr, model_tag = _encode_uncached_texts_with_tag([cleaned_text])
    with _EMBED_CACHE_LOCK:
        arr_ro = _store_in_embed_cache_locked(cleaned_text, arr[0], model_tag)
    return arr_ro, model_tag


def _embed_cached(cleaned_text: str) -> np.ndarray:
    vec_arr, _ = _embed_cached_with_tag(cleaned_text)
    return vec_arr


def _embed_with_tag(text: str) -> tuple[list[float], str]:
    cleaned = " ".join(text.strip().split())
    vec_arr, model_tag = _embed_cached_with_tag(cleaned)
    return vec_arr.tolist(), model_tag


def _embed(text: str) -> list[float]:
    vec, _ = _embed_with_tag(text)
    return vec


def _embed_query_with_tag(query_text: str) -> tuple[list[float], str]:
    cleaned = " ".join(query_text.strip().split())
    with _NORM_QUERY_CACHE_LOCK:
        cached = _NORM_QUERY_CACHE.get(cleaned)
        if cached is not None:
            _NORM_QUERY_CACHE.move_to_end(cleaned)
            return list(cached[0]), cached[1]

    base_arr, tag = _embed_cached_with_tag(cleaned)
    base_vec = np.asarray(base_arr, dtype=np.float32)

    if tag == "onnx-minilm-l6-v2":
        res_list = base_vec.tolist()
        with _NORM_QUERY_CACHE_LOCK:
            _NORM_QUERY_CACHE[cleaned] = (res_list, tag)
            if len(_NORM_QUERY_CACHE) > _NORM_QUERY_MAXSIZE:
                _NORM_QUERY_CACHE.popitem(last=False)
        return res_list, tag

    _, content_tokens, _ = _extract_search_tokens(cleaned)
    if not content_tokens or len(content_tokens) > 5:
        res_list = base_vec.tolist()
        with _NORM_QUERY_CACHE_LOCK:
            _NORM_QUERY_CACHE[cleaned] = (res_list, tag)
            if len(_NORM_QUERY_CACHE) > _NORM_QUERY_MAXSIZE:
                _NORM_QUERY_CACHE.popitem(last=False)
        return res_list, tag

    syn_map = expand_query_tokens(content_tokens)
    added_syns: list[str] = []
    for tok in content_tokens:
        for s in sorted(syn_map.get(tok, set()))[:3]:
            if s not in added_syns:
                added_syns.append(s)

    if not added_syns:
        res_list = base_vec.tolist()
        with _NORM_QUERY_CACHE_LOCK:
            _NORM_QUERY_CACHE[cleaned] = (res_list, tag)
            if len(_NORM_QUERY_CACHE) > _NORM_QUERY_MAXSIZE:
                _NORM_QUERY_CACHE.popitem(last=False)
        return res_list, tag

    expanded_text = f"{cleaned} {' '.join(added_syns[:8])}"
    exp_arr, _ = _embed_cached_with_tag(expanded_text)
    exp_vec = np.asarray(exp_arr, dtype=np.float32)
    blended = _normalize_rows(0.85 * base_vec + 0.15 * exp_vec)
    res_list = blended.tolist()
    with _NORM_QUERY_CACHE_LOCK:
        _NORM_QUERY_CACHE[cleaned] = (res_list, tag)
        if len(_NORM_QUERY_CACHE) > _NORM_QUERY_MAXSIZE:
            _NORM_QUERY_CACHE.popitem(last=False)
    return res_list, tag


def _embed_query(query_text: str) -> list[float]:
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
        if not (p.is_file() and not p.is_symlink() and not p.name.endswith(("-wal", "-shm"))):
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
    if ttl_days is None or not decay_start_at:
        return 1.0, None
    try:
        ttl = float(ttl_days)
        if ttl <= 0.0:
            return 1.0, None
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
    cleaned = _normalize_text_for_tokens(norm_text)
    nums = _NUMBER_TOKEN_RE.findall(cleaned)
    alnum_ids = set(_ALNUM_ID_WITH_DIGITS_RE.findall(cleaned))
    return nums, alnum_ids


def _is_true_duplicate(new_text: str, existing_text: str, similarity: float) -> bool:
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

    if len(norm_new) > 350 or len(norm_old) > 350:
        max_len = max(len(norm_new), len(norm_old), 1)
        min_len = min(len(norm_new), len(norm_old))
        if min_len / max_len < 0.88:
            return False
        union_words = words_new | words_old
        if union_words and (len(words_new & words_old) / len(union_words)) < 0.88:
            return False

    return True


# Finding N3: Non-backtracking copula operator matcher (prevents O(N^2) ReDoS on 16 KB strings)
_COPULA_OP_RE = re.compile(
    r"(?:\b(?:is|are|was|equals)\b|(?<![<>=!])=(?!=)|:(?=\s|$))"
)
_SUBJECT_VALUE_MAX_CHARS = 512


def _extract_subject_value_pair(norm_text: str) -> Optional[tuple[str, str]]:
    """
    Extract `(subject_phrase, value_phrase)` for copula or key-value statements in O(N) linear time (Finding N3):
      - "the user's pen is blue" -> ("user pen", "blue")
      - "staging redis port is 6380" -> ("staging redi port", "6380")
      - "port = 5432" / "port=5432" / "port: 5432" -> ("port", "5432")
      - "url is http://a:80/x" -> ("url", "http a 80 x")
      - "a == b" / "a <= b" / "10:30" -> None
    """
    if not norm_text or len(norm_text) > _SUBJECT_VALUE_MAX_CHARS:
        return None
    cleaned = _normalize_text_for_tokens(norm_text)
    if not cleaned or len(cleaned) > _SUBJECT_VALUE_MAX_CHARS:
        return None
    m = _COPULA_OP_RE.search(cleaned)
    if not m:
        return None
    lhs = cleaned[: m.start()].strip()
    rhs = cleaned[m.end() :].strip()
    if not lhs or not rhs:
        return None
    subj_tokens = [
        _stem_word(w)
        for w in _WORD_TOKEN_RE.findall(lhs)
        if w not in _STOPWORDS and w not in _NEGATION_TOKENS
    ]
    val_tokens = [
        _stem_word(w)
        for w in _WORD_TOKEN_RE.findall(rhs)
        if w not in _STOPWORDS or w in _NEGATION_TOKENS
    ]
    if not subj_tokens or not val_tokens:
        return None
    return (" ".join(subj_tokens), " ".join(val_tokens))


def _extract_number_contexts(norm_text: str) -> dict[str, set[str]]:
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

    sv_new = _extract_subject_value_pair(norm_new)
    sv_old = _extract_subject_value_pair(norm_old)
    if sv_new is not None and sv_old is not None:
        if sv_new[0] == sv_old[0] and sv_new[1] != sv_old[1]:
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

    if neg_new != neg_old:
        return True

    if _has_numeric_slot_conflict(norm_new, norm_old):
        return True

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


_OWNERSHIP_ENFORCEMENT_OVERRIDE: contextvars.ContextVar[Optional[bool]] = contextvars.ContextVar(
    "mesh_ownership_enforcement_override", default=None
)


def _ownership_enforced() -> bool:
    override = _OWNERSHIP_ENFORCEMENT_OVERRIDE.get()
    if override is not None:
        return override
    return os.environ.get("MESH_ENFORCE_AGENT_OWNERSHIP", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _can_modify_agent_memory(
    agent_id: str,
    existing_agent: Optional[str],
    trusted_local: bool = False,
) -> bool:
    if not _ownership_enforced() or trusted_local:
        return True
    if not existing_agent or existing_agent == agent_id:
        return True
    return False


def _check_agent_ownership(
    agent_id: str,
    existing_agent: Optional[str],
    trusted_local: bool = False,
) -> None:
    if not _can_modify_agent_memory(agent_id, existing_agent, trusted_local):
        raise ValueError(
            f"Agent '{agent_id}' is not authorized to modify memory owned by '{existing_agent}'."
        )


def _check_agent_scope(
    agent_id: str,
    namespace: str,
    *,
    write: bool = False,
    trusted_local: bool = False,
) -> None:
    """
    Finding S6 / 2e: Enforce per-agent read-only and namespace confinement scopes
    configured via `MESH_READ_ONLY=1` or `MESH_AGENT_SCOPES` JSON policy.
    """
    if trusted_local:
        return
    if write and env_bool("MESH_READ_ONLY", False):
        raise ValueError(
            f"Agent '{agent_id}' cannot modify namespace '{namespace}': Mesh is in read-only mode (MESH_READ_ONLY=1)."
        )
    raw_scopes = (os.environ.get("MESH_AGENT_SCOPES") or "").strip()
    if not raw_scopes:
        return
    try:
        scopes_map = json.loads(raw_scopes)
    except Exception:
        raise ValueError("Malformed MESH_AGENT_SCOPES configuration; expected valid JSON object.")
    if not isinstance(scopes_map, dict):
        return
    rule = scopes_map.get(agent_id) or scopes_map.get("*")
    if not isinstance(rule, dict):
        return

    allowed_ns = rule.get("namespaces")
    if isinstance(allowed_ns, list) and namespace not in [str(x).strip() for x in allowed_ns]:
        raise ValueError(
            f"Agent '{agent_id}' is not authorized to access namespace '{namespace}' (restricted by MESH_AGENT_SCOPES)."
        )
    if write:
        mode = str(rule.get("mode") or "rw").strip().lower()
        if mode in ("ro", "read", "readonly", "read-only", "read_only"):
            raise ValueError(
                f"Agent '{agent_id}' has read-only scope in MESH_AGENT_SCOPES and is not authorized to modify memories."
            )


def _clamp_confidence(value: Any, *, field: str = "confidence") -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number, got {value!r}")
    if not math.isfinite(parsed):
        raise ValueError(f"{field} must be a finite number, got {value!r}")
    return max(0.0, min(1.0, parsed))


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
        trusted_local: bool = False,
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
        self.trusted_local = bool(trusted_local)
        db_file = ensure_secure_mesh_dir().resolve() / f"mesh_{self.namespace}.db"
        self._ns_existed_before = db_file.exists()
        self.store = MeshStore(self.namespace, create=create)
        _trigger_background_bootstrap()

    def _heal_namespace_embeddings_if_needed(self, max_batch: int = 0) -> None:
        if os.environ.get("MESH_AUTO_HEAL_ON_CALL", "").strip().lower() not in ("1", "true", "yes") and max_batch <= 0:
            return
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
        secret_ref: Optional[Any] = None,
        trust_tier: Optional[str] = None,
        quarantined: bool = False,
        vault_secrets: bool = False,
        allow_redacted_public: bool = False,
    ) -> dict[str, Any]:
        _check_agent_scope(self.agent_id, self.namespace, write=True, trusted_local=self.trusted_local)
        if not content or not content.strip():
            raise ValueError("content cannot be empty")

        from .guards import guard_ingest, guard_tags

        requested_local_only = bool(local_only)

        # Finding S1 / 2b: Convert-instead-of-destroy flow when vault_secrets=True or MESH_VAULT_SECRETS=1
        auto_vault = bool(vault_secrets or env_bool("MESH_VAULT_SECRETS", False))
        vaulted_locators: list[SecretLocator] = []
        if auto_vault and is_credential(content):
            content, vaulted_locators = convert_text_secrets_to_refs(content, namespace=self.namespace)
            if vaulted_locators and secret_ref is None:
                secret_ref = vaulted_locators[0].to_dict()
                memory_type = "secret_ref"

        guarded = guard_ingest(content, local_only=requested_local_only)
        if guarded.rejected:
            raise ValueError(guarded.reason or "Memory content rejected by ingestion guard.")
        content = guarded.content
        clean_tags, tags_private, tag_warnings = guard_tags(
            _parse_tags(tags),
            local_only=guarded.local_only,
        )

        # Finding S11: Support explicit `--force-public` on interactive human CLI after redaction
        if allow_redacted_public and self.trusted_local:
            local_only = requested_local_only
        else:
            local_only = bool(guarded.local_only or tags_private)

        clean_content = content.strip()
        if len(clean_content) > MAX_CONTENT_LENGTH:
            raise ValueError(
                f"content length ({len(clean_content)} chars) exceeds maximum allowed length ({MAX_CONTENT_LENGTH} chars)."
            )

        norm_ref = normalize_secret_ref(secret_ref, default_namespace=self.namespace)
        if norm_ref is not None and (not memory_type or memory_type == "fact"):
            memory_type = "secret_ref"

        memory_type = (memory_type or "fact").strip().lower()
        if memory_type not in VALID_MEMORY_TYPES:
            raise ValueError(
                f"memory_type must be one of {sorted(VALID_MEMORY_TYPES)}, got '{memory_type}'"
            )

        default_tier = "user" if self.trusted_local else "agent"
        resolved_trust_tier = _normalize_trust_tier(trust_tier, default=default_tier)
        if resolved_trust_tier == "user" and not self.trusted_local and trust_tier is not None:
            resolved_trust_tier = "agent"

        confidence = _clamp_confidence(confidence)
        ttl_val = float(ttl_days) if ttl_days is not None and float(ttl_days) > 0 else None
        now = _utc_now_iso()

        self._heal_namespace_embeddings_if_needed()
        embedding, emb_model_tag = _embed_with_tag(clean_content)
        _, active_backend = _get_model()
        is_degraded = emb_model_tag != "onnx-minilm-l6-v2"
        memory_id = str(uuid.uuid4())

        was_new_namespace = not self._ns_existed_before and self.store.count() == 0
        similar_existing = (
            find_similar_namespaces(self.namespace, max_distance=2)
            if was_new_namespace
            else []
        )

        conflict: Optional[dict[str, Any]] = None
        with namespace_write_lock(self.namespace):
            self.store._get_vector_cache()
            if self.store.count() > 0:
                raw = self.store.query(embedding, n=3, query_embedding_model=emb_model_tag)
                docs = raw.get("documents", [[]])[0]
                distances = raw.get("distances", [[]])[0]
                ids = raw.get("ids", [[]])[0]
                metadatas = raw.get("metadatas", [[]])[0]

                if deduplicate:
                    for i, doc in enumerate(docs):
                        meta = metadatas[i] if metadatas and i < len(metadatas) else {}
                        if not _is_same_known_space(meta.get("embedding_model"), emb_model_tag):
                            continue
                        similarity = round(1.0 - float(distances[i]), 4)
                        existing_id = ids[i] if ids and i < len(ids) else None
                        existing_is_priv = bool(meta.get("local_only"))
                        if existing_is_priv and not requested_local_only:
                            continue
                        if existing_id and not _can_modify_agent_memory(
                            self.agent_id, meta.get("source_agent"), self.trusted_local
                        ):
                            continue
                        if existing_id and _is_true_duplicate(clean_content, doc, similarity):
                            existing_tags = _parse_tags(meta.get("tags"))
                            merged_tags = list(dict.fromkeys([*existing_tags, *clean_tags]))
                            updated_conf = max(confidence, float(meta.get("confidence", 1.0)))
                            content_changed = clean_content != doc
                            is_priv = bool(local_only or meta.get("local_only"))
                            updated = self.store.update(
                                memory_id=existing_id,
                                content=clean_content if content_changed else None,
                                embedding=embedding if content_changed else None,
                                memory_type=None,
                                confidence=updated_conf,
                                tags=merged_tags,
                                ttl_days=ttl_val,
                                reset_ttl=ttl_val is not None,
                                local_only=is_priv,
                                source_agent=None,
                                embedding_model=emb_model_tag if content_changed else None,
                                secret_ref=norm_ref if norm_ref is not None else None,
                            )
                            stored_local_only = (
                                bool(updated.get("local_only")) if updated else is_priv
                            )
                            if content_changed and emb_model_tag == "hash-384":
                                with _PENDING_REEMBED_LOCK:
                                    _PENDING_REEMBED_QUEUE.put(self.namespace, existing_id, clean_content)
                            dedup_res: dict[str, Any] = {
                                "memory_id": existing_id,
                                "status": "stored",
                                "deduplicated": True,
                                "local_only": stored_local_only,
                                "conflict": None,
                                "embedding_backend": active_backend or "hash-fallback",
                                "embedding_degraded": is_degraded,
                                "trust_tier": meta.get("trust_tier", resolved_trust_tier),
                                "quarantined": bool(meta.get("quarantined", quarantined)),
                            }
                            if norm_ref is not None:
                                dedup_res["secret_ref"] = norm_ref
                            log_learn(
                                content=clean_content,
                                namespace=self.namespace,
                                agent_id=self.agent_id,
                                result=dedup_res,
                                local_only=stored_local_only,
                            )
                            return dedup_res

                for i, doc in enumerate(docs):
                    meta = metadatas[i] if metadatas and i < len(metadatas) else {}
                    if not _is_same_known_space(meta.get("embedding_model"), emb_model_tag):
                        continue
                    similarity = round(1.0 - float(distances[i]), 4)
                    existing_is_private = bool(meta.get("local_only"))
                    if existing_is_private and not local_only:
                        continue
                    if _is_true_contradiction(clean_content, doc, similarity):
                        existing_id = ids[i] if ids and i < len(ids) else None
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
            stored_local_only = bool(
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
                        "trust_tier": resolved_trust_tier,
                        "quarantined": bool(quarantined),
                        "secret_ref": norm_ref,
                    },
                    local_only=bool(local_only),
                    embedding_model=emb_model_tag,
                    allow_redacted_public=bool(allow_redacted_public and self.trusted_local),
                )
            )
            self._ns_existed_before = True

        if emb_model_tag == "hash-384":
            with _PENDING_REEMBED_LOCK:
                _PENDING_REEMBED_QUEUE.put(self.namespace, memory_id, clean_content)

        all_warnings = tuple([*guarded.warnings, *tag_warnings])
        result: dict[str, Any] = {
            "memory_id": memory_id,
            "status": "stored_with_conflict" if conflict else "stored",
            "deduplicated": False,
            "local_only": stored_local_only,
            "conflict": conflict,
            "embedding_backend": active_backend or "hash-fallback",
            "embedding_degraded": is_degraded,
            "trust_tier": resolved_trust_tier,
            "quarantined": bool(quarantined),
        }
        if norm_ref is not None:
            result["secret_ref"] = norm_ref
        if all_warnings:
            result["warnings"] = all_warnings
        if was_new_namespace:
            result["namespace_created"] = True
            result["similar_existing"] = similar_existing

        log_learn(
            content=clean_content,
            namespace=self.namespace,
            agent_id=self.agent_id,
            result=result,
            local_only=stored_local_only,
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
        include_quarantined: bool = False,
        record_telemetry: bool = True,
        _precomputed_embedding: Optional[list[float]] = None,
        _precomputed_model_tag: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        _check_agent_scope(self.agent_id, self.namespace, write=False, trusted_local=self.trusted_local)
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
            include_quarantined=include_quarantined,
        )

        memories: list[dict[str, Any]] = []
        best_similarity: Optional[float] = (
            max((float(r.get("similarity", 0.0)) for r in candidates), default=None)
            if candidates
            else None
        )
        max_lexical_in_candidates = max(
            (float(r.get("lexical_score", 0.0)) for r in candidates),
            default=0.0,
        )
        effective_min_similarity = (
            float(min_similarity)
            if min_similarity > 0.0
            else RECALL_ABSOLUTE_SIMILARITY_FLOOR
        )

        for row in candidates:
            similarity = float(row.get("similarity", 0.0))
            sem_sim = float(row.get("semantic_similarity", similarity))
            lex_score = float(row.get("lexical_score", 0.0))

            if similarity < effective_min_similarity:
                continue

            if min_similarity <= 0.0:
                row_emb_model = str(row.get("embedding_model") or "")
                if (
                    (query_model_tag == "hash-384" or row_emb_model == "hash-384")
                    and lex_score <= 0.05
                    and sem_sim < HASH_SEMANTIC_ONLY_FLOOR
                ):
                    continue
                if (
                    max_lexical_in_candidates >= 0.55
                    and lex_score <= 0.05
                    and sem_sim < 0.35
                    and similarity < max(
                        RECALL_ABSOLUTE_SIMILARITY_FLOOR,
                        (best_similarity or 0.0) * 0.42,
                    )
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

            mem_entry: dict[str, Any] = {
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
                "trust_tier": row.get("trust_tier", "user"),
                "quarantined": bool(row.get("quarantined", False)),
                "created_at": row.get("created_at", ""),
                "accessed_at": row.get("accessed_at", ""),
            }
            if row.get("secret_ref") is not None:
                mem_entry["secret_ref"] = row.get("secret_ref")
            memories.append(mem_entry)

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

        # Finding S12: Redact credential queries and mark private_query=True even on 0 results
        if record_telemetry and self.store.db_path.exists():
            returned_ids = [m["id"] for m in memories if m.get("id")]
            query_has_cred = is_credential(clean_query)
            is_priv_query = bool(
                include_private is True
                or query_has_cred
                or any(m.get("local_only") for m in memories)
            )
            safe_logged_query = redact_secrets_local(clean_query) if query_has_cred else clean_query
            try:
                self.store.record_recall_telemetry(
                    memory_ids=returned_ids,
                    query_text=safe_logged_query,
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
        include_quarantined: bool = False,
    ) -> dict[str, Any]:
        """
        Execute `recall()` and also surface `other_possible_memories`, computing
        `_embed_query_with_tag()` only once across both stages (Phase 2.1).
        Finding N8: Excludes `local_only=True` memories from `other_possible_memories`
        unless `include_private=True` was explicitly passed.
        """
        clean_query = (query or "").strip()
        if not clean_query:
            raise ValueError("query cannot be empty")

        actual_include_private = _resolve_sdk_include_private(include_private)
        # Finding N8: Peripheral "other possible" snippets only include private rows if explicitly requested
        other_include_private = bool(include_private is True)
        query_embedding, query_model_tag = _embed_query_with_tag(clean_query)
        _, active_backend = _get_model()

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
            include_quarantined=include_quarantined,
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
                include_private=other_include_private,
                query_embedding_model=query_model_tag,
                include_quarantined=include_quarantined,
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
            "total_in_namespace": self.store.count_matching(
                include_private=actual_include_private,
                include_quarantined=include_quarantined,
            ),
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
        trust_tier: Optional[str] = None,
        quarantined: Optional[bool] = None,
        secret_ref: Optional[Any] = None,
    ) -> dict[str, Any]:
        _check_agent_scope(self.agent_id, self.namespace, write=True, trusted_local=self.trusted_local)
        if not memory_id or not memory_id.strip():
            raise ValueError("memory_id cannot be empty")

        resolved_id = self.store.resolve_id_prefix(memory_id)
        if not resolved_id:
            raise ValueError(f"Memory ID '{memory_id}' not found in namespace '{self.namespace}'")

        existing = self.store.get(resolved_id)
        if not existing:
            raise ValueError(f"Memory '{resolved_id}' not found in namespace '{self.namespace}'")
        _check_agent_ownership(self.agent_id, existing.get("source_agent"), self.trusted_local)
        old_content = str(existing.get("content", ""))

        from .guards import guard_ingest, guard_tags

        new_embedding: Optional[list[float]] = None
        emb_model_tag: Optional[str] = None
        clean_content: Optional[str] = None
        if content is not None:
            if not content.strip():
                raise ValueError("content cannot be empty")
            guarded = guard_ingest(content, local_only=bool(local_only))
            if guarded.rejected:
                raise ValueError(guarded.reason or "Memory content rejected by ingestion guard.")
            content = guarded.content
            if guarded.local_only:
                local_only = True
            clean_content = content.strip()
            if len(clean_content) > MAX_CONTENT_LENGTH:
                raise ValueError(
                    f"content length ({len(clean_content)} chars) exceeds maximum allowed length ({MAX_CONTENT_LENGTH} chars)."
                )
            if clean_content != old_content:
                new_embedding, emb_model_tag = _embed_with_tag(clean_content)
        elif local_only is False:
            # Finding S11: Re-guard existing content when attempting to flip a private memory to public
            guarded_existing = guard_ingest(old_content, local_only=False)
            if guarded_existing.rejected:
                raise ValueError(guarded_existing.reason or "Existing memory content rejected by ingestion guard.")
            if guarded_existing.local_only:
                clean_content = guarded_existing.content
                local_only = True

        clean_tags: Optional[list[str]] = None
        if tags is not None:
            clean_tags, tags_private, _ = guard_tags(
                _parse_tags(tags),
                local_only=bool(local_only),
            )
            if tags_private:
                local_only = True

        clean_type: Optional[str] = None
        if memory_type is not None:
            clean_type = memory_type.strip().lower()
            if clean_type not in VALID_MEMORY_TYPES:
                raise ValueError(
                    f"memory_type must be one of {sorted(VALID_MEMORY_TYPES)}, got '{memory_type}'"
                )

        clean_conf: Optional[float] = None
        if confidence is not None:
            clean_conf = _clamp_confidence(confidence)

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
            source_agent=source_agent,
            embedding_model=emb_model_tag,
            trust_tier=trust_tier,
            quarantined=quarantined,
            secret_ref=secret_ref,
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

    def approve(self, memory_id: str) -> dict[str, Any]:
        """
        Approve a quarantined memory (`quarantined=False`), making it eligible for
        agent recalls and context blocks (Finding S8).
        """
        _check_agent_scope(self.agent_id, self.namespace, write=True, trusted_local=self.trusted_local)
        if not memory_id or not memory_id.strip():
            raise ValueError("memory_id cannot be empty")
        resolved_id = self.store.resolve_id_prefix(memory_id)
        if not resolved_id:
            raise ValueError(f"Memory ID '{memory_id}' not found in namespace '{self.namespace}'")
        updated = self.store.approve(resolved_id)
        if not updated:
            raise ValueError(f"Memory '{resolved_id}' not found in namespace '{self.namespace}'")
        return {
            "memory_id": resolved_id,
            "status": "approved",
            "memory": self._enrich_memory_row(updated),
        }

    def get(self, memory_id: str) -> Optional[dict[str, Any]]:
        _check_agent_scope(self.agent_id, self.namespace, write=False, trusted_local=self.trusted_local)
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
        enriched["trust_tier"] = row.get("trust_tier", "user")
        enriched["quarantined"] = bool(row.get("quarantined", False))
        enriched["tags_list"] = (
            row["tags_list"] if "tags_list" in row else _parse_tags(row.get("tags"))
        )
        return enriched

    def forget(self, memory_id: str) -> bool:
        _check_agent_scope(self.agent_id, self.namespace, write=True, trusted_local=self.trusted_local)
        resolved_id = self.store.resolve_id_prefix(memory_id) or memory_id
        existing = self.store.get(resolved_id)
        if existing:
            _check_agent_ownership(self.agent_id, existing.get("source_agent"), self.trusted_local)
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
        _check_agent_scope(self.agent_id, self.namespace, write=False, trusted_local=self.trusted_local)
        return self.store.get_patterns(include_private=include_private)

    def contradictions(self, resolved: bool = False) -> list[dict[str, Any]]:
        _check_agent_scope(self.agent_id, self.namespace, write=False, trusted_local=self.trusted_local)
        return self.store.get_contradictions(resolved=resolved)

    def resolve_contradiction(self, contradiction_id: str) -> bool:
        _check_agent_scope(self.agent_id, self.namespace, write=True, trusted_local=self.trusted_local)
        clean_pref = (contradiction_id or "").strip().lower()
        if not clean_pref:
            raise ValueError("contradiction_id cannot be empty")
        if _ownership_enforced() and not self.trusted_local and self.store.db_path.exists():
            all_records = [
                *self.store.get_contradictions(resolved=False),
                *self.store.get_contradictions(resolved=True),
            ]
            matched: Optional[dict[str, Any]] = None
            for rec in all_records:
                cid = str(rec.get("id") or "").lower()
                if cid == clean_pref:
                    matched = rec
                    break
            if matched is None and len(clean_pref) >= 4:
                pref_matches = [
                    rec
                    for rec in all_records
                    if str(rec.get("id") or "").lower().startswith(clean_pref)
                ]
                if len(pref_matches) == 1:
                    matched = pref_matches[0]
            if matched is not None:
                existing_mid = str(matched.get("existing_memory_id") or "").strip()
                new_mid = str(matched.get("new_memory_id") or "").strip()
                existing_row = self.store.get(existing_mid) if existing_mid else None
                new_row = self.store.get(new_mid) if new_mid else None
                if existing_row is not None:
                    _check_agent_ownership(
                        self.agent_id,
                        existing_row.get("source_agent"),
                        self.trusted_local,
                    )
                elif new_row is not None:
                    _check_agent_ownership(
                        self.agent_id,
                        new_row.get("source_agent"),
                        self.trusted_local,
                    )
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
        order_by: str = "accessed",
        include_quarantined: bool = True,
        quarantined_only: bool = False,
    ) -> list[dict[str, Any]]:
        _check_agent_scope(self.agent_id, self.namespace, write=False, trusted_local=self.trusted_local)
        actual_include_private = _resolve_sdk_include_private(include_private)
        rows = self.store.list_all(
            limit=limit,
            memory_type=memory_type,
            tag=tag,
            tags=tags,
            include_private=actual_include_private,
            include_stale=include_stale,
            min_confidence=min_confidence,
            order_by=order_by,
            include_quarantined=include_quarantined,
            quarantined_only=quarantined_only,
        )
        now = _utc_now()
        return [self._enrich_memory_row(r, now=now) for r in rows]

    def count_matching(
        self,
        memory_type: Optional[str] = None,
        tag: Optional[str] = None,
        tags: Optional[list[str]] = None,
        include_private: Optional[bool] = None,
        source_agent: Optional[str] = None,
        include_quarantined: bool = True,
    ) -> int:
        _check_agent_scope(self.agent_id, self.namespace, write=False, trusted_local=self.trusted_local)
        actual_include_private = _resolve_sdk_include_private(include_private)
        return self.store.count_matching(
            memory_type=memory_type,
            tag=tag,
            tags=tags,
            include_private=actual_include_private,
            source_agent=source_agent,
            include_quarantined=include_quarantined,
        )

    def clear(self) -> None:
        _check_agent_scope(self.agent_id, self.namespace, write=True, trusted_local=self.trusted_local)
        self.store.clear_namespace()

    def count(self) -> int:
        return self.store.count()

    def create_work_item(
        self,
        title: str,
        kind: str = "task",
        parent_id: Optional[str] = None,
        description: str = "",
        priority: str = "medium",
        tags: Optional[list[str]] = None,
        local_only: bool = False,
    ) -> dict[str, Any]:
        _check_agent_scope(self.agent_id, self.namespace, write=True, trusted_local=self.trusted_local)
        return self.store.create_work_item(
            title=title,
            kind=kind,
            parent_id=parent_id,
            description=description,
            priority=priority,
            tags=tags,
            created_by=self.agent_id,
            local_only=local_only,
        )

    def update_work_item(
        self,
        item_id: str,
        status: Optional[str] = None,
        priority: Optional[str] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        note: Optional[str] = None,
    ) -> dict[str, Any]:
        _check_agent_scope(self.agent_id, self.namespace, write=True, trusted_local=self.trusted_local)
        return self.store.update_work_item(
            item_id=item_id,
            status=status,
            priority=priority,
            title=title,
            description=description,
            note=note,
            agent_id=self.agent_id,
        )

    def complete_work_item(
        self,
        item_id: str,
        summary: str,
        evidence: Optional[list[str]] = None,
        note: Optional[str] = None,
    ) -> dict[str, Any]:
        _check_agent_scope(self.agent_id, self.namespace, write=True, trusted_local=self.trusted_local)
        return self.store.complete_work_item(
            item_id=item_id,
            summary=summary,
            evidence=evidence,
            note=note,
            agent_id=self.agent_id,
        )

    def get_work_item(self, item_id: str, include_private: Optional[bool] = None) -> Optional[dict[str, Any]]:
        _check_agent_scope(self.agent_id, self.namespace, write=False, trusted_local=self.trusted_local)
        inc_priv = _resolve_sdk_include_private(include_private)
        return self.store.get_work_item(item_id, include_private=inc_priv)

    def list_work_items(
        self,
        status: Optional[str] = None,
        kind: Optional[str] = None,
        parent_id: Optional[str] = None,
        include_private: Optional[bool] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        _check_agent_scope(self.agent_id, self.namespace, write=False, trusted_local=self.trusted_local)
        inc_priv = _resolve_sdk_include_private(include_private)
        return self.store.list_work_items(
            status=status,
            kind=kind,
            parent_id=parent_id,
            include_private=inc_priv,
            limit=limit,
        )

    def get_work_events(self, item_id: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
        _check_agent_scope(self.agent_id, self.namespace, write=False, trusted_local=self.trusted_local)
        return self.store.get_work_events(item_id=item_id, limit=limit)

    def project_brief(
        self,
        max_tokens: int = 500,
        include_work_items: bool = True,
        include_private: Optional[bool] = None,
    ) -> dict[str, Any]:
        _check_agent_scope(self.agent_id, self.namespace, write=False, trusted_local=self.trusted_local)
        inc_priv = _resolve_sdk_include_private(include_private)
        return self.store.get_project_brief(
            max_tokens=max_tokens,
            include_work_items=include_work_items,
            include_private=inc_priv,
        )

    def changelog(
        self,
        since: str = "7d",
        format: str = "markdown",
        include_private: Optional[bool] = None,
    ) -> dict[str, Any]:
        _check_agent_scope(self.agent_id, self.namespace, write=False, trusted_local=self.trusted_local)
        inc_priv = _resolve_sdk_include_private(include_private)
        return self.store.get_changelog(
            since=since,
            format=format,
            include_private=inc_priv,
        )

    # Method aliases mirroring MCP tool names (Issue A-006 / B-004)
    task_create = create_work_item
    task_update = update_work_item
    task_complete = complete_work_item
    task_list = list_work_items
    task_get = get_work_item
    log = changelog

    def close(self) -> None:
        if hasattr(self, "store") and self.store is not None:
            self.store.flush_telemetry()
        _drain_reembed_for_close()

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
    text = " ".join(str(msg).split())
    with _WARNED_LOCK:
        if text in _WARNED_MESSAGES:
            return
        if len(_WARNED_MESSAGES) >= 256:
            _WARNED_MESSAGES.clear()
        _WARNED_MESSAGES.add(text)
    try:
        from . import ui

        ui.warn(text)
    except Exception:
        sys.stderr.write(f"[Mesh WARNING] {text}\n")

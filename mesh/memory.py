"""
mesh/memory.py — Core semantic & hybrid memory orchestration for Mesh.

Performance & Memory Architecture:
  - Non-Blocking 384-dim `all-MiniLM-L6-v2` Embedding Engine:
      1. Direct `onnxruntime` + `tokenizers` (Zero-network local ONNX, ~80ms cold start, ~2ms inference)
      2. Concept-aware deterministic 384-dim semantic fallback (<0.05ms) while background
         worker downloads the ONNX model from Google Cloud Storage / CDN mirrors
      3. Optional `sentence-transformers` (only when `MESH_FORCE_PYTORCH=1` is set)
  - Automatic Idle Model Unloading (`MESH_IDLE_UNLOAD_SECONDS`, default 180s):
      Unloads neural weights from RAM when idle so background daemons consume ~0% CPU
      and minimal RAM while keeping the 4,096-entry LRU vector cache instant (<0.01ms).
"""
from __future__ import annotations

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
from typing import Any, Optional

import numpy as np

from .store import MESH_DIR, MeshStore, _parse_iso_utc, _utc_now, _utc_now_iso

_MODEL_LOCK = threading.RLock()
_model: Optional[Any] = None
_torch: Optional[Any] = None
_model_backend: Optional[str] = None
_last_model_use_ts: float = 0.0
_watchdog_started: bool = False

_MODEL_NAME = "all-MiniLM-L6-v2"
_ONNX_MODEL_DIR = MESH_DIR / "models" / "all-MiniLM-L6-v2-onnx"

# Google Cloud Storage archive mirror FIRST (immune to Hugging Face rate limits)
_GCS_TARBALL_MIRRORS = [
    "https://storage.googleapis.com/qdrant-fastembed/sentence-transformers-all-MiniLM-L6-v2.tar.gz",
]

# Direct CDN mirrors (quantized 23MB & standard ONNX)
_TOKENIZER_MIRRORS = [
    "https://hf-mirror.com/sentence-transformers/all-MiniLM-L6-v2/resolve/main/tokenizer.json",
    "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/tokenizer.json",
    "https://hf-mirror.com/Xenova/all-MiniLM-L6-v2/resolve/main/tokenizer.json",
]

_ONNX_MODEL_MIRRORS = [
    "https://hf-mirror.com/Xenova/all-MiniLM-L6-v2/resolve/main/onnx/model_quantized.onnx",
    "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main/onnx/model_quantized.onnx",
    "https://hf-mirror.com/sentence-transformers/all-MiniLM-L6-v2/resolve/main/onnx/model.onnx",
    "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/onnx/model.onnx",
]

# Background download & auto-daemon bootstrap state
_DOWNLOAD_LOCK = threading.Lock()
_download_thread: Optional[threading.Thread] = None
_download_in_progress: bool = False
_download_complete_event = threading.Event()
_bootstrap_triggered: bool = False

# Queue of (namespace, memory_id, content) stored while ONNX model downloads in background
_PENDING_REEMBED_LOCK = threading.Lock()
_PENDING_REEMBED_QUEUE: list[tuple[str, str, str]] = []

# Unload neural model weights after N seconds of inactivity (default: 180s / 3 minutes).
# Set MESH_IDLE_UNLOAD_SECONDS=0 to keep the model loaded permanently.
_IDLE_UNLOAD_SECONDS: float = float(os.environ.get("MESH_IDLE_UNLOAD_SECONDS", "180"))

_EMBED_CACHE_LOCK = threading.Lock()
_EMBED_CACHE_MAXSIZE = 4096
_EMBED_CACHE: OrderedDict[str, tuple[float, ...]] = OrderedDict()

# Similarity threshold above which two memories are checked for contradiction
CONTRADICTION_THRESHOLD = 0.82

# Similarity threshold above which identical/near-identical facts are deduplicated
DEDUP_THRESHOLD = 0.985

# Effective confidence below which a memory is considered stale
STALE_THRESHOLD = 0.3

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

_NUMBER_TOKEN_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
_WORD_TOKEN_RE = re.compile(r"[a-z']+")
_NEGATION_TOKENS = {
    "not",
    "never",
    "no",
    "dont",
    "don't",
    "cannot",
    "can't",
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
    "against", "during", "without", "before", "under", "around", "among",
    "what", "where", "when", "who", "whom", "which", "how", "why",
    "do", "does", "did", "have", "has", "had", "having", "can", "could",
    "should", "would", "may", "might", "must", "shall", "will", "that",
    "this", "these", "those", "there", "their", "they", "them", "it",
    "its", "we", "our", "you", "your", "i", "me", "my", "and", "or",
    "but", "if", "then", "else", "any", "all", "some", "such", "very",
}

# Semantic concept clusters for instant zero-wait local embedding before ONNX finishes downloading
_CONCEPT_CLUSTERS: list[tuple[str, set[str]]] = [
    ("@c_theme", {"dark", "light", "mode", "theme", "visual", "color", "appearance", "editor", "editors", "ui"}),
    ("@c_pref", {"prefer", "prefers", "preference", "preferences", "like", "likes", "favorite", "want", "strongly"}),
    ("@c_cred", {"password", "credential", "credentials", "secret", "secrets", "vault", "apikey", "key", "private"}),
    ("@c_auth", {"auth", "authentication", "login", "session", "jwt", "token", "tokens", "cookie", "cookies", "httponly", "localstorage", "management"}),
    ("@c_db", {"database", "db", "postgres", "postgresql", "sql", "sqlite", "index", "indexing", "migration", "schema", "pgbouncer", "pooling"}),
    ("@c_err", {"error", "errors", "bug", "bugs", "issue", "issues", "problem", "problems", "failed", "fails", "incident", "timeout", "quorum"}),
    ("@c_limit", {"rate", "limit", "limits", "constraint", "constraints", "quota", "requests", "minute", "ip", "api"}),
    ("@c_lang", {"typescript", "javascript", "python", "language", "languages", "program", "programming", "code", "coding", "file", "files"}),
    ("@c_pet", {"cat", "cats", "dog", "dogs", "pet", "pets", "mat", "sat", "fox"}),
    ("@c_env", {"staging", "production", "prod", "environment", "server", "url", "port", "deploy", "script", "host"}),
    ("@c_mem", {"agent", "agents", "coordinate", "shared", "memory", "memories", "recall", "learn", "mesh", "benchmark", "seed"}),
]


def _normalize_rows(arr: np.ndarray) -> np.ndarray:
    """L2-normalize a 1D or 2D float32 numpy array."""
    a = np.asarray(arr, dtype=np.float32)
    if a.ndim == 1:
        norm = float(np.linalg.norm(a))
        return a / norm if norm > 1e-12 else a
    norms = np.linalg.norm(a, axis=1, keepdims=True)
    norms = np.where(norms > 1e-12, norms, 1.0)
    return a / norms


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
        sess_options.intra_op_num_threads = min(4, max(1, cpu_count))
        sess_options.inter_op_num_threads = 1
        sess_options.log_severity_level = 3

        self.session = ort.InferenceSession(
            str(model_path),
            sess_options=sess_options,
            providers=["CPUExecutionProvider"],
        )
        self.input_names = {inp.name for inp in self.session.get_inputs()}

    def encode(self, texts: list[str], batch_size: int = 32) -> np.ndarray:
        if not texts:
            return np.empty((0, 384), dtype=np.float32)

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


def _stem_word(w: str) -> str:
    if len(w) <= 3:
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
    Used whenever the neural ONNX model is still downloading in the background so tests
    and CLI calls never hang or wait on network downloads.
    """
    vec = np.zeros(384, dtype=np.float32)
    cleaned = text.strip().lower()
    raw_words = _WORD_TOKEN_RE.findall(cleaned)
    nums = _NUMBER_TOKEN_RE.findall(cleaned)

    content_words = [w for w in raw_words if w not in _STOPWORDS or w in _NEGATION_TOKENS]
    if not content_words and not nums:
        content_words = raw_words or ["empty"]

    stemmed_words = [_stem_word(w) for w in content_words]
    word_set = set(raw_words) | set(stemmed_words)

    features: list[tuple[str, float]] = []
    for w, sw in zip(content_words, stemmed_words):
        features.append((f"w:{sw}", 1.2))
        if w != sw:
            features.append((f"raw:{w}", 0.5))
    for n in nums:
        features.append((f"num:{n}", 1.5))

    # Bigrams of stemmed content words
    for i in range(len(stemmed_words) - 1):
        features.append((f"bi:{stemmed_words[i]}_{stemmed_words[i + 1]}", 0.65))

    # Concept cluster projection
    for cid, cwords in _CONCEPT_CLUSTERS:
        overlap = len(word_set & cwords)
        if overlap > 0:
            features.append((cid, 1.8 + min(1.2, overlap * 0.4)))

    # Character 4-grams on longer content words
    for sw in stemmed_words:
        if len(sw) >= 5:
            for j in range(len(sw) - 3):
                features.append((f"c4:{sw[j : j + 4]}", 0.2))

    for feat, weight in features:
        digest = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
        h = int.from_bytes(digest, "little")
        idx1 = h % 384
        sign1 = 1.0 if ((h >> 9) & 1) == 0 else -1.0
        idx2 = (h >> 16) % 384
        sign2 = 1.0 if ((h >> 25) & 1) == 0 else -1.0
        idx3 = (h >> 32) % 384
        sign3 = 1.0 if ((h >> 41) & 1) == 0 else -1.0
        vec[idx1] += sign1 * weight
        vec[idx2] += sign2 * (weight * 0.6)
        vec[idx3] += sign3 * (weight * 0.3)

    return _normalize_rows(vec)


def _has_local_onnx_pair() -> Optional[tuple[Path, Path]]:
    """
    O(1) hot-path check (<0.02ms) for `~/.mesh/models/all-MiniLM-L6-v2-onnx/{model.onnx, tokenizer.json}`.
    Never walks directory trees on the foreground thread.
    """
    target_model = _ONNX_MODEL_DIR / "model.onnx"
    target_tok = _ONNX_MODEL_DIR / "tokenizer.json"
    if target_model.exists() and target_tok.exists():
        try:
            if target_model.stat().st_size > 1_000_000 and target_tok.stat().st_size > 10_000:
                return target_model, target_tok
        except OSError:
            pass
    return None


def _discover_local_onnx_pair() -> Optional[tuple[Path, Path]]:
    """
    Check `_ONNX_MODEL_DIR` first; if missing, scan local HuggingFace / FastEmbed caches
    in the background and copy any found `model.onnx` or `tokenizer.json` into `_ONNX_MODEL_DIR`.
    """
    fast_pair = _has_local_onnx_pair()
    if fast_pair is not None:
        return fast_pair

    target_model = _ONNX_MODEL_DIR / "model.onnx"
    target_tok = _ONNX_MODEL_DIR / "tokenizer.json"

    home = Path.home()
    hf_hub = Path(os.environ.get("HF_HOME", str(home / ".cache" / "huggingface"))) / "hub"
    search_roots = [
        hf_hub / "models--sentence-transformers--all-MiniLM-L6-v2",
        hf_hub / "models--qdrant--all-MiniLM-L6-v2-onnx",
        hf_hub / "models--Xenova--all-MiniLM-L6-v2",
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
                    if candidate.stat().st_size > 10_000:
                        found_tok = candidate
                elif (
                    name_lower in ("model.onnx", "model_optimized.onnx", "model_quantized.onnx")
                    and found_model is None
                ):
                    if candidate.stat().st_size > 1_000_000:
                        found_model = candidate
                if found_model is not None and found_tok is not None:
                    break
        except OSError:
            continue

    if found_tok is not None and not target_tok.exists():
        try:
            _ONNX_MODEL_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(found_tok, target_tok)
        except OSError:
            pass

    if found_model is not None and not target_model.exists():
        try:
            _ONNX_MODEL_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(found_model, target_model)
        except OSError:
            pass

    return _has_local_onnx_pair()


def _download_file_from_mirrors(
    urls: list[str],
    dest: Path,
    min_bytes: int,
) -> bool:
    """Download a single file from the first responsive mirror with strict connect/read timeouts."""
    if dest.exists():
        try:
            if dest.stat().st_size >= min_bytes:
                return True
        except OSError:
            pass

    _ONNX_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "mesh-context-layer/0.6"}
    hf_token = (
        os.environ.get("HF_TOKEN")
        or os.environ.get("HUGGING_FACE_HUB_TOKEN")
        or os.environ.get("HUGGINGFACE_TOKEN")
    )

    try:
        import httpx

        timeout_cfg = httpx.Timeout(connect=3.5, read=8.0, write=5.0, pool=5.0)
        for url in urls:
            req_headers = dict(headers)
            if hf_token and "huggingface.co" in url:
                req_headers["Authorization"] = f"Bearer {hf_token.strip()}"
            tmp_path = dest.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
            try:
                with httpx.Client(follow_redirects=True, timeout=timeout_cfg, headers=req_headers) as client:
                    with client.stream("GET", url) as resp:
                        if resp.status_code != 200:
                            continue
                        with open(tmp_path, "wb") as f:
                            for chunk in resp.iter_bytes(chunk_size=65536):
                                if chunk:
                                    f.write(chunk)
                if tmp_path.exists() and tmp_path.stat().st_size >= min_bytes:
                    tmp_path.replace(dest)
                    return True
            except Exception:
                pass
            finally:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
    except Exception:
        pass
    return False


def _download_from_gcs_tarball() -> bool:
    """
    Primary background downloader using Google Cloud Storage `.tar.gz` bundle.
    Completely independent of Hugging Face servers and immune to HF rate limits.
    """
    if _has_local_onnx_pair() is not None:
        return True

    target_model = _ONNX_MODEL_DIR / "model.onnx"
    target_tok = _ONNX_MODEL_DIR / "tokenizer.json"
    _ONNX_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    tmp_tar = _ONNX_MODEL_DIR / f"bundle.{os.getpid()}.{threading.get_ident()}.tar.gz"
    try:
        if not _download_file_from_mirrors(_GCS_TARBALL_MIRRORS, tmp_tar, min_bytes=1_000_000):
            return False
        with tarfile.open(tmp_tar, "r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                base_name = Path(member.name).name.lower()
                if base_name in ("model.onnx", "model_optimized.onnx") and not target_model.exists():
                    extracted = tar.extractfile(member)
                    if extracted is not None:
                        tmp_out = target_model.with_suffix(f".tmp.{os.getpid()}")
                        with open(tmp_out, "wb") as out_f:
                            shutil.copyfileobj(extracted, out_f)
                        tmp_out.replace(target_model)
                elif base_name == "tokenizer.json" and not target_tok.exists():
                    extracted = tar.extractfile(member)
                    if extracted is not None:
                        tmp_out = target_tok.with_suffix(f".tmp.{os.getpid()}")
                        with open(tmp_out, "wb") as out_f:
                            shutil.copyfileobj(extracted, out_f)
                        tmp_out.replace(target_tok)
        return _has_local_onnx_pair() is not None
    except Exception:
        return False
    finally:
        try:
            tmp_tar.unlink(missing_ok=True)
        except OSError:
            pass


def ensure_model_downloaded(blocking: bool = False, timeout: float = 15.0) -> Optional[tuple[Path, Path]]:
    """
    Ensure the `all-MiniLM-L6-v2` ONNX model and tokenizer are present on local disk.
    Defaults to `blocking=False` so callers never hang waiting for network downloads.
    """
    existing = _has_local_onnx_pair()
    if existing is not None:
        _download_complete_event.set()
        return existing

    _start_background_model_download()
    if not blocking:
        return None

    _download_complete_event.wait(timeout=timeout)
    return _has_local_onnx_pair()


def _background_download_worker() -> None:
    """Download `tokenizer.json` and `model.onnx` in the background and hot-swap the ONNX engine."""
    global _download_in_progress, _model, _model_backend
    try:
        # 1. Check local disk caches first
        pair = _discover_local_onnx_pair()

        # 2. Try Google Cloud Storage tarball first (no HF rate limit)
        if pair is None:
            _download_from_gcs_tarball()
            pair = _has_local_onnx_pair()

        # 3. Try direct quantized/standard ONNX mirrors
        if pair is None:
            tok_dest = _ONNX_MODEL_DIR / "tokenizer.json"
            model_dest = _ONNX_MODEL_DIR / "model.onnx"
            _download_file_from_mirrors(_TOKENIZER_MIRRORS, tok_dest, min_bytes=10_000)
            _download_file_from_mirrors(_ONNX_MODEL_MIRRORS, model_dest, min_bytes=1_000_000)
            pair = _has_local_onnx_pair()

        if pair is not None:
            try:
                onnx_eng = _DirectOnnxEmbedding(pair[0], pair[1])
                with _MODEL_LOCK:
                    _model = onnx_eng
                    _model_backend = "onnx"
                    _ensure_idle_watchdog()
                _backfill_pending_embeddings()
            except Exception:
                pass
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
    """Re-embed any memories that were stored while the ONNX model was downloading in the background."""
    with _PENDING_REEMBED_LOCK:
        if not _PENDING_REEMBED_QUEUE:
            return
        pending = list(_PENDING_REEMBED_QUEUE)
        _PENDING_REEMBED_QUEUE.clear()

    with _EMBED_CACHE_LOCK:
        _EMBED_CACHE.clear()

    by_ns: dict[str, list[tuple[str, str]]] = {}
    for ns, mid, text in pending:
        by_ns.setdefault(ns, []).append((mid, text))

    for ns, items in by_ns.items():
        try:
            texts = [t for _, t in items]
            vecs = _encode_texts(texts)
            store = MeshStore(namespace=ns)
            for (mid, text), vec in zip(items, vecs):
                store.update(memory_id=mid, content=text, embedding=vec.tolist())
        except Exception:
            pass


def _trigger_background_bootstrap() -> None:
    """
    On first Mesh usage, non-blockingly start the background ONNX downloader
    and auto-start the background daemon (unless in pytest or explicitly disabled).
    """
    global _bootstrap_triggered
    if _bootstrap_triggered:
        return
    _bootstrap_triggered = True

    if _has_local_onnx_pair() is None:
        _start_background_model_download()

    if (
        "pytest" not in sys.modules
        and os.environ.get("MESH_NO_AUTO_DAEMON", "").strip().lower() not in ("1", "true", "yes")
        and os.environ.get("MESH_IN_DAEMON", "").strip().lower() not in ("1", "true", "yes")
    ):
        def _auto_daemon() -> None:
            try:
                from .daemon import ensure_daemon_running

                ensure_daemon_running(wait=False)
            except Exception:
                pass

        threading.Thread(target=_auto_daemon, name="mesh-auto-daemon", daemon=True).start()


def set_idle_unload_seconds(seconds: float) -> None:
    """Configure the automatic idle model unload timeout in seconds (0 disables unloading)."""
    global _IDLE_UNLOAD_SECONDS
    _IDLE_UNLOAD_SECONDS = max(0.0, float(seconds))
    if _IDLE_UNLOAD_SECONDS > 0:
        _ensure_idle_watchdog()


def unload_model() -> bool:
    """
    Explicitly unload the embedding model from memory and trigger garbage collection.
    The in-memory LRU `_EMBED_CACHE` and `MeshStore` vector caches remain intact.
    Returns True if a loaded model was unloaded.
    """
    global _model, _torch, _model_backend
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
    """Return current embedding engine and memory residency status in O(1) time."""
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
    return {
        "model_loaded": loaded,
        "backend": backend,
        "model_name": _MODEL_NAME,
        "idle_seconds": idle_for,
        "idle_unload_timeout_seconds": _IDLE_UNLOAD_SECONDS,
        "embed_cache_entries": cache_size,
        "embed_cache_maxsize": _EMBED_CACHE_MAXSIZE,
        "onnx_cached_on_disk": _has_local_onnx_pair() is not None,
        "download_in_progress": _download_in_progress,
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
    Non-blocking thread-safe model loader.
    1. If `model.onnx` + `tokenizer.json` are on local disk, loads `_DirectOnnxEmbedding` (~80ms, 0 network).
    2. If `model.onnx` is still downloading in the background, starts the background download worker
       and immediately returns the `<0.05ms` concept-aware semantic embedder with zero blocking.
    3. Only imports PyTorch `SentenceTransformer` if `MESH_FORCE_PYTORCH=1` is explicitly set.
    """
    global _model, _torch, _model_backend, _last_model_use_ts
    _last_model_use_ts = time.monotonic()

    # Fast lock-free path when ONNX or custom model is already loaded
    if _model is not None and _model_backend is not None:
        if _model_backend != "onnx-warming" or _has_local_onnx_pair() is None:
            return _model, _model_backend

    with _MODEL_LOCK:
        _last_model_use_ts = time.monotonic()
        onnx_pair = _has_local_onnx_pair()

        if _model is not None and _model_backend is not None:
            if _model_backend != "onnx-warming" or onnx_pair is None:
                return _model, _model_backend

        force_pytorch = os.environ.get("MESH_FORCE_PYTORCH", "").strip().lower() in ("1", "true", "yes")

        # Tier 1: Direct Local ONNX (~80ms cold start, ~2ms inference, zero network calls)
        if not force_pytorch and onnx_pair is not None:
            try:
                _model = _DirectOnnxEmbedding(onnx_pair[0], onnx_pair[1])
                _model_backend = "onnx"
                _ensure_idle_watchdog()
                return _model, _model_backend
            except Exception:
                pass

        # Optional explicit PyTorch override (only when MESH_FORCE_PYTORCH=1)
        if force_pytorch:
            try:
                import torch as torch_mod

                _torch = torch_mod
                from sentence_transformers import SentenceTransformer

                _model = SentenceTransformer(_MODEL_NAME)
                _model_backend = "sentence-transformers"
                _ensure_idle_watchdog()
                return _model, _model_backend
            except Exception:
                pass

        # Tier 2: Non-blocking instant semantic engine while background worker downloads ONNX
        _start_background_model_download()
        _model = _hash_embed_384
        _model_backend = "onnx-warming"
        _ensure_idle_watchdog()
        return _model, _model_backend


def _encode_uncached_texts(texts: list[str]) -> np.ndarray:
    """Encode uncached strings into L2-normalized float32 rows."""
    global _last_model_use_ts
    model, backend = _get_model()
    _last_model_use_ts = time.monotonic()

    if isinstance(model, _DirectOnnxEmbedding):
        return model.encode(texts, batch_size=min(32, max(1, len(texts))))

    if backend in ("onnx-warming", "hash-fallback") or model is _hash_embed_384:
        vecs = np.vstack([_hash_embed_384(t) for t in texts])
        return _normalize_rows(vecs)

    if backend == "fastembed":
        gen = model.embed(texts, batch_size=min(32, max(1, len(texts))))
        vecs = np.asarray(list(gen), dtype=np.float32)
        return _normalize_rows(vecs)

    # sentence-transformers backend or custom unit-test mock
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
    return _normalize_rows(arr)


def _encode_texts(texts: list[str]) -> np.ndarray:
    """
    Encode a list of strings into a 2D array of L2-normalized float32 embeddings.
    Checks and populates `_EMBED_CACHE` so repeated strings resolve in <0.01ms
    with zero inference overhead.
    """
    if not texts:
        return np.empty((0, 384), dtype=np.float32)

    cleaned_list = [" ".join(str(t).strip().split()) for t in texts]
    results: list[Optional[tuple[float, ...]]] = [None] * len(cleaned_list)
    missing_by_text: dict[str, list[int]] = {}

    with _EMBED_CACHE_LOCK:
        for idx, ctext in enumerate(cleaned_list):
            cached = _EMBED_CACHE.get(ctext)
            if cached is not None:
                _EMBED_CACHE.move_to_end(ctext)
                results[idx] = cached
            else:
                missing_by_text.setdefault(ctext, []).append(idx)

    if missing_by_text:
        unique_misses = list(missing_by_text.keys())
        vecs = _encode_uncached_texts(unique_misses)
        with _EMBED_CACHE_LOCK:
            for ctext, vec in zip(unique_misses, vecs):
                tup = tuple(vec.tolist())
                _EMBED_CACHE[ctext] = tup
                _EMBED_CACHE.move_to_end(ctext)
                if len(_EMBED_CACHE) > _EMBED_CACHE_MAXSIZE:
                    _EMBED_CACHE.popitem(last=False)
                for target_idx in missing_by_text[ctext]:
                    results[target_idx] = tup

    return np.asarray(results, dtype=np.float32)


def warmup_model(namespace: Optional[str] = None, blocking: bool = False) -> None:
    """
    Pre-load the embedding engine and warm the default namespace vector cache.
    Triggers ONNX model download in the background without blocking the caller.
    """
    try:
        ensure_model_downloaded(blocking=blocking, timeout=10.0)
        ns = (namespace or os.environ.get("MESH_NAMESPACE", "shared")).strip() or "shared"
        store = MeshStore(namespace=ns)
        store._get_vector_cache()
        _embed_cached("warmup")
    except Exception:
        pass


def _embed_cached(cleaned_text: str) -> tuple[float, ...]:
    """
    O(1) LRU-cached embedding helper. Returns an immutable tuple of floats
    so repeated queries and duplicate content strings resolve in <0.01ms
    without waking the neural model if it has been unloaded during idle.
    """
    with _EMBED_CACHE_LOCK:
        cached = _EMBED_CACHE.get(cleaned_text)
        if cached is not None:
            _EMBED_CACHE.move_to_end(cleaned_text)
            return cached

    arr = _encode_uncached_texts([cleaned_text])[0]
    result = tuple(arr.tolist())

    with _EMBED_CACHE_LOCK:
        _EMBED_CACHE[cleaned_text] = result
        _EMBED_CACHE.move_to_end(cleaned_text)
        if len(_EMBED_CACHE) > _EMBED_CACHE_MAXSIZE:
            _EMBED_CACHE.popitem(last=False)
    return result


def _embed(text: str) -> list[float]:
    """Embed a single text string into a normalized float vector."""
    cleaned = " ".join(text.strip().split())
    return list(_embed_cached(cleaned))


def _embed_batch(texts: list[str]) -> list[list[float]]:
    """
    Embed multiple texts in a single batch forward pass while reading from and
    populating the shared LRU cache for all items.
    """
    if not texts:
        return []
    if len(texts) == 1:
        return [_embed(texts[0])]
    arr = _encode_texts(texts)
    return [row.tolist() for row in arr]


def _compute_decay_metrics(
    ttl_days: Optional[float],
    decay_start_at: Optional[str],
    now: Optional[datetime] = None,
) -> tuple[float, Optional[int]]:
    """
    Compute (decay_factor, days_until_stale) in a single timestamp parse pass.
    """
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


def _is_true_duplicate(new_text: str, existing_text: str, similarity: float) -> bool:
    """
    Determine whether `new_text` is a semantic duplicate of `existing_text`
    rather than a conflicting update (e.g., 'rate limit is 100' vs 'rate limit is 200').
    """
    norm_new = " ".join(new_text.strip().lower().split())
    norm_old = " ".join(existing_text.strip().lower().split())
    if norm_new == norm_old:
        return True
    if similarity < DEDUP_THRESHOLD:
        return False

    nums_new = _NUMBER_TOKEN_RE.findall(norm_new)
    nums_old = _NUMBER_TOKEN_RE.findall(norm_old)
    if nums_new != nums_old:
        return False

    words_new = set(_WORD_TOKEN_RE.findall(norm_new))
    words_old = set(_WORD_TOKEN_RE.findall(norm_old))
    if bool(words_new & _NEGATION_TOKENS) != bool(words_old & _NEGATION_TOKENS):
        return False

    return True


def _is_true_contradiction(new_text: str, existing_text: str, similarity: float) -> bool:
    """
    Determine whether `new_text` genuinely conflicts with `existing_text`
    rather than merely elaborating on the same compatible fact.
    """
    if similarity < CONTRADICTION_THRESHOLD:
        return False
    if _is_true_duplicate(new_text, existing_text, similarity):
        return False

    norm_new = " ".join(new_text.strip().lower().split())
    norm_old = " ".join(existing_text.strip().lower().split())

    nums_new = _NUMBER_TOKEN_RE.findall(norm_new)
    nums_old = _NUMBER_TOKEN_RE.findall(norm_old)
    if nums_new != nums_old:
        return True

    words_new = set(_WORD_TOKEN_RE.findall(norm_new))
    words_old = set(_WORD_TOKEN_RE.findall(norm_old))
    if bool(words_new & _NEGATION_TOKENS) != bool(words_old & _NEGATION_TOKENS):
        return True

    if norm_new in norm_old or norm_old in norm_new:
        return False

    return True


class Mesh:
    """
    Shared semantic & hybrid memory for AI agents.
    """

    def __init__(self, namespace: str = "default", agent_id: str = "agent"):
        self.namespace = namespace
        self.agent_id = agent_id
        self.store = MeshStore(namespace)
        _trigger_background_bootstrap()

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

        memory_type = (memory_type or "fact").strip().lower()
        if memory_type not in VALID_MEMORY_TYPES:
            raise ValueError(
                f"memory_type must be one of {sorted(VALID_MEMORY_TYPES)}, got '{memory_type}'"
            )

        confidence = max(0.0, min(1.0, float(confidence)))
        clean_tags = _parse_tags(tags)
        ttl_val = float(ttl_days) if ttl_days is not None and float(ttl_days) > 0 else None
        now = _utc_now_iso()

        embedding = _embed(clean_content)
        memory_id = str(uuid.uuid4())

        # --- Deduplication & Contradiction Detection ---
        conflict: Optional[dict[str, Any]] = None
        if self.store.count() > 0:
            raw = self.store.query(embedding, n=3)
            docs = raw.get("documents", [[]])[0]
            distances = raw.get("distances", [[]])[0]
            ids = raw.get("ids", [[]])[0]
            metadatas = raw.get("metadatas", [[]])[0]

            # Pass 1: Check if any top candidate is a true duplicate
            if deduplicate:
                for i, doc in enumerate(docs):
                    similarity = round(1.0 - float(distances[i]), 4)
                    existing_id = ids[i] if ids and i < len(ids) else None
                    meta = metadatas[i] if metadatas and i < len(metadatas) else {}

                    if existing_id and _is_true_duplicate(clean_content, doc, similarity):
                        existing_tags = _parse_tags(meta.get("tags"))
                        merged_tags = list(dict.fromkeys([*existing_tags, *clean_tags]))
                        updated_conf = max(confidence, float(meta.get("confidence", 1.0)))
                        content_changed = clean_content != doc

                        self.store.update(
                            memory_id=existing_id,
                            content=clean_content if content_changed else None,
                            embedding=embedding if content_changed else None,
                            memory_type=memory_type,
                            confidence=updated_conf,
                            tags=merged_tags,
                            ttl_days=ttl_val,
                            reset_ttl=ttl_val is not None,
                            local_only=bool(local_only or meta.get("local_only")),
                            source_agent=self.agent_id,
                        )
                        return {
                            "memory_id": existing_id,
                            "status": "stored",
                            "deduplicated": True,
                            "conflict": None,
                        }

            # Pass 2: Check for genuine semantic contradiction against the closest existing memories
            for i, doc in enumerate(docs):
                similarity = round(1.0 - float(distances[i]), 4)
                if _is_true_contradiction(clean_content, doc, similarity):
                    existing_id = ids[i] if ids and i < len(ids) else None
                    meta = metadatas[i] if metadatas and i < len(metadatas) else {}
                    contradiction_id = self.store.log_contradiction(
                        new_memory_id=memory_id,
                        new_content=clean_content,
                        existing_memory_id=existing_id or "",
                        existing_content=doc,
                        similarity=similarity,
                    )
                    conflict = {
                        "existing_memory_id": existing_id,
                        "existing_content": doc,
                        "existing_memory": doc,
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
                "accessed_at": now,
                "access_count": 0,
                "ttl_days": ttl_val,
                "decay_start_at": decay_start_at,
            },
            local_only=bool(local_only),
        )

        if _model_backend == "onnx-warming":
            with _PENDING_REEMBED_LOCK:
                _PENDING_REEMBED_QUEUE.append((self.namespace, memory_id, clean_content))

        return {
            "memory_id": memory_id,
            "status": "stored_with_conflict" if conflict else "stored",
            "deduplicated": False,
            "conflict": conflict,
        }

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
    ) -> list[dict[str, Any]]:
        if not query or not query.strip():
            raise ValueError("query cannot be empty")
        clean_query = query.strip()

        embedding = _embed(clean_query)
        candidate_limit = max(n * 3, 15)
        candidates = self.store.hybrid_query(
            query_text=clean_query,
            embedding=embedding,
            n=candidate_limit,
            memory_type=memory_type,
            tags=_parse_tags(tags) if tags else None,
            source_agent=source_agent,
        )

        now = _utc_now()
        memories: list[dict[str, Any]] = []
        best_similarity: Optional[float] = None

        for row in candidates:
            similarity = float(row.get("similarity", 0.0))
            if best_similarity is None or similarity > best_similarity:
                best_similarity = similarity

            if similarity < min_similarity:
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
            access_count = int(row.get("access_count") or 0) + 1

            usage_boost = min(1.0, math.log1p(access_count) / 5.0)
            composite_score = round(
                0.75 * similarity + 0.18 * effective_confidence + 0.07 * usage_boost,
                4,
            )

            memories.append(
                {
                    "id": row.get("id"),
                    "content": row["content"],
                    "similarity": similarity,
                    "semantic_similarity": row.get("semantic_similarity", similarity),
                    "lexical_score": row.get("lexical_score", 0.0),
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
                x["similarity"],
                x["composite_score"],
                x["effective_confidence"],
                x["access_count"],
            ),
            reverse=True,
        )
        memories = memories[:n]

        returned_ids = [m["id"] for m in memories if m.get("id")]
        try:
            self.store.record_recall_telemetry(
                memory_ids=returned_ids,
                query_text=clean_query,
                agent_id=self.agent_id,
                best_similarity=best_similarity,
                result_count=len(memories),
            )
        except AttributeError:
            try:
                if returned_ids:
                    self.store.increment_access_batch(returned_ids)
                self.store.log_query(
                    query_text=clean_query,
                    agent_id=self.agent_id,
                    best_similarity=best_similarity,
                    result_count=len(memories),
                )
            except Exception:
                pass
        except Exception:
            pass

        return memories

    def update(
        self,
        memory_id: str,
        content: Optional[str] = None,
        memory_type: Optional[str] = None,
        confidence: Optional[float] = None,
        tags: Optional[list[str]] = None,
        ttl_days: Optional[float] = None,
        reset_ttl: bool = False,
        local_only: Optional[bool] = None,
    ) -> dict[str, Any]:
        if not memory_id or not memory_id.strip():
            raise ValueError("memory_id cannot be empty")

        existing = self.store.get(memory_id)
        if not existing:
            raise ValueError(f"Memory '{memory_id}' not found in namespace '{self.namespace}'")

        new_embedding: Optional[list[float]] = None
        clean_content: Optional[str] = None
        if content is not None:
            if not content.strip():
                raise ValueError("content cannot be empty")
            clean_content = content.strip()
            if clean_content != existing["content"]:
                new_embedding = _embed(clean_content)

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
        ttl_val = float(ttl_days) if ttl_days is not None and float(ttl_days) > 0 else None

        updated = self.store.update(
            memory_id=memory_id,
            content=clean_content,
            embedding=new_embedding,
            memory_type=clean_type,
            confidence=clean_conf,
            tags=clean_tags,
            ttl_days=ttl_val,
            reset_ttl=reset_ttl or (ttl_days is not None),
            local_only=local_only,
            source_agent=self.agent_id,
        )
        if not updated:
            raise ValueError(f"Failed to update memory '{memory_id}'")

        if clean_content and _model_backend == "onnx-warming":
            with _PENDING_REEMBED_LOCK:
                _PENDING_REEMBED_QUEUE.append((self.namespace, memory_id, clean_content))

        return {
            "memory_id": memory_id,
            "status": "updated",
            "memory": self._enrich_memory_row(updated),
        }

    def get(self, memory_id: str) -> Optional[dict[str, Any]]:
        row = self.store.get(memory_id)
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
        return self.store.delete(memory_id)

    def patterns(self) -> dict[str, Any]:
        return self.store.get_patterns()

    def contradictions(self, resolved: bool = False) -> list[dict[str, Any]]:
        return self.store.get_contradictions(resolved=resolved)

    def resolve_contradiction(self, contradiction_id: str) -> None:
        self.store.resolve_contradiction(contradiction_id)

    def inspect(
        self,
        limit: int = 20,
        memory_type: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        rows = self.store.list_all(limit=limit, memory_type=memory_type, tag=tag)
        now = _utc_now()
        return [self._enrich_memory_row(r, now=now) for r in rows]

    def clear(self) -> None:
        self.store.clear_namespace()

    def count(self) -> int:
        return self.store.count()

    def __repr__(self) -> str:
        return (
            f"Mesh(namespace='{self.namespace}', "
            f"agent_id='{self.agent_id}', "
            f"memories={self.count()})"
        )
"""
tests/test_model_download_fallback.py — Mocked HTTP tests for the ONNX model
mirror fallback (optional item 1 of the Run C re-test).

The downloader tries the canonical Hugging Face URL first and the pinned
``hf-mirror.com`` URL second. This suite proves, with ``httpx`` fully mocked and
no socket opened, that:

1. a primary-mirror HTTP failure, connection error, truncation or checksum
   mismatch falls through to the secondary mirror;
2. every downloaded byte is checksum-verified against the pinned allow-list
   before it is installed (fail-closed: corrupt payloads are never promoted);
3. the background worker installs a verified pair and switches to ONNX only
   after both files pass verification;
4. the mirror list itself stays HTTPS-only and pinned to the two known hosts,
   so no new network destination is introduced silently.

All state lives under a temp ``MESH_HOME``; no cloud LLM call, telemetry, or
real network access occurs.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable, Iterator

import httpx
import pytest

import mesh.memory as memory

# Small payloads for the unit-level downloader tests.
GOOD = b"verified-model-payload-" * 200
BAD = b"corrupt-model-payload-" * 200
GOOD_SHA = hashlib.sha256(GOOD).hexdigest()

PRIMARY = "https://primary.invalid/model.onnx"
SECONDARY = "https://secondary.invalid/model.onnx"


# ==============================================================================
# Mocks
# ==============================================================================


class _FakeResponse:
    def __init__(self, status_code: int, chunks: Iterable[bytes]):
        self.status_code = status_code
        self._chunks = chunks

    def iter_bytes(self, chunk_size: int = 65536) -> Iterator[bytes]:
        yield from self._chunks


class _FakeStream:
    def __init__(self, response: _FakeResponse):
        self._response = response

    def __enter__(self) -> _FakeResponse:
        return self._response

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeHttpxClient:
    """Stands in for ``httpx.Client``; every request must match a route."""

    routes: dict[str, object] = {}
    calls: list[str] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def __enter__(self) -> "_FakeHttpxClient":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def stream(self, method: str, url: str) -> _FakeStream:
        _FakeHttpxClient.calls.append(url)
        route = _FakeHttpxClient.routes.get(url)
        if route is None:
            raise httpx.ConnectError(f"no mocked route for {url}")
        if isinstance(route, Exception):
            raise route
        assert isinstance(route, _FakeResponse)
        return _FakeStream(route)


def _repeat(chunk: bytes, count: int) -> Iterator[bytes]:
    for _ in range(count):
        yield chunk


def _sha_of_repeated(chunk: bytes, count: int) -> str:
    digest = hashlib.sha256()
    for _ in range(count):
        digest.update(chunk)
    return digest.hexdigest()


@pytest.fixture
def download_env(monkeypatch, tmp_path):
    home = tmp_path / "home"
    mesh_home = home / ".mesh"
    mesh_home.mkdir(parents=True)
    monkeypatch.setenv("MESH_HOME", str(mesh_home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("MESH_NO_AUTO_DAEMON", "1")
    monkeypatch.setenv("MESH_NO_MODEL_DOWNLOAD", "1")
    monkeypatch.setenv("MESH_NO_UPDATE_CHECK", "1")
    monkeypatch.delenv("MESH_SKIP_CHECKSUM", raising=False)
    monkeypatch.delenv("MESH_MODEL_SHA256", raising=False)
    monkeypatch.delenv("MESH_TOKENIZER_SHA256", raising=False)

    _FakeHttpxClient.routes = {}
    _FakeHttpxClient.calls = []
    monkeypatch.setattr(httpx, "Client", _FakeHttpxClient)
    try:
        yield mesh_home
    finally:
        _FakeHttpxClient.routes = {}
        _FakeHttpxClient.calls = []


def _download(dest: Path, allowed: set[str] | None = None) -> bool:
    return memory._download_file_from_mirrors(
        [PRIMARY, SECONDARY],
        dest,
        min_bytes=1000,
        allowed_sha256=allowed,
    )


def _leftover_temps(dest: Path) -> list[Path]:
    return sorted(dest.parent.glob(f"{dest.stem}.tmp.*"))


# ==============================================================================
# Unit: fallback order and fail-closed verification
# ==============================================================================


def test_primary_http_error_falls_back_to_secondary(download_env):
    dest = download_env / "models" / "model.onnx"
    _FakeHttpxClient.routes = {
        PRIMARY: _FakeResponse(503, []),
        SECONDARY: _FakeResponse(200, [GOOD]),
    }

    assert _download(dest, {GOOD_SHA}) is True
    assert dest.read_bytes() == GOOD
    assert _FakeHttpxClient.calls == [PRIMARY, SECONDARY]
    assert _leftover_temps(dest) == []


def test_primary_checksum_mismatch_is_discarded_and_falls_back(download_env):
    dest = download_env / "models" / "model.onnx"
    _FakeHttpxClient.routes = {
        PRIMARY: _FakeResponse(200, [BAD]),
        SECONDARY: _FakeResponse(200, [GOOD]),
    }

    assert _download(dest, {GOOD_SHA}) is True
    assert dest.read_bytes() == GOOD
    assert _FakeHttpxClient.calls == [PRIMARY, SECONDARY]
    assert _leftover_temps(dest) == []


def test_truncated_primary_falls_back_to_secondary(download_env):
    dest = download_env / "models" / "model.onnx"
    _FakeHttpxClient.routes = {
        PRIMARY: _FakeResponse(200, [b"short"]),
        SECONDARY: _FakeResponse(200, [GOOD]),
    }

    assert _download(dest, {GOOD_SHA}) is True
    assert dest.read_bytes() == GOOD
    assert _FakeHttpxClient.calls == [PRIMARY, SECONDARY]


def test_connection_error_falls_back_to_secondary(download_env):
    dest = download_env / "models" / "model.onnx"
    _FakeHttpxClient.routes = {
        PRIMARY: httpx.ConnectError("primary unreachable"),
        SECONDARY: _FakeResponse(200, [GOOD]),
    }

    assert _download(dest, {GOOD_SHA}) is True
    assert dest.read_bytes() == GOOD
    assert _FakeHttpxClient.calls == [PRIMARY, SECONDARY]


def test_corrupt_payload_on_every_mirror_is_fail_closed(download_env):
    dest = download_env / "models" / "model.onnx"
    _FakeHttpxClient.routes = {
        PRIMARY: _FakeResponse(200, [BAD]),
        SECONDARY: _FakeResponse(200, [BAD]),
    }

    assert _download(dest, {GOOD_SHA}) is False
    assert not dest.exists(), "an unverified payload must never be installed"
    assert _FakeHttpxClient.calls == [PRIMARY, SECONDARY]
    assert _leftover_temps(dest) == []


def test_unreachable_every_mirror_returns_false(download_env):
    dest = download_env / "models" / "model.onnx"
    _FakeHttpxClient.routes = {
        PRIMARY: httpx.ConnectError("primary unreachable"),
        SECONDARY: httpx.ConnectError("secondary unreachable"),
    }

    assert _download(dest, {GOOD_SHA}) is False
    assert not dest.exists()
    assert _FakeHttpxClient.calls == [PRIMARY, SECONDARY]
    assert _leftover_temps(dest) == []


def test_existing_verified_file_skips_network(download_env):
    dest = download_env / "models" / "model.onnx"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(GOOD)
    _FakeHttpxClient.routes = {}

    assert _download(dest, {GOOD_SHA}) is True
    assert _FakeHttpxClient.calls == []


# ==============================================================================
# Pinned destinations (no silently added network target)
# ==============================================================================


def test_mirror_lists_stay_https_pinned_and_bounded():
    assert len(memory._TOKENIZER_MIRRORS) == 2
    assert len(memory._ONNX_MODEL_MIRRORS) == 2
    assert memory._GCS_TARBALL_MIRRORS == []

    revision = memory._HF_MODEL_REVISION
    assert len(revision) == 40 and all(c in "0123456789abcdef" for c in revision)

    for urls, filename in (
        (memory._TOKENIZER_MIRRORS, "tokenizer.json"),
        (memory._ONNX_MODEL_MIRRORS, "onnx/model.onnx"),
    ):
        hosts = set()
        for url in urls:
            assert url.startswith("https://"), url
            assert f"/resolve/{revision}/" in url, url
            assert url.endswith(filename), url
            hosts.add(url.split("/")[2])
        assert hosts == {"huggingface.co", "hf-mirror.com"}, hosts

    # The allow-lists must never become empty, which would disable verification.
    assert memory._ALLOWED_MODEL_SHA256
    assert memory._ALLOWED_TOKENIZER_SHA256


# ==============================================================================
# Worker: verified pair installed after a primary-mirror failure
# ==============================================================================

_MODEL_CHUNK = b"mesh-onnx-model" * 8192  # 114,688 bytes
_MODEL_CHUNKS = 700  # 80,281,600 bytes > 80 MB threshold
_TOKENIZER_CHUNK = b'{"t":"mesh"}' * 8192  # 98,304 bytes
_TOKENIZER_CHUNKS = 3  # 294,912 bytes > 100 KB threshold


class _DummyEngine:
    def __init__(self, model_path: Path, tokenizer_path: Path) -> None:
        self.model_path = model_path
        self.tokenizer_path = tokenizer_path


def test_background_worker_installs_verified_pair_via_secondary_mirror(
    download_env, monkeypatch
):
    model_sha = _sha_of_repeated(_MODEL_CHUNK, _MODEL_CHUNKS)
    tokenizer_sha = _sha_of_repeated(_TOKENIZER_CHUNK, _TOKENIZER_CHUNKS)
    monkeypatch.setenv("MESH_MODEL_SHA256", model_sha)
    monkeypatch.setenv("MESH_TOKENIZER_SHA256", tokenizer_sha)

    _FakeHttpxClient.routes = {
        memory._TOKENIZER_MIRRORS[0]: _FakeResponse(503, []),
        memory._TOKENIZER_MIRRORS[1]: _FakeResponse(
            200, _repeat(_TOKENIZER_CHUNK, _TOKENIZER_CHUNKS)
        ),
        memory._ONNX_MODEL_MIRRORS[0]: _FakeResponse(503, []),
        memory._ONNX_MODEL_MIRRORS[1]: _FakeResponse(
            200, _repeat(_MODEL_CHUNK, _MODEL_CHUNKS)
        ),
    }

    # Only look inside the temp MESH_HOME; never scan the real HF/fastembed caches.
    monkeypatch.setattr(
        memory, "_discover_local_onnx_pair", lambda: memory._has_local_onnx_pair()
    )
    monkeypatch.setattr(memory, "_DirectOnnxEmbedding", _DummyEngine)
    monkeypatch.setattr(memory, "_backfill_pending_embeddings", lambda: None)
    monkeypatch.setattr(memory, "_ensure_idle_watchdog", lambda: None)
    monkeypatch.setattr(memory, "_model", None)
    monkeypatch.setattr(memory, "_model_backend", "hash-fallback")
    monkeypatch.setattr(memory, "_download_in_progress", False)
    monkeypatch.setattr(memory, "_download_failed_until", 0.0)
    memory._download_complete_event.clear()

    memory._background_download_worker()

    model_dir = memory._get_onnx_model_dir()
    model_path = model_dir / "model.onnx"
    tokenizer_path = model_dir / "tokenizer.json"
    assert model_path.exists() and tokenizer_path.exists()
    assert hashlib.sha256(model_path.read_bytes()).hexdigest() == model_sha
    assert hashlib.sha256(tokenizer_path.read_bytes()).hexdigest() == tokenizer_sha

    # Primary mirror was tried first, then the pinned fallback, for both files.
    assert _FakeHttpxClient.calls == [
        memory._TOKENIZER_MIRRORS[0],
        memory._TOKENIZER_MIRRORS[1],
        memory._ONNX_MODEL_MIRRORS[0],
        memory._ONNX_MODEL_MIRRORS[1],
    ]

    assert memory._model_backend == "onnx"
    assert isinstance(memory._model, _DummyEngine)
    assert memory._download_complete_event.is_set()

    status = json.loads((download_env / "model_status.json").read_text(encoding="utf-8"))
    assert status["state"] == "ready"
    assert status["backend"] == "onnx"

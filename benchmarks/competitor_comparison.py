"""Source-bound, offline competitor retrieval comparison for GitHub Actions.

Only preparation fetches static fixtures. Measured workers have isolated homes,
fail-closed LLM adapters and no external network. Labels are opened after the
ranked export is closed. Previously inspected fixtures remain regressions.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import urllib.request
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parents[1]
TOOLS = ("mesh", "mem0", "basic-memory", "auxly")
DATASETS = ("prospective", "longtext-prospective", "longmemeval", "nfcorpus", "scifact")
PINS = {
    "mem0": {"distribution": "mem0ai", "version": "2.2.1", "sha256": "fe91bb91ac8926231993a4aa58df00a60c6c74c709e6a338fe399500776eef4d"},
    "basic-memory": {"distribution": "basic-memory", "version": "0.24.1", "sha256": "b528fff0a92bc91683e791553f9b10f57f7e3185a1aa15d494f5b8294eaad89e"},
    "auxly": {"version": "1.5.0", "sha256": "ec452fc983e862f78f4cf23d41d3e25396ebff974508aceb45e8224720759b04"},
}
MODEL = {
    "revision": "1110a243fdf4706b3f48f1d95db1a4f5529b4d41",
    "model.onnx": "6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452",
    "tokenizer.json": "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037",
}
FASTEMBED_VERSION = "0.9.1"
BM25_REVISION = "22b8d2af71a76161e18dd432d2cee0eefa66e412"
BM25_FILES = {
    "config.json": (2, "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"),
    "arabic.txt": (6348, "0df01c0a184d8c15077c6f0ee70e25e0c0308d827b92b14a19f5f819a0c465d0"),
    "danish.txt": (424, "1bedc9cf5a8830dacf8c4ee0d8b301f0801861756ad0d504431d01047f961b0c"),
    "dutch.txt": (453, "e5a2a7c390fe3ad0c0a132586ed11492b635d66a1f426cd89677f80b07bc76a6"),
    "english.txt": (936, "019f104ba2ed07436d05f9cdd3383034ad66014edc27fc651f837e1a038b6451"),
    "finnish.txt": (1579, "952af766edc9b8e7ddc877fc464cbd94b91754b5621fdfdd7020568fd4813fcd"),
    "french.txt": (813, "58ac7f7c074b70dc0fc86f6fcf40b2c227e9f95627e90e57971d5cce1e30e2e9"),
    "german.txt": (1362, "f0c25acd7ec02a31f6679d9664ec5222ca846fdbc58db96160f4e7c0ddb0f7ea"),
    "greek.txt": (2167, "7a97c6dc81144f7e8fcfe976025954ca3731f78a75d1fd86aa87f261fd195dae"),
    "hungarian.txt": (1227, "64a68b5ebaa616b25bd976ac915c326f2ea8cce8b59a0ac8c5728b980ae4fb0c"),
    "italian.txt": (1654, "293d7841f198e4012f49e8e5653c3bfd073a58cea1259fa2c0fcec894167b628"),
    "norwegian.txt": (851, "f7e5b42208ccf1b1f282f9e0f8570e464272762bda5718b6b26750f510a688dc"),
    "portuguese.txt": (1286, "6e7b98378c6b728266a83ebf035d927abf16b9e0d815d6204d81d90d896bcc9b"),
    "romanian.txt": (1910, "0ab53d21cbde00c6bf367706a0243c646140ba564cce7e83ac33d80f8923e957"),
    "russian.txt": (1235, "1743191192b4a4f77fcc216499455dc00c1b8626fdd407076a8deefff80e3d59"),
    "spanish.txt": (2176, "6125eadf28ba664a60bf4296147bcbd40b80be93670056fdb229960ac15e2310"),
    "swedish.txt": (559, "2a9d9d756bc4257d49329994f805c712cd5ab6746162e2082752238766c1c0c8"),
    "turkish.txt": (260, "f2c7f0c2bd3dba42da700776266831853a3b5f1207fada5c15207334109abb57"),
}
QUALIFICATION_COMMIT = "e4284c8fb5ba565043accccbdd6353839c3eb43a"
QUALIFICATION_HARNESS = {"benchmarks/production_bench.py", "benchmarks/production_transport.py", "benchmarks/embedding_shadow.py", "benchmarks/retrieval_ci.py", "benchmarks/storage_profile.py"}
PROFILES = {
    "mesh": {"interface": "SDK", "ranking": "production default hybrid", "embedding": "pinned MiniLM; production encoder"},
    "mem0": {"interface": "SDK", "ranking": "native local Qdrant hybrid; infer=False; no reranker", "embedding": "pinned MiniLM; 256-token truncated ONNX adapter"},
    "basic-memory": {"interface": "MCP stdio", "ranking": "local keyword search", "embedding": "none; keyword-only configuration"},
    "auxly": {"interface": "MCP stdio", "ranking": "native substring search; unordered results", "embedding": "none; keyword-only configuration"},
}
WORKER_FAILURE_CODES = frozenset({
    "SourceBodyChangedDuringSearch", "StoredBodyMismatch", "UnknownReturnedIdentity",
    "CompetitorIndexFailed", "MissingCompetitorExecutable", "McpToolError",
    "ChildEnvironmentChanged", "ChildEnvironmentFilesChanged", "ChildInterpreterMismatch",
    "ChildEncoderVersionMismatch", "CompetitorVersionMismatch", "KeywordProfileHasVectorRuntime",
    "ReturnedBodyMismatch", "IncompleteWorkerAccounting", "ExternalNetworkForbidden",
    "MissingAcknowledgedIdentity", "UnrecognizedCompetitorOutput", "Bm25IncompleteNativeAccounting",
    "Bm25SparseWriteFailed", "Bm25SparseQueryFailed", "Bm25EncodingFailed",
    "MeshLearnFailed",
})
WORKER_FAILURE_FILES = frozenset({
    "competitor_comparison.py", "stdio.py", "session.py", "main.py", "project_service.py",
    "project_readiness.py", "sync_service.py", "config.py", "config_models.py",
})
WORKER_FAILURE_TYPES = frozenset({
    "Exception", "ComparisonError", "RuntimeError", "ValueError", "TypeError", "KeyError",
    "OSError", "FileNotFoundError", "PermissionError", "TimeoutError", "ModuleNotFoundError",
    "ImportError", "ExceptionGroup", "BaseExceptionGroup", "CancelledError", "ConnectionError",
    "KeyboardInterrupt", "SystemExit", "IncompleteReadError",
})


class ComparisonError(RuntimeError):
    """Fixed content-free diagnostic code."""


class ParentRejected(Exception):
    """Only Mesh's content-ingestion boundary can reject a source parent."""


def mesh_content_rejection(error: ValueError) -> bool:
    trace = error.__traceback__
    while trace is not None and trace.tb_next is not None:
        trace = trace.tb_next
    if trace is None or trace.tb_frame.f_code.co_name != "_prepare_learn" or Path(trace.tb_frame.f_code.co_filename).name != "memory.py":
        return False
    # Do not treat scope, metadata or fixed caller-argument errors as data loss.
    values = trace.tb_frame.f_locals
    limit = trace.tb_frame.f_globals.get("MAX_CONTENT_LENGTH")
    if type(limit) is not int or limit <= 0:
        return False
    content = values.get("content")
    if not isinstance(content, str) or not content.strip() or len(content) > limit:
        return True
    guarded = values.get("guarded")
    if guarded is not None and getattr(guarded, "rejected", False) is True:
        return True
    clean_content = values.get("clean_content")
    return "clean_content" in values and isinstance(clean_content, str) and (not clean_content or len(clean_content) > limit)


def worker_failure_projection(error: BaseException) -> dict:
    """Project bounded diagnostic identities without third-party exception text."""
    pending, failures = [error], []
    while pending and len(failures) < 8:
        item = pending.pop(0)
        nested = getattr(item, "exceptions", ())
        if isinstance(nested, tuple):
            pending.extend(nested[:8])
        name = type(item).__name__
        if name not in WORKER_FAILURE_TYPES:
            name = "Exception"
        code = item.args[0] if isinstance(item, ComparisonError) and len(item.args) == 1 and type(item.args[0]) is str and item.args[0] in WORKER_FAILURE_CODES else None
        frames, trace = [], item.__traceback__
        while trace is not None and len(frames) < 12:
            source = Path(trace.tb_frame.f_code.co_filename).name
            if source in WORKER_FAILURE_FILES and 0 < trace.tb_lineno < 1000000:
                frames.append({"source": source, "line": trace.tb_lineno})
            trace = trace.tb_next
        failures.append({"exception_type": name, "code": code, "frames": frames})
    return {"protocol": "comparison-worker-failure-v1", "failures": failures}


def checked_worker_failure(value: dict) -> dict:
    if not isinstance(value, dict) or value.get("protocol") != "comparison-worker-failure-v1" or set(value) != {"protocol", "failures"} or not isinstance(value["failures"], list) or not 1 <= len(value["failures"]) <= 8:
        raise ComparisonError("InvalidWorkerDiagnostic")
    for item in value["failures"]:
        if not isinstance(item, dict) or set(item) != {"exception_type", "code", "frames"}:
            raise ComparisonError("InvalidWorkerDiagnostic")
        if type(item["exception_type"]) is not str or item["exception_type"] not in WORKER_FAILURE_TYPES or item["code"] is not None and (type(item["code"]) is not str or item["code"] not in WORKER_FAILURE_CODES) or not isinstance(item["frames"], list) or len(item["frames"]) > 12:
            raise ComparisonError("InvalidWorkerDiagnostic")
        for frame in item["frames"]:
            if not isinstance(frame, dict) or set(frame) != {"source", "line"} or type(frame["source"]) is not str or frame["source"] not in WORKER_FAILURE_FILES or type(frame["line"]) is not int or not 0 < frame["line"] < 1000000:
                raise ComparisonError("InvalidWorkerDiagnostic")
    return value


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def verify_bm25(directory: Path) -> dict:
    for name, (size, pin) in BM25_FILES.items():
        path = checked(directory / name, directory, exists=True)
        if path.stat().st_size != size or digest(path) != pin:
            raise ComparisonError("Bm25AssetDigestMismatch")
    return {"repo": "Qdrant/bm25", "revision": BM25_REVISION, "bytes": sum(size for size, _ in BM25_FILES.values()), "files": {name: pin for name, (_, pin) in BM25_FILES.items()}}


def provision_bm25(directory: Path) -> None:
    """Only immutable configuration and stopwords; no model runtime executes."""
    directory = checked(directory, REPO / ".work")
    directory.mkdir(parents=True, exist_ok=True)
    for name, (size, pin) in BM25_FILES.items():
        destination = checked(directory / name, directory)
        if destination.exists():
            if destination.stat().st_size != size or digest(destination) != pin:
                raise ComparisonError("Bm25AssetDigestMismatch")
            continue
        request = urllib.request.Request(f"https://huggingface.co/Qdrant/bm25/resolve/{BM25_REVISION}/{name}", headers={"User-Agent": "Mesh-comparison-fixture/1"})
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read(size + 1)
        if len(body) != size or hashlib.sha256(body).hexdigest() != pin:
            raise ComparisonError("Bm25AssetDigestMismatch")
        with destination.open("xb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
    print(json.dumps(verify_bm25(directory)))


def install_bm25_cache(directory: Path, root: Path) -> Path:
    verify_bm25(directory)
    cache = checked(root / "fastembed", root)
    repository = cache / "models--Qdrant--bm25"
    snapshot = repository / "snapshots" / BM25_REVISION
    checked(snapshot, root).mkdir(parents=True)
    for name in BM25_FILES:
        shutil.copyfile(checked(directory / name, directory, exists=True), snapshot / name)
    refs = repository / "refs"
    refs.mkdir()
    (refs / "main").write_text(BM25_REVISION, encoding="utf-8")
    verify_bm25(snapshot)
    return snapshot


def checked(path: Path, root: Path | None = None, *, exists: bool = False) -> Path:
    path = path.absolute()
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise ComparisonError("RedirectedPath")
    resolved = path.resolve(strict=exists)
    if root is not None and not resolved.is_relative_to(root.resolve()):
        raise ComparisonError("PathOutsideOwnedRoot")
    return resolved


def write_json(path: Path, value: dict) -> None:
    path = checked(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".new")
    checked(temporary)
    if temporary.exists():
        raise ComparisonError("ExistingTemporaryOutput")
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=True, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in checked(path, exists=True).read_text(encoding="utf-8").splitlines() if line.strip()]


def fixture(data: Path) -> tuple[dict, dict[str, list[dict]], list[dict]]:
    """Verify original input bytes without opening qrels or selecting by scores."""
    data = checked(data, exists=True)
    manifest = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("name") not in DATASETS:
        raise ComparisonError("UnknownDataset")
    for relative, pin in manifest["files"].items():
        if "\\" in relative or ":" in relative or relative.startswith("/") or any(p in {"", ".", ".."} for p in relative.split("/")):
            raise ComparisonError("UnsafeFixtureMember")
        expected = pin["sha256"] if isinstance(pin, dict) else pin
        if digest(checked(data / relative, data, exists=True)) != expected:
            raise ComparisonError("FixtureDigestMismatch")
    groups = {}
    for path in sorted(data.glob("corpus*.jsonl")):
        if path.name not in manifest["files"]:
            raise ComparisonError("UnregisteredCorpusFile")
        name = "complete" if path.name == "corpus.jsonl" else path.name
        rows = read_jsonl(path)
        if len({str(r.get("id", r.get("_id"))) for r in rows}) != len(rows):
            raise ComparisonError("DuplicateSourceIdentity")
        groups[name] = [{"id": str(r.get("id", r.get("_id"))), "content": r.get("content", (r.get("title", "") + "\n" + r.get("text", "")).strip()).strip()} for r in rows]
    queries = read_jsonl(data / "queries-selected.jsonl")
    if sum(map(len, groups.values())) != manifest["corpus_count"] or len(queries) != manifest["query_count"]:
        raise ComparisonError("FixtureCountMismatch")
    if len({q["id"] for q in queries}) != len(queries) or any(q.get("corpus", "complete") not in groups for q in queries):
        raise ComparisonError("QueryCorpusMismatch")
    if any(not isinstance(r["content"], str) or not r["content"].strip() for rows in groups.values() for r in rows):
        raise ComparisonError("InvalidSourceBody")
    return manifest, groups, queries


def isolated_environment(root: Path) -> dict[str, str]:
    preserved = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "LANG", "LC_ALL", "LC_CTYPE"}
    env = {name: value for name, value in os.environ.items() if name.upper() in preserved}
    for name, relative in {"HOME": "home", "USERPROFILE": "home", "MESH_HOME": "mesh-home", "BASIC_MEMORY_CONFIG_DIR": "basic-home", "APPDATA": "appdata", "LOCALAPPDATA": "localappdata", "XDG_CONFIG_HOME": "xdg/config", "XDG_CACHE_HOME": "xdg/cache", "XDG_DATA_HOME": "xdg/data", "CODEX_HOME": "codex", "CLAUDE_CONFIG_DIR": "claude", "MEM0_DIR": "mem0", "FASTEMBED_CACHE_PATH": "fastembed", "HF_HOME": "hf-home", "HF_HUB_CACHE": "hf-home/hub", "TEMP": "tmp", "TMP": "tmp", "TMPDIR": "tmp"}.items():
        path = root / relative
        path.mkdir(parents=True, exist_ok=True)
        env[name] = str(path)
    env.update(MESH_NO_AUTO_DAEMON="1", MESH_NO_DAEMON_PROXY="1", MESH_NO_MODEL_DOWNLOAD="1", MESH_NO_UPDATE_CHECK="1", MESH_NO_IMPLICIT_CLOUD="1", MESH_AUTO_APPROVE="0", MESH_ALLOW_CLOUD_SYNC="0", MESH_ALLOW_CLOUD_CAPTURE="0", MESH_ENABLE_AUTO_SYNC="0", MESH_AUTO_DOWNLOAD_MODEL="0", MESH_RERANK="0", MESH_PASSAGE_INDEX="0", MEM0_TELEMETRY="false", BASIC_MEMORY_SEMANTIC_SEARCH_ENABLED="false", BASIC_MEMORY_ENABLE_CLOUD="false", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false", OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1", PYTHONUTF8="1")
    env.update(BASIC_MEMORY_AUTO_UPDATE="false", BASIC_MEMORY_RERANKER_ENABLED="false", BASIC_MEMORY_DEFAULT_SEARCH_TYPE="text", AUXLY_HOST_SELFHEAL="off", PYTHONDONTWRITEBYTECODE="1")
    env["PYTHONPATH"] = str(REPO)
    return env


def native_command(config: Path, interpreter: Path | None = None) -> list[str]:
    """A disconnected namespace applies to every Python and Go descendant."""
    if sys.platform != "linux" or any(shutil.which(name) is None for name in ("sudo", "unshare", "setpriv")):
        raise ComparisonError("NetworkIsolationUnavailable")
    python = sys.executable if interpreter is None else str(checked(interpreter, REPO / ".work", exists=True))
    return ["sudo", "-n", "--preserve-env", "unshare", "--net", "--pid", "--fork", "--kill-child=SIGKILL", "--mount-proc", "--", "setpriv", "--reuid", str(os.getuid()), "--regid", str(os.getgid()), "--init-groups", "--no-new-privs", python, str(Path(__file__)), "worker", "--config", str(config)]


def stop_observed_processes(psutil, owned: dict[tuple[int, float], int]) -> bool:
    """Settle retained host identities even after their original parent exits."""
    pending = []
    for (pid, created), group in owned.items():
        try:
            process = psutil.Process(pid)
            if process.create_time() != created:
                continue
            if group != os.getpgrp() and os.getpgid(pid) == group:
                os.killpg(group, signal.SIGKILL)
            else:
                process.kill()
            pending.append(process)
        except (psutil.NoSuchProcess, ProcessLookupError):
            continue
        except (psutil.AccessDenied, PermissionError):
            return False
    deadline = time.monotonic() + 30
    for process in pending:
        try:
            process.wait(timeout=max(.01, deadline - time.monotonic()))
        except psutil.NoSuchProcess:
            continue
        except (psutil.TimeoutExpired, psutil.AccessDenied):
            return False
    return True


@contextmanager
def owned_temporary_root(output: Path, state: dict):
    root = checked(Path(tempfile.mkdtemp(prefix="mesh-comparison-")), Path(tempfile.gettempdir()), exists=True)
    try:
        yield root
    finally:
        inventory = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
        write_json(output / "temporary-inventory-private.json", {"entries": inventory, "settled": state["settled"]})
        state["inventory_sha256"] = digest(output / "temporary-inventory-private.json")
        if state["settled"]:
            if not root.name.startswith("mesh-comparison-") or checked(root, Path(tempfile.gettempdir()), exists=True) != root:
                raise ComparisonError("InvalidOwnedCleanupRoot")
            shutil.rmtree(root)
            state["status"] = "removed_after_owned_process_settlement"
        else:
            state["status"] = "preserved_unsettled_process_home"


def deny_external_network() -> None:
    def guard(event, args):
        if event == "socket.connect":
            address = args[1]
            if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1", "localhost"}:
                raise ComparisonError("ExternalNetworkForbidden")
        if event == "socket.getaddrinfo" and args[0] not in {None, "127.0.0.1", "::1", "localhost"}:
            raise ComparisonError("ExternalNetworkForbidden")
    sys.addaudithook(guard)


class LocalOnnx:
    """Matched local MiniLM adapter; ranking/storage remain Mem0's implementation."""
    def __init__(self, directory: Path):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self.np = np
        self.tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=256)
        self.tokenizer.enable_padding(length=None)
        options = ort.SessionOptions()
        options.intra_op_num_threads = options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(directory / "model.onnx"), options, providers=["CPUExecutionProvider"])

    def embed(self, text, memory_action=None):
        return self.embed_batch([text], memory_action)[0]

    def embed_batch(self, texts, memory_action="add"):
        encoded = self.tokenizer.encode_batch(texts)
        np = self.np
        inputs = {"input_ids": np.array([e.ids for e in encoded], dtype=np.int64), "attention_mask": np.array([e.attention_mask for e in encoded], dtype=np.int64), "token_type_ids": np.array([e.type_ids for e in encoded], dtype=np.int64)}
        output = self.session.run(None, {name.name: inputs[name.name] for name in self.session.get_inputs()})[0]
        if output.ndim == 3:
            mask = inputs["attention_mask"][..., None].astype(np.float32)
            output = (output * mask).sum(axis=1) / np.maximum(mask.sum(axis=1), 1)
        output = output / np.maximum(np.linalg.norm(output, axis=1, keepdims=True), 1e-12)
        return output.tolist()


class ForbiddenLlm:
    def generate_response(self, *args, **kwargs):
        raise ComparisonError("LlmInferenceForbidden")


class MeshAdapter:
    def __init__(self, root: Path, models: Path, corpus: str):
        installed = root / "mesh-home/models/all-MiniLM-L6-v2-onnx"
        installed.mkdir(parents=True, exist_ok=True)
        for name in ("model.onnx", "tokenizer.json"):
            shutil.copyfile(models / name, installed / name)
        os.environ.update(MESH_REQUIRE_MODEL="1", MESH_QUERY_MODE="neural", MESH_SYNC_ONNX="1", MESH_FORCE_HASH_EMBED="0")
        from mesh.memory import Mesh, _validate_index_wait
        _validate_index_wait(True, 60)
        self.mesh = Mesh(namespace="comparison-" + corpus.replace(".", "-"), agent_id="comparison")
        self.ids = {}
        self.bodies = {}
        self.unfaithful = set()

    def add(self, row):
        try:
            result = self.mesh.learn(row["content"], local_only=False, wait_for_indexed=True, index_timeout=60)
        except ValueError as error:
            if mesh_content_rejection(error):
                raise ParentRejected() from None
            raise ComparisonError("MeshLearnFailed") from None
        identifier = result.get("memory_id", result.get("id"))
        if not identifier:
            raise ComparisonError("MissingAcknowledgedIdentity")
        stored = self.mesh.store.get(identifier)
        if not stored or stored.get("embedding_model") != "onnx-minilm-l6-v2":
            raise ComparisonError("IndexReadinessFailed")
        faithful = bool(stored["content"] == row["content"] and not stored.get("local_only") and not stored.get("quarantined"))
        if faithful:
            if self.bodies.get(identifier) != row["content"]:
                self.ids[identifier] = row["id"]
            self.bodies[identifier] = row["content"]
            self.unfaithful.discard(identifier)
        else:
            self.ids.pop(identifier, None)
            self.bodies.pop(identifier, None)
            self.unfaithful.add(identifier)
        return faithful

    def search(self, query):
        rows = self.mesh.recall(query, n=10, include_private=False, include_quarantined=False)
        result = []
        for row in rows:
            if row["id"] in self.unfaithful:
                result.append(None)
                continue
            if row["id"] not in self.ids:
                raise ComparisonError("UnknownReturnedIdentity")
            if row["content"] != self.bodies[row["id"]] or row.get("local_only") or row.get("quarantined"):
                raise ComparisonError("ReturnedBodyMismatch")
            result.append(self.ids[row["id"]])
        return result

    def close(self):
        self.mesh.close()


class Mem0Adapter:
    def __init__(self, root: Path, models: Path, corpus: str):
        from mem0.utils.factory import EmbedderFactory, LlmFactory
        local = LocalOnnx(models)
        EmbedderFactory.create = staticmethod(lambda *_args, **_kwargs: local)
        LlmFactory.create = staticmethod(lambda *_args, **_kwargs: ForbiddenLlm())
        from mem0 import Memory
        self.memory = Memory.from_config({"version": "v1.1", "history_db_path": str(root / (corpus + "-history.db")), "vector_store": {"provider": "qdrant", "config": {"collection_name": "comparison", "path": str(root / corpus), "embedding_model_dims": 384}}, "embedder": {"provider": "fastembed", "config": {"model": "sentence-transformers/all-MiniLM-L6-v2"}}, "llm": {"provider": "openai", "config": {}}})
        self.ids = {}
        self.bodies = {}
        self.monitor_bm25()

    def monitor_bm25(self):
        from fastembed import SparseTextEmbedding
        if importlib.metadata.version("fastembed") != FASTEMBED_VERSION:
            raise ComparisonError("FastembedVersionMismatch")
        store = self.memory.vector_store
        self.bm25_encoder = store._get_bm25_encoder()
        sparse_slots = store.client.get_collection(store.collection_name).config.params.sparse_vectors
        if not isinstance(self.bm25_encoder, SparseTextEmbedding) or not store._has_bm25_slot or not sparse_slots or "bm25" not in sparse_slots:
            raise ComparisonError("Bm25ReadinessFailed")
        self.bm25_counts = {"encoded": 0, "sparse_written": 0, "sparse_queried": 0, "fallback": False}
        original_embed = self.bm25_encoder.embed
        original_upsert = store.client.upsert
        original_keyword = store.keyword_search

        def embed(documents, *args, **kwargs):
            try:
                results = list(original_embed(documents, *args, **kwargs))
                if len(results) != len(documents) or any(len(row.indices) != len(row.values) or any(not math.isfinite(value) for value in row.values) for row in results):
                    raise ComparisonError("Bm25EncodingFailed")
                self.bm25_counts["encoded"] += len(results)
                return iter(results)
            except Exception:
                self.bm25_counts["fallback"] = True
                raise

        def upsert(*args, **kwargs):
            points = kwargs.get("points")
            if not isinstance(points, list) or any(not isinstance(point.vector, dict) or "bm25" not in point.vector for point in points):
                self.bm25_counts["fallback"] = True
                raise ComparisonError("Bm25SparseWriteFailed")
            result = original_upsert(*args, **kwargs)
            self.bm25_counts["sparse_written"] += len(points)
            return result

        def keyword(*args, **kwargs):
            result = original_keyword(*args, **kwargs)
            if result is None:
                self.bm25_counts["fallback"] = True
                raise ComparisonError("Bm25SparseQueryFailed")
            self.bm25_counts["sparse_queried"] += 1
            return result

        self.bm25_encoder.embed = embed
        store.client.upsert = upsert
        store.keyword_search = keyword

    def check_bm25(self, before: dict, action: str):
        store = self.memory.vector_store
        expected = "sparse_written" if action == "add" else "sparse_queried"
        if self.bm25_counts["fallback"] or store._bm25_encoder is not self.bm25_encoder or not store._has_bm25_slot or self.bm25_counts["encoded"] - before["encoded"] != 1 or self.bm25_counts[expected] - before[expected] != 1:
            raise ComparisonError("Bm25IncompleteNativeAccounting")

    def add(self, row):
        before = dict(self.bm25_counts)
        result = self.memory.add(row["content"], user_id="comparison", infer=False)
        self.check_bm25(before, "add")
        entries = result.get("results", [])
        if len(entries) != 1 or not entries[0].get("id"):
            raise ComparisonError("MissingAcknowledgedIdentity")
        identifier = entries[0]["id"]
        stored = self.memory.get(identifier)
        if not stored or stored.get("memory") != row["content"]:
            raise ComparisonError("StoredBodyMismatch")
        if self.bodies.get(identifier) != row["content"]:
            self.ids[identifier] = row["id"]
        self.bodies[identifier] = row["content"]
        return True

    def search(self, query):
        before = dict(self.bm25_counts)
        result = self.memory.search(query, filters={"user_id": "comparison"}, top_k=10, rerank=False)
        self.check_bm25(before, "search")
        rows = result.get("results", [])
        if any(row["id"] not in self.ids for row in rows):
            raise ComparisonError("UnknownReturnedIdentity")
        if any(row.get("memory") != self.bodies[row["id"]] for row in rows):
            raise ComparisonError("ReturnedBodyMismatch")
        return [self.ids[row["id"]] for row in rows]

    def close(self):
        self.memory.vector_store.client.close()


def mcp_json(result):
    if getattr(result, "isError", False):
        raise ComparisonError("McpToolError")
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    text = "".join(getattr(item, "text", "") for item in result.content)
    return json.loads(text)


def auxly_candidates(text: str, identifiers: dict[str, str]) -> list[str]:
    # Native headers start at column zero; matched source lines are indented.
    names = re.findall(r"^📄 (d\d{6})\.md[ \t]*$", text, flags=re.MULTILINE)
    if any(name not in identifiers for name in names):
        raise ComparisonError("UnknownReturnedIdentity")
    candidates = list(dict.fromkeys(identifiers[name] for name in names))[:10]
    if not candidates and text.strip() and not any(mark in text.lower() for mark in ("no results", "no match", "no memories", "not found")):
        raise ComparisonError("UnrecognizedCompetitorOutput")
    return candidates


async def mcp_group(tool: str, root: Path, rows: list[dict], queries: list[dict], output, accounting: dict, progress):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    vault = root / "notes"
    vault.mkdir(parents=True)
    identifiers = {f"d{index:06d}": row["id"] for index, row in enumerate(rows)}
    begin = time.monotonic()
    for index, row in enumerate(rows):
        body = row["content"]
        path = vault / f"d{index:06d}.md"
        # Native sync adds a missing permalink; seed it to preserve note bytes.
        source = f"---\ntitle: d{index:06d}\ntype: note\npermalink: d{index:06d}\n---\n{body}\n" if tool == "basic-memory" else body
        path.write_text(source, encoding="utf-8", newline="\n")
        if path.read_bytes() != source.encode("utf-8"):
            raise ComparisonError("StoredBodyMismatch")
    env = dict(os.environ)
    env["BASIC_MEMORY_CONFIG_DIR"] = str(root / "config")
    if tool == "basic-memory":
        executable = Path(sys.executable).parent / "basic-memory"
        if not executable.is_file():
            raise ComparisonError("MissingCompetitorExecutable")
        executable = str(checked(executable, REPO / ".work", exists=True))
        for args in (["project", "add", "comparison", str(vault), "--local"],):
            result = subprocess.run([executable, *args], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
            if result.returncode:
                raise ComparisonError("CompetitorIndexFailed")
        params = StdioServerParameters(command=executable, args=["mcp"], env=env, cwd=str(root))
    else:
        executable = checked(Path(env["COMPARISON_AUXLY"]), exists=True)
        if digest(executable) != PINS["auxly"]["sha256"]:
            raise ComparisonError("CompetitorBinaryDigestMismatch")
        params = StdioServerParameters(command=str(executable), args=["--path", str(vault), "mcp-server"], env=env, cwd=str(root))
    accounting["ingested"] += len(rows)
    accounting["faithful"] += len(rows)
    accounting["ingest_seconds"] += time.monotonic() - begin
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=120)
            for query in queries:
                begin = time.monotonic()
                if tool == "basic-memory":
                    result = await asyncio.wait_for(session.call_tool("search_notes", arguments={"query": query["query"], "project": "comparison", "output_format": "json", "search_type": "text", "page_size": 10}), timeout=120)
                    data = mcp_json(result)
                    matches = data.get("results", [])
                    if any(row.get("type") != "entity" or row.get("file_path") != row.get("title", "") + ".md" or row.get("title") not in identifiers for row in matches):
                        raise ComparisonError("UnknownReturnedIdentity")
                    candidates = [identifiers[row["title"]] for row in matches]
                else:
                    result = await asyncio.wait_for(session.call_tool("auxly_memory_search", arguments={"query": query["query"]}), timeout=120)
                    if getattr(result, "isError", False):
                        raise ComparisonError("McpToolError")
                    text = "".join(getattr(item, "text", "") for item in result.content)
                    candidates = auxly_candidates(text, identifiers)
                elapsed = (time.monotonic() - begin) * 1000
                append_query(output, query, candidates, elapsed)
                accounting["queried"] += 1
                progress()
    for index, row in enumerate(rows):
        body = f"---\ntitle: d{index:06d}\ntype: note\npermalink: d{index:06d}\n---\n{row['content']}\n" if tool == "basic-memory" else row["content"]
        if (vault / f"d{index:06d}.md").read_bytes() != body.encode("utf-8"):
            raise ComparisonError("SourceBodyChangedDuringSearch")


def append_query(stream, query: dict, candidates: list[str | None], elapsed_ms: float) -> None:
    faithful = [identifier for identifier in candidates if identifier is not None]
    if len(candidates) > 10 or len(set(faithful)) != len(faithful) or not math.isfinite(elapsed_ms) or elapsed_ms < 0:
        raise ComparisonError("InvalidRankedExport")
    stream.write(json.dumps({"query_ref": text_digest(query["id"]), "candidate_refs": [text_digest(identifier) if identifier is not None else None for identifier in candidates], "latency_ms": elapsed_ms}, allow_nan=False) + "\n")
    stream.flush()


def worker(config: dict) -> int:
    deny_external_network()
    _, groups, queries = fixture(Path(config["data"]))
    tool, root, models = config["tool"], Path(config["home"]), Path(config["models"])
    for name in ("model.onnx", "tokenizer.json"):
        if digest(models / name) != MODEL[name]:
            raise ComparisonError("ModelDigestMismatch")
    if tool in ("mem0", "basic-memory") and importlib.metadata.version(PINS[tool]["distribution"]) != PINS[tool]["version"]:
        raise ComparisonError("CompetitorVersionMismatch")
    installed = {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()}
    if tool in ("mem0", "basic-memory"):
        verify_child_inventory(config["child_environment"], installed, sys.version.split()[0])
        if verify_child_files(config["child_environment"]) != checked(Path(sys.executable), REPO / ".work", exists=True):
            raise ComparisonError("ChildInterpreterMismatch")
    elif not dependency_report(config["qualification"], installed)["compatible"]:
        raise ComparisonError("SharedDependencyVersionMismatch")
    bm25_snapshot = install_bm25_cache(Path(config["bm25"]), root) if tool == "mem0" else None
    accounting = {"submitted": sum(map(len, groups.values())), "ingested": 0, "faithful": 0, "unfaithful": 0, "rejected": 0, "queried": 0, "ingest_seconds": 0.0}
    if tool == "mem0":
        accounting["native_bm25"] = {}
    progress_path = Path(config["output"]) / "progress.json"

    def progress():
        write_json(progress_path, accounting)

    with (Path(config["output"]) / "ranked.jsonl").open("x", encoding="utf-8", newline="\n") as stream:
        for name, rows in groups.items():
            selected = [query for query in queries if query.get("corpus", "complete") == name]
            own = root / name
            own.mkdir()
            if tool in ("basic-memory", "auxly"):
                asyncio.run(mcp_group(tool, own, rows, selected, stream, accounting, progress))
                continue
            adapter = (MeshAdapter if tool == "mesh" else Mem0Adapter)(root, models, name)
            try:
                begin = time.monotonic()
                for row in rows:
                    try:
                        faithful = adapter.add(row)
                    except ParentRejected:
                        if tool != "mesh":
                            raise
                        accounting["rejected"] += 1
                        continue
                    accounting["ingested"] += 1
                    accounting["faithful"] += int(faithful)
                    accounting["unfaithful"] += int(not faithful)
                accounting["ingest_seconds"] += time.monotonic() - begin
                if tool == "mem0":
                    accounting["native_bm25"][name] = dict(adapter.bm25_counts)
                progress()
                for query in selected:
                    begin = time.monotonic()
                    candidates = adapter.search(query["query"])
                    append_query(stream, query, candidates, (time.monotonic() - begin) * 1000)
                    accounting["queried"] += 1
                    if tool == "mem0":
                        accounting["native_bm25"][name] = dict(adapter.bm25_counts)
                    progress()
            finally:
                if tool == "mem0":
                    accounting["native_bm25"][name] = dict(adapter.bm25_counts)
                    progress()
                adapter.close()
    if accounting["ingested"] + accounting["rejected"] != accounting["submitted"] or accounting["queried"] != len(queries):
        raise ComparisonError("IncompleteWorkerAccounting")
    if bm25_snapshot is not None:
        verify_bm25(bm25_snapshot)
        verify_bm25(Path(config["bm25"]))
    if tool in ("mem0", "basic-memory"):
        final_packages = {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()}
        verify_child_inventory(config["child_environment"], final_packages, sys.version.split()[0])
        verify_child_files(config["child_environment"])
    return 0


def score_rows(rows: list[dict], labels: dict, queries: list[dict]) -> dict:
    expected = {text_digest(query["id"]) for query in queries}
    if len(expected) != len(queries) or len(rows) != len(expected) or {row["query_ref"] for row in rows} != expected:
        raise ComparisonError("IncompleteRankedExport")
    for row in rows:
        candidates = row.get("candidate_refs")
        if not isinstance(candidates, list) or len(candidates) > 10 or any(value is not None and (not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)) for value in candidates):
            raise ComparisonError("InvalidRankedExport")
        faithful = [value for value in candidates if value is not None]
        if len(set(faithful)) != len(faithful):
            raise ComparisonError("DuplicateRankedIdentity")
    if set(labels["qrels"]) - {query["id"] for query in queries}:
        raise ComparisonError("UnexpectedLabelQuery")
    qrels = {}
    for query, gold in labels["qrels"].items():
        if any(type(grade) is not int or grade < 0 for grade in gold.values()):
            raise ComparisonError("InvalidRelevanceGrade")
        qrels[text_digest(query)] = {text_digest(doc): grade for doc, grade in gold.items() if grade > 0}
    answered = [row for row in rows if qrels.get(row["query_ref"])]
    metrics = {"answered_queries": len(answered), "all_queries": len(rows)}
    for cutoff in (1, 5, 10):
        hits = [int(any(identifier in qrels[row["query_ref"]] for identifier in row["candidate_refs"][:cutoff])) for row in answered]
        metrics[f"hit_at_{cutoff}"] = statistics.mean(hits) if hits else None
        recalls = [len(set(row["candidate_refs"][:cutoff]) & set(qrels[row["query_ref"]])) / len(qrels[row["query_ref"]]) for row in answered]
        metrics[f"document_recall_at_{cutoff}"] = statistics.mean(recalls) if recalls else None
        ndcg = []
        for row in answered:
            gold = qrels[row["query_ref"]]
            dcg = sum((2 ** gold.get(identifier, 0) - 1) / math.log2(index + 2) for index, identifier in enumerate(row["candidate_refs"][:cutoff]))
            ideal = sum((2 ** grade - 1) / math.log2(index + 2) for index, grade in enumerate(sorted(gold.values(), reverse=True)[:cutoff]))
            ndcg.append(dcg / ideal)
        metrics[f"ndcg_at_{cutoff}"] = statistics.mean(ndcg) if ndcg else None
    ranks = [next((1 / rank for rank, identifier in enumerate(row["candidate_refs"], 1) if identifier in qrels[row["query_ref"]]), 0) for row in answered]
    metrics["mrr_at_10"] = statistics.mean(ranks) if ranks else None
    return metrics


def quality_metrics(tool: str, rows: list[dict], labels: dict, queries: list[dict]) -> dict:
    metrics = score_rows(rows, labels, queries)
    if "session_qrels" in labels:
        metrics["session_hits"] = score_rows(rows, {"qrels": labels["session_qrels"]}, queries)
    if tool == "auxly":
        metrics = {"all_queries": metrics["all_queries"], "answered_queries": metrics["answered_queries"], "returned_subset_hit": metrics["hit_at_10"], "returned_subset_document_recall": metrics["document_recall_at_10"], "rank_metrics": None, "scope": "first ten distinct matches in native arbitrary Go map order; no ranked-retrieval claim"}
    return metrics


def git_blob(commit: str, relative: str) -> bytes:
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or any(part in {"", ".", ".."} for part in relative.split("/")) or "\\" in relative or ":" in relative:
        raise ComparisonError("InvalidQualificationSourceIdentity")
    return subprocess.check_output(["git", "show", f"{commit}:{relative}"], cwd=REPO)


def source_equivalence(relative: str, expected: str, commit: str, head: str) -> str:
    """Prove exact Git content and either deterministic LF or CRLF checkout."""
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ComparisonError("InvalidQualificationSourceDigest")
    pinned_blob, current_blob = git_blob(commit, relative), git_blob(head, relative)
    pinned_lf = pinned_blob.replace(b"\r\n", b"\n")
    if pinned_blob != current_blob:
        raise ComparisonError("QualificationGitBlobMismatch")
    variants = {hashlib.sha256(value).hexdigest() for value in (pinned_blob, pinned_lf, pinned_lf.replace(b"\n", b"\r\n"))}
    current = digest(checked(REPO / relative, REPO, exists=True))
    if expected not in variants or current not in variants:
        raise ComparisonError("QualificationSourceMismatch")
    return current


def normalized_distribution(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def frozen_constraints(path: Path) -> dict[str, str]:
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+!-]+)", line)
        if not match:
            raise ComparisonError("InvalidQualifiedDependencyConstraint")
        name, version = normalized_distribution(match[1]), match[2]
        if name in result:
            raise ComparisonError("DuplicateQualifiedDependencyConstraint")
        result[name] = version
    if not result:
        raise ComparisonError("MissingQualifiedDependencyConstraints")
    return result


def dependency_report(evidence: dict, installed: dict[str, str]) -> dict:
    qualified = evidence["primary"]["dependency_constraints"]
    current = {normalized_distribution(name): version for name, version in installed.items()}
    required = {normalized_distribution(name): version for name, version in evidence["primary"]["runtime"]["packages"].items()}
    shared = {name: {"qualified": qualified[name], "installed": current[name]} for name in sorted(set(current) & set(qualified))}
    missing = sorted(set(required) - set(current))
    mismatch = sorted(name for name, pair in shared.items() if pair["qualified"] != pair["installed"])
    return {"compatible": not missing and not mismatch, "shared_packages": shared, "missing_native_packages": missing, "version_mismatches": mismatch, "unqualified_packages": {name: version for name, version in current.items() if name not in qualified}, "scope": "shared distribution versions match primary frozen constraints; Python, OS and platform wheel bytes differ and are separately reported"}


def child_snapshot(interpreter: Path) -> dict:
    """Inspect the separate official environment without loading a model."""
    code = "import importlib.metadata,json,platform,sys; print(json.dumps({'packages':{d.metadata['Name']:d.version for d in importlib.metadata.distributions()},'python_version':sys.version.split()[0],'platform':sys.platform,'machine':platform.machine(),'abi':sys.implementation.cache_tag}))"
    result = subprocess.run([str(interpreter), "-c", code], capture_output=True, timeout=30)
    if result.returncode or len(result.stdout) > 1024 * 1024:
        raise ComparisonError("ChildRuntimeInspectionFailed")
    try:
        snapshot = json.loads(result.stdout)
        snapshot["packages"] = {normalized_distribution(name): version for name, version in snapshot["packages"].items()}
    except (ValueError, KeyError, TypeError) as exc:
        raise ComparisonError("InvalidChildRuntimeInventory") from exc
    return snapshot


def child_encoder_versions(tool: str, qualification: dict) -> dict[str, str]:
    # The official MCP2 server cannot share Mesh's MCP1 environment.
    names = ("numpy", "onnxruntime", "tokenizers", "psutil") if tool == "mem0" else ("psutil",)
    qualified = {normalized_distribution(name): version for name, version in qualification["primary"]["runtime"]["packages"].items()}
    if any(name not in qualified for name in names):
        raise ComparisonError("MissingQualifiedEncoderVersion")
    return {name: qualified[name] for name in names}


def verify_child_inventory(report: dict, installed: dict[str, str], python_version: str) -> None:
    current = {normalized_distribution(name): version for name, version in installed.items()}
    if current != report.get("packages") or python_version != report.get("python_version"):
        raise ComparisonError("ChildEnvironmentChanged")
    tool = report.get("tool")
    if tool not in ("mem0", "basic-memory") or current.get(normalized_distribution(PINS[tool]["distribution"])) != PINS[tool]["version"]:
        raise ComparisonError("CompetitorVersionMismatch")
    if any(current.get(name) != version for name, version in report.get("encoder_versions", {}).items()):
        raise ComparisonError("ChildEncoderVersionMismatch")
    if tool == "mem0" and current.get("fastembed") != FASTEMBED_VERSION:
        raise ComparisonError("FastembedVersionMismatch")
    if tool == "basic-memory" and "sqlite-vec" in current:
        raise ComparisonError("KeywordProfileHasVectorRuntime")


def verify_child_files(report: dict) -> Path:
    if report.get("protocol") != "comparison-child-v1" or report.get("wheel_sha256") != PINS[report["tool"]]["sha256"]:
        raise ComparisonError("InvalidChildEnvironmentPin")
    for relative, pin in report["files"].items():
        if digest(checked(REPO / relative, REPO / ".work", exists=True)) != pin:
            raise ComparisonError("ChildEnvironmentFilesChanged")
    interpreter = checked(REPO / report["interpreter"], REPO / ".work", exists=True)
    if report["interpreter"] not in report["files"]:
        raise ComparisonError("MissingChildInterpreterPin")
    return interpreter


def freeze_child(args) -> None:
    directory = checked(args.directory, REPO / ".work", exists=True)
    interpreter = checked(directory / "bin/python", directory, exists=True)
    if digest(checked(args.wheel, REPO / ".work", exists=True)) != PINS[args.tool]["sha256"]:
        raise ComparisonError("CompetitorDistributionDigestMismatch")
    qualification = json.loads(checked(args.summary, REPO / ".work", exists=True).read_text(encoding="utf-8"))["qualification"]
    check = subprocess.run([str(interpreter), "-m", "pip", "check"], capture_output=True, timeout=120)
    if check.returncode:
        raise ComparisonError("ChildDependencyConsistencyFailed")
    profile_check = "passed"
    if args.tool == "basic-memory":
        # List the deliberately removed optional native distribution first.
        listing = subprocess.run([str(interpreter), "-m", "pip", "show", "--files", "sqlite-vec"], capture_output=True, timeout=30)
        if listing.returncode:
            raise ComparisonError("MissingKeywordProfileVectorDistribution")
        (directory / "removed-vector-files-private.txt").write_bytes(listing.stdout)
        removed = subprocess.run([str(interpreter), "-m", "pip", "uninstall", "-y", "sqlite-vec"], capture_output=True, timeout=120)
        if removed.returncode:
            raise ComparisonError("KeywordProfileRemovalFailed")
        check = subprocess.run([str(interpreter), "-m", "pip", "check"], capture_output=True, timeout=120)
        expected = f"basic-memory {PINS[args.tool]['version']} requires sqlite-vec, which is not installed."
        if check.returncode != 1 or check.stdout.decode("utf-8").strip() != expected:
            raise ComparisonError("UnexpectedKeywordProfileDependencyFailure")
        profile_check = "only_declared_sqlite_vec_absence"
    snapshot = child_snapshot(interpreter)
    files = {interpreter.relative_to(REPO).as_posix(): digest(interpreter)}
    if args.tool == "basic-memory":
        executable = checked(directory / "bin/basic-memory", directory, exists=True)
        files[executable.relative_to(REPO).as_posix()] = digest(executable)
        # Exercise the client import boundary of the actual MCP2 environment.
        code = "from mcp import ClientSession,StdioServerParameters; from mcp.client.stdio import stdio_client"
        imported = subprocess.run([str(interpreter), "-c", code], capture_output=True, timeout=30)
        if imported.returncode:
            raise ComparisonError("ChildMcpProtocolUnavailable")
    report = snapshot | {"protocol": "comparison-child-v1", "tool": args.tool, "interpreter": interpreter.relative_to(REPO).as_posix(), "files": files, "wheel_sha256": PINS[args.tool]["sha256"], "encoder_versions": child_encoder_versions(args.tool, qualification), "dependency_consistency": {"before_profile": "passed", "after_profile": profile_check}, "scope": "official competitor in its separately frozen supported environment; Mesh host source and dependency qualification remains separate"}
    verify_child_inventory(report, snapshot["packages"], snapshot["python_version"])
    write_json(directory / "environment.json", report)
    summary = json.loads(args.summary.read_text(encoding="utf-8"))
    summary["child_environment"] = report
    write_json(args.summary, summary)


def verify_child_environment(directory: Path, qualification: dict, tool: str) -> dict:
    directory = checked(directory, REPO / ".work", exists=True)
    report = json.loads(checked(directory / "environment.json", directory, exists=True).read_text(encoding="utf-8"))
    if report.get("tool") != tool or report.get("encoder_versions") != child_encoder_versions(tool, qualification):
        raise ComparisonError("ChildEnvironmentSelectionMismatch")
    expected = "only_declared_sqlite_vec_absence" if tool == "basic-memory" else "passed"
    if report.get("dependency_consistency") != {"before_profile": "passed", "after_profile": expected}:
        raise ComparisonError("ChildDependencyConsistencyFailed")
    interpreter = verify_child_files(report)
    snapshot = child_snapshot(interpreter)
    verify_child_inventory(report, snapshot["packages"], snapshot["python_version"])
    if any(snapshot.get(key) != report.get(key) for key in ("platform", "machine", "abi")):
        raise ComparisonError("ChildRuntimeIdentityChanged")
    return report


def verify_qualification(primary: Path, floor: Path, commit: str) -> dict:
    """Bind complete source qualification and both frozen dependency manifests."""
    sys.path.insert(0, str(REPO / "benchmarks"))
    from production_runner import qualified_junit
    evidence, bindings, current_files = {}, {}, {}
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ComparisonError("InvalidQualificationSourceIdentity")

    def bind_source(relative, expected):
        current = source_equivalence(relative, expected, commit, head)
        if relative in current_files and current_files[relative] != current:
            raise ComparisonError("QualificationEvidenceChangedDuringVerification")
        current_files[relative] = current

    for name, directory in (("primary", primary), ("floor", floor)):
        directory = checked(directory, REPO / ".work", exists=True)
        source_path = checked(directory / "source-runtime.json", directory, exists=True)
        source_bytes = source_path.read_bytes()
        bindings[source_path] = hashlib.sha256(source_bytes).hexdigest()
        source = json.loads(source_bytes)
        constraints_path = checked(directory / "constraints.txt", directory, exists=True)
        bindings[constraints_path] = digest(constraints_path)
        constraints = frozen_constraints(constraints_path)
        runtime = source.get("runtime")
        if not isinstance(runtime, dict) or not isinstance(runtime.get("packages"), dict) or not runtime["packages"]:
            raise ComparisonError("MissingQualifiedRuntimeMetadata")
        if any(not isinstance(package, str) or not isinstance(version, str) or constraints.get(normalized_distribution(package)) != version for package, version in runtime["packages"].items()):
            raise ComparisonError("QualifiedRuntimeConstraintMismatch")
        runtime_sources = source["runtime_sources"]
        if set(runtime_sources) != {path.relative_to(REPO).as_posix() for path in (REPO / "mesh").rglob("*.py")} | {"pyproject.toml"}:
            raise ComparisonError("QualificationSourceSelectionMismatch")
        for relative, expected in runtime_sources.items():
            bind_source(relative, expected)
        if set(source["harness_sources"]) != QUALIFICATION_HARNESS:
            raise ComparisonError("QualificationHarnessSelectionMismatch")
        for relative, expected in source["harness_sources"].items():
            if not re.fullmatch(r"benchmarks/[A-Za-z0-9_]+\.py", relative):
                raise ComparisonError("InvalidQualificationHarnessIdentity")
            bind_source(relative, expected)
        for key, relative in {"launcher_sha256": "scripts/run_tests.py", "controller_sha256": "benchmarks/production_test_speed.py"}.items():
            bind_source(relative, source["test_execution_sources"][key])
        junit = checked(directory / "full.xml", directory, exists=True)
        bindings[junit] = digest(junit)
        tests = qualified_junit(junit, require_recovery=name == "primary")
        evidence[name] = tests | {"sha256": bindings[junit], "source_manifest_sha256": bindings[source_path], "runtime": runtime, "dependency_constraints": constraints, "constraints_sha256": bindings[constraints_path]}
    coverage_path = checked(primary / "coverage.xml", primary, exists=True)
    package_path = checked(primary / "package-final-validation.json", primary, exists=True)
    coverage_bytes, package_bytes = coverage_path.read_bytes(), package_path.read_bytes()
    bindings[coverage_path] = hashlib.sha256(coverage_bytes).hexdigest()
    bindings[package_path] = hashlib.sha256(package_bytes).hexdigest()
    coverage = float(ET.fromstring(coverage_bytes).get("line-rate", "nan"))
    package = json.loads(package_bytes)
    if not math.isfinite(coverage) or coverage < .60 or package.get("status") != "passed":
        raise ComparisonError("QualificationCoverageOrPackageFailure")
    if any(digest(path) != expected for path, expected in bindings.items()) or any(digest(checked(REPO / relative, REPO, exists=True)) != expected for relative, expected in current_files.items()):
        raise ComparisonError("QualificationEvidenceChangedDuringVerification")
    artifacts = {f"{name}/{path.name}": expected for name, directory in (("primary", primary), ("floor", floor)) for path, expected in bindings.items() if path.parent == directory.resolve()}
    evidence.update(coverage=coverage, coverage_sha256=bindings[coverage_path], package_report_sha256=bindings[package_path], same_source=True, same_native_dependencies=False, qualification_source_commit=commit, comparison_source_commit=head, source_equivalence="identical pinned/current Git blobs; artifact and current hashes match exact LF/CRLF checkout variants", current_source_files=current_files, artifact_files=artifacts)
    return evidence


def verify_qualification_artifacts(evidence: dict, primary: Path, floor: Path) -> None:
    directories = {"primary": primary, "floor": floor}
    for relative, expected in evidence["artifact_files"].items():
        kind, name = relative.split("/")
        path = checked(directories[kind] / name, directories[kind], exists=True)
        if digest(path) != expected:
            raise ComparisonError("QualificationEvidenceChangedDuringMeasurement")


def summarize_latency(rows: list[dict]) -> dict:
    values = sorted(row["latency_ms"] for row in rows)
    if not values or any(not math.isfinite(value) or value < 0 for value in values):
        raise ComparisonError("MissingLatencyObservations")
    def percentile(p):
        index = (len(values) - 1) * p
        lower, upper = math.floor(index), math.ceil(index)
        return values[lower] + (values[upper] - values[lower]) * (index - lower)
    return {"calls": len(values), "cold_first_ms": rows[0]["latency_ms"], "mean_ms": statistics.mean(values), "p50_ms": percentile(.5), "p95_ms": percentile(.95), "p99_ms": percentile(.99), "scope": "single sequential pass, all calls including first; SDK and MCP are separate interface strata"}


def initial_summary(tool: str, dataset: str) -> dict:
    return {"protocol": "comparison-v1", "status": "not_run", "tool": tool, "dataset": dataset, "tool_pin": PINS.get(tool), "profile": PROFILES[tool], "claims": {"generated_answer_accuracy": False, "general_superiority": False, "full_LongMemEval": False}, "labels_in_ranking_inputs": False, "quality": None, "resources": None}


def run(args) -> int:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise ComparisonError("NativeComparisonRequiresGitHubActions")
    out = checked(args.output, REPO / ".work")
    out.mkdir(parents=True, exist_ok=True)
    summary = initial_summary(args.tool, args.dataset)
    write_json(out / "summary.json", summary)
    if (out / "ranked.jsonl").exists() or (out / "progress.json").exists():
        raise ComparisonError("ExistingMeasurementArtifacts")
    manifest, _, queries = fixture(args.data)
    if manifest["name"] != args.dataset:
        raise ComparisonError("DatasetSelectionMismatch")
    import psutil
    summary["qualification"] = verify_qualification(args.qualification_directory, args.floor_qualification_directory, args.qualification_commit)
    installed = {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()}
    summary["packages"] = installed
    summary["dependencies"] = dependency_report(summary["qualification"], installed)
    write_json(out / "summary.json", summary)
    if not summary["dependencies"]["compatible"]:
        raise ComparisonError("SharedDependencyVersionMismatch")
    interpreter = None
    if args.tool in ("mem0", "basic-memory"):
        summary["child_environment"] = verify_child_environment(args.competitor_environment, summary["qualification"], args.tool)
        interpreter = verify_child_files(summary["child_environment"])
        write_json(out / "summary.json", summary)
    if args.tool == "mem0":
        summary["bm25_assets"] = verify_bm25(checked(args.bm25, exists=True))
        summary["fastembed_version"] = summary["child_environment"]["packages"]["fastembed"]
        if summary["fastembed_version"] != FASTEMBED_VERSION:
            raise ComparisonError("FastembedVersionMismatch")
    sources = {path.relative_to(REPO).as_posix(): digest(path) for path in sorted((REPO / "mesh").rglob("*.py"))}
    sources["benchmarks/competitor_comparison.py"] = digest(Path(__file__))
    sources["pyproject.toml"] = digest(REPO / "pyproject.toml")
    sources.update(summary["qualification"]["current_source_files"])
    for relative in ("benchmarks/prepare_retrieval_ci.py", "benchmarks/provision_production_model.py", ".github/workflows/competitor-comparison.yml"):
        sources[relative] = digest(REPO / relative)
    summary.update(status="running", started_at_utc=datetime.now(timezone.utc).isoformat(), source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(), source_files=sources, dataset_manifest_sha256=digest(args.data / "manifest.json"), dataset_scope=manifest.get("scope", "full published BEIR test query selection"), corpus_count=manifest["corpus_count"], query_count=manifest["query_count"], model_files={name: MODEL[name] for name in ("model.onnx", "tokenizer.json")}, model_revision=MODEL["revision"], system={"python": sys.version.split()[0], "platform": sys.platform, "logical_cpus": os.cpu_count(), "available_ram_bytes": psutil.virtual_memory().available}, limits={"wall_seconds": 2700, "tree_rss_bytes": 4 * 1024 ** 3, "sample_seconds": .05}, network_isolation="disconnected Linux network namespace inherited by all children", input_body_normalization="strip surrounding whitespace for every tool; title newline body for public BEIR parents")
    summary["dataset_manifest"] = manifest
    write_json(out / "summary.json", summary)
    peak, cpu, failure = 0, 0.0, None
    started = time.monotonic()
    cleanup = {"settled": True}
    with owned_temporary_root(out, cleanup) as home:
        config = {"tool": args.tool, "data": str(checked(args.data, exists=True)), "models": str(checked(args.models, exists=True)), "output": str(out), "home": str(home), "qualification": summary["qualification"]}
        if interpreter is not None:
            config["child_environment"] = summary["child_environment"]
        if args.tool == "mem0":
            config["bm25"] = str(checked(args.bm25, exists=True))
        config_path = home / "config.json"
        write_json(config_path, config)
        env = isolated_environment(home)
        if interpreter is not None:
            env["PATH"] = str(interpreter.parent) + os.pathsep + env.get("PATH", "")
        if args.tool == "auxly":
            env["COMPARISON_AUXLY"] = str(checked(args.auxly, exists=True))
        with (out / "worker-private.log").open("wb") as log:
            process = subprocess.Popen(native_command(config_path, interpreter), cwd=home, env=env, stdout=log, stderr=log, start_new_session=True)
            cleanup["settled"] = False
            observed_cpu = {}
            owned_processes = {}
            try:
                while process.poll() is None:
                    try:
                        owner = psutil.Process(process.pid)
                        tree = [owner, *owner.children(recursive=True)]
                        rss = 0
                        for child in tree:
                            try:
                                rss += child.memory_info().rss
                                times = child.cpu_times()
                                observed_cpu[(child.pid, child.create_time())] = times.user + times.system
                                owned_processes[(child.pid, child.create_time())] = os.getpgid(child.pid)
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                pass
                        peak = max(peak, rss)
                    except psutil.NoSuchProcess:
                        pass
                    if time.monotonic() - started > 2700 or peak > 4 * 1024 ** 3:
                        failure = "ResourceBudgetExceeded"
                        break
                    time.sleep(.05)
            finally:
                # The process group persists if its leader exits before an MCP child.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                if process.poll() is None:
                    try:
                        children = psutil.Process(process.pid).children(recursive=True)
                    except psutil.NoSuchProcess:
                        children = []
                    for child in reversed(children):
                        try:
                            child.kill()
                        except psutil.NoSuchProcess:
                            pass
                    process.kill()
                process.wait(timeout=30)
                if not stop_observed_processes(psutil, owned_processes):
                    raise ComparisonError("OwnedProcessCleanupFailed")
                cleanup["settled"] = True
                cpu = sum(observed_cpu.values())
            if process.returncode and failure is None:
                failure = "WorkerFailed"
        summary["resources"] = {"sampled_tree_peak_rss_bytes": peak, "sampled_tree_cpu_seconds": cpu, "elapsed_seconds": time.monotonic() - started, "scope": "sampled worker plus child processes; peaks can be missed and shared pages counted twice"}
    summary["status"] = "failed" if failure else "complete"
    summary["cleanup"] = cleanup
    summary["failure_code"] = failure
    worker_diagnostic = out / "worker-failure.json"
    if worker_diagnostic.exists():
        summary["worker_failure"] = checked_worker_failure(json.loads(checked(worker_diagnostic, out, exists=True).read_text(encoding="utf-8")))
    progress = out / "progress.json"
    if progress.exists():
        summary["accounting"] = json.loads(progress.read_text(encoding="utf-8"))
    write_json(out / "summary.json", summary)
    if failure is None:
        verify_qualification_artifacts(summary["qualification"], args.qualification_directory, args.floor_qualification_directory)
        rows = read_jsonl(out / "ranked.jsonl")
        # Qrels become visible only after the worker tree stopped and export closed.
        labels = json.loads((args.data / "labels.json").read_text(encoding="utf-8"))
        summary["ranked_export_sha256"] = digest(out / "ranked.jsonl")
        summary["labels_sha256"] = digest(args.data / "labels.json")
        summary["quality"] = quality_metrics(args.tool, rows, labels, queries)
        summary["latency"] = summarize_latency(rows)
        if any(digest(REPO / relative) != pin for relative, pin in sources.items()):
            raise ComparisonError("SourceChangedDuringMeasurement")
        if digest(args.data / "manifest.json") != summary["dataset_manifest_sha256"] or fixture(args.data)[0] != manifest:
            raise ComparisonError("FixtureChangedDuringMeasurement")
        final_packages = {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()}
        if final_packages != installed:
            raise ComparisonError("DependenciesChangedDuringMeasurement")
        if interpreter is not None and verify_child_environment(args.competitor_environment, summary["qualification"], args.tool) != summary["child_environment"]:
            raise ComparisonError("ChildEnvironmentChangedDuringMeasurement")
        if args.tool == "mem0" and verify_bm25(args.bm25) != summary["bm25_assets"]:
            raise ComparisonError("Bm25AssetsChangedDuringMeasurement")
    summary["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    write_json(out / "summary.json", summary)
    print(json.dumps({"tool": args.tool, "dataset": args.dataset, "status": summary["status"], "failure_code": failure}))
    return int(bool(failure))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "run"):
        item = sub.add_parser(name)
        item.add_argument("--tool", choices=TOOLS, required=True)
        item.add_argument("--dataset", choices=DATASETS, required=True)
        item.add_argument("--output", type=Path, required=True)
        if name == "run":
            item.add_argument("--data", type=Path, required=True)
            item.add_argument("--models", type=Path, required=True)
            item.add_argument("--auxly", type=Path)
            item.add_argument("--bm25", type=Path, help="Verified static BM25 config and stopword directory; required for Mem0")
            item.add_argument("--qualification-directory", type=Path, required=True)
            item.add_argument("--floor-qualification-directory", type=Path, required=True)
            item.add_argument("--qualification-commit", default=QUALIFICATION_COMMIT, help="Exact Git commit that supplied source qualification; LF/CRLF checkout conversion is verified against its blobs")
            item.add_argument("--competitor-environment", type=Path, help="Separately frozen official Mem0 or Basic Memory virtual environment")
    item = sub.add_parser("worker")
    item.add_argument("--config", type=Path, required=True)
    item = sub.add_parser("provision-bm25", help="Fetch and verify only pinned static BM25 configuration and stopwords")
    item.add_argument("--directory", type=Path, required=True)
    item = sub.add_parser("freeze-child", help="Check and freeze an official competitor environment separately from the qualified Mesh host")
    item.add_argument("--tool", choices=("mem0", "basic-memory"), required=True)
    item.add_argument("--directory", type=Path, required=True)
    item.add_argument("--wheel", type=Path, required=True)
    item.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "init":
        out = checked(args.output, REPO / ".work")
        out.mkdir(parents=True, exist_ok=True)
        write_json(out / "summary.json", initial_summary(args.tool, args.dataset))
        return 0
    if args.command == "worker":
        config = json.loads(checked(args.config, exists=True).read_text(encoding="utf-8"))
        try:
            return worker(config)
        except BaseException as error:
            destination = checked(Path(config["output"]) / "worker-failure.json", REPO / ".work")
            write_json(destination, worker_failure_projection(error))
            return 1
    if args.command == "provision-bm25":
        provision_bm25(args.directory)
        return 0
    if args.command == "freeze-child":
        freeze_child(args)
        return 0
    try:
        return run(args)
    except Exception as exc:
        existing = checked(args.output, REPO / ".work") / "summary.json"
        summary = json.loads(existing.read_text(encoding="utf-8")) if existing.exists() else initial_summary(args.tool, args.dataset)
        summary.update(status="failed", failure_code=str(exc) if isinstance(exc, ComparisonError) else type(exc).__name__)
        summary["quality"] = None
        write_json(checked(args.output, REPO / ".work") / "summary.json", summary)
        print(json.dumps({"status": "failed", "failure_code": summary["failure_code"]}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

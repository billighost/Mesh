"""Tiny mocked C4 contracts; no model, corpus, provider, or daemon is executed."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from mesh import audit, local_rerank as rerank, memory, store as storage


class Tokenizer:
    def __init__(self):
        self.pairs = []
        self.padding = False

    def no_truncation(self):
        pass

    def no_padding(self):
        self.padding = False

    def enable_padding(self, **kwargs):
        assert kwargs == {"pad_id": 0, "pad_token": "[PAD]"}
        self.padding = True

    def num_special_tokens_to_add(self, is_pair=False):
        return 3 if is_pair else 2

    def encode(self, text, add_special_tokens=False):
        assert not add_special_tokens
        offsets = [match.span() for match in re.finditer(r"\S+", text)]
        return SimpleNamespace(ids=[1] * len(offsets), offsets=offsets)

    def encode_batch(self, pairs):
        assert self.padding
        self.pairs.extend(pairs)
        lengths = [len(self.encode(query).ids) + len(self.encode(passage).ids) + 3 for query, passage in pairs]
        width = max(lengths)
        return [SimpleNamespace(ids=[1] * length + [0] * (width - length),
                                attention_mask=[1] * length + [0] * (width - length),
                                type_ids=[0] * width) for length in lengths]


class Session:
    def __init__(self, scores=None, on_run=None):
        self.scores, self.on_run = scores, on_run
        self.calls = []

    def get_inputs(self):
        return [SimpleNamespace(name=name, type="tensor(int64)") for name in
                ("input_ids", "attention_mask", "token_type_ids")]

    def get_outputs(self):
        return [SimpleNamespace(name="logits", type="tensor(float)")]

    def get_providers(self):
        return ["CPUExecutionProvider"]

    def run(self, outputs, arrays):
        assert outputs is None
        self.calls.append(arrays)
        if self.on_run:
            self.on_run()
        count = len(arrays["input_ids"])
        scores = list(range(count)) if self.scores is None else self.scores
        return [np.asarray(scores, dtype=np.float32).reshape(-1, 1)]


def encoder(scores=None, on_run=None):
    engine = object.__new__(rerank._LocalCrossEncoder)
    engine.np, engine.tokenizer, engine.session = np, Tokenizer(), Session(scores, on_run)
    engine.names = {"input_ids", "attention_mask", "token_type_ids"}
    return engine


def row(index=0, content="Original source evidence."):
    return {"id": f"parent-{index}", "content": content, "revision": 3,
            "incarnation": "a" * 32, "store_generation": "b" * 32,
            "rank_score": 1.0 - index * 0.01, "similarity": 0.9,
            "supporting_spans": [], "selected_passage_ordinal": None}


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("MESH_NO_OUTBOX_WORKER", "1")
    monkeypatch.setenv("MESH_BG_ONNX", "0")
    monkeypatch.setenv("MESH_FORCE_HASH_EMBED", "1")
    monkeypatch.setenv("MESH_RERANK_ENABLED", "0")
    monkeypatch.setattr(memory, "_trigger_background_bootstrap", lambda: None)
    monkeypatch.setattr(memory, "_ensure_idle_watchdog", lambda: None)
    monkeypatch.setattr(memory, "_get_model", lambda: (memory._hash_embed_384, "hash-fallback"))
    monkeypatch.setattr(audit, "_ensure_audit_drain_thread", lambda: None)
    rerank.unload_reranker()
    yield
    rerank.unload_reranker()
    audit.stop_audit_worker()
    audit._close_all_audit_connections()
    storage._close_all_pooled_connections()
    storage._VECTOR_CACHE.clear()
    storage._CACHE_REJECTIONS.clear()
    memory._REEMBED_STORES.clear()


@pytest.fixture
def assets(tmp_path, monkeypatch):
    directory = tmp_path / "offline-reranker"
    directory.mkdir()
    metadata = dict(rerank.MODEL)
    for kind, body in (("model", b"fake quantized model"), ("tokenizer", b"fake tokenizer")):
        (directory / metadata[kind + "_file"]).write_bytes(body)
        metadata[kind + "_bytes"] = len(body)
        metadata[kind + "_sha256"] = hashlib.sha256(body).hexdigest()
    monkeypatch.setattr(rerank, "MODEL", metadata)
    monkeypatch.setenv("MESH_RERANK_MODEL_DIR", str(directory))
    return directory


def seed(mesh, count=5, **metadata):
    vector = np.zeros(384, dtype=np.float32)
    vector[0] = 1.0
    for index in range(count):
        mesh.store.upsert(f"item-{index}", f"Anchor record {index} is available.", vector,
                          {"embedding_model": "hash-384", "source_agent": "test-agent", **metadata})
    return vector


def recall(mesh, vector, **kwargs):
    return mesh.recall("Anchor", record_access=False, _precomputed_embedding=vector.tolist(),
                       _precomputed_model_tag="hash-384", **kwargs)


def test_module_and_offline_verifier_import_no_model_runtime(assets):
    code = ("import json, sys; import mesh.local_rerank; "
            "print(json.dumps([name for name in ('numpy','onnxruntime','tokenizers') if name in sys.modules]))")
    result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == []
    rerank.verify_model_files(assets)


def test_default_off_and_forced_hash_leave_exact_base_results(monkeypatch):
    mesh = memory.Mesh(namespace="rerank-off")
    vector = seed(mesh)
    monkeypatch.setattr(rerank, "_get_engine", lambda: pytest.fail("default off loaded a reranker"))
    first = recall(mesh, vector, n=1)
    monkeypatch.delenv("MESH_RERANK_ENABLED")
    assert recall(mesh, vector, n=1) == first
    assert first[0]["id"] == "item-0"
    assert not any(key.startswith("rerank_") for key in first[0])


def test_enabled_without_local_configuration_keeps_base_order(monkeypatch):
    monkeypatch.delenv("MESH_RERANK_MODEL_DIR", raising=False)
    result = rerank.rerank("query", [row(0), row(1)], revalidate=lambda rows: rows)
    assert [item["id"] for item in result] == ["parent-0", "parent-1"]
    assert all(item["rerank_status"] == "model_not_configured" for item in result)


def test_enabled_n1_scores_four_and_preserves_original_identity_thresholds_and_tail(monkeypatch):
    mesh = memory.Mesh(namespace="rerank-n1")
    vector = seed(mesh)
    baseline = recall(mesh, vector, n=5, min_similarity=0.99)
    engine = encoder(scores=[-3.0, 2.0, 2.0, 7.0])
    monkeypatch.setattr(rerank, "_get_engine", lambda: engine)
    monkeypatch.setenv("MESH_RERANK_ENABLED", "1")
    first = recall(mesh, vector, n=1, min_similarity=0.99)
    assert first[0]["id"] == "item-3"
    assert len(engine.session.calls[0]["input_ids"]) == 4
    result = recall(mesh, vector, n=5, min_similarity=0.99)
    assert [item["id"] for item in result] == ["item-3", "item-1", "item-2", "item-0", "item-4"]
    old = {item["id"]: item for item in baseline}
    for item in result:
        assert {key: value for key, value in item.items() if not key.startswith("rerank_")} == old[item["id"]]
    assert result[-1]["rerank_status"] == "outside_parent_cap"
    assert result[0]["rerank_score"] == 7.0


def test_lazy_loader_uses_one_cpu_session_one_tokenizer_and_verified_bytes(assets, monkeypatch):
    loaded, tokenizers, options = [], [], []

    class Options:
        def __init__(self):
            self.entries = {}
            options.append(self)

        def add_session_config_entry(self, key, value):
            self.entries[key] = value

    def load(body, sess_options, providers):
        assert body == (assets / "model.onnx").read_bytes()
        assert providers == ["CPUExecutionProvider"]
        loaded.append(body)
        return Session()

    def tokenize(body):
        assert body == "fake tokenizer"
        tokenizers.append(body)
        return Tokenizer()

    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(
        SessionOptions=Options, InferenceSession=load,
        ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL="sequential"),
        GraphOptimizationLevel=SimpleNamespace(ORT_ENABLE_ALL="all")))
    monkeypatch.setitem(sys.modules, "tokenizers", SimpleNamespace(Tokenizer=SimpleNamespace(from_str=tokenize)))
    first = rerank._get_engine()
    assert rerank._get_engine() is first
    assert len(loaded) == len(tokenizers) == len(options) == 1
    assert options[0].intra_op_num_threads == options[0].inter_op_num_threads == 1
    assert options[0].execution_mode == "sequential"
    assert options[0].entries == {"session.intra_op.allow_spinning": "0", "session.inter_op.allow_spinning": "0"}


@pytest.mark.parametrize("failure", ["missing", "hash", "size", "linked"])
def test_missing_untrusted_or_linked_assets_never_load_a_session(assets, monkeypatch, failure):
    if failure == "missing":
        monkeypatch.setenv("MESH_RERANK_MODEL_DIR", str(assets / "missing"))
    elif failure == "hash":
        (assets / "model.onnx").write_bytes(b"x" * rerank.MODEL["model_bytes"])
    elif failure == "size":
        (assets / "model.onnx").write_bytes(b"x")
    else:
        original = Path.lstat

        def reparse(path):
            metadata = original(path)
            if path == assets:
                return SimpleNamespace(st_mode=metadata.st_mode, st_file_attributes=0x400)
            return metadata

        monkeypatch.setattr(Path, "lstat", reparse)
    monkeypatch.setattr(rerank, "_LocalCrossEncoder", lambda *args: pytest.fail("unverified model loaded"))
    result = rerank.rerank("query", [row(0), row(1)], revalidate=lambda rows: rows)
    assert [item["id"] for item in result] == ["parent-0", "parent-1"]
    assert result[0]["rerank_status"] == {"missing": "model_unavailable", "hash": "model_hash_mismatch",
                                          "size": "model_size_mismatch", "linked": "model_path_linked"}[failure]


def test_short_original_parent_prefix_has_explicit_original_range_and_no_silent_pair_truncation():
    engine = encoder()
    body = " ".join(f"word{index}" for index in range(250))
    query = " ".join(f"query{index}" for index in range(40))
    candidate = row(content=body)
    candidate["supporting_spans"] = [{"text": "a base retrieval excerpt"}]
    arrays, accounting = engine.prepare(query, [candidate])
    prefix = " ".join(f"word{index}" for index in range(224))
    assert engine.tokenizer.pairs == [(" ".join(f"query{index}" for index in range(29)), prefix)]
    assert arrays["input_ids"].shape == (1, 256)
    assert accounting[0] == {"source": "accepted_original_body", "start": 0, "end": len(prefix),
                             "query_tokens": 40, "kept_query_tokens": 29,
                             "removed_query_characters": len(query) - len(engine.tokenizer.pairs[0][0]),
                             "passage_tokens": 250, "kept_passage_tokens": 224,
                             "removed_passage_characters": len(body) - len(prefix), "pair_tokens": 256}
    assert candidate["supporting_spans"] == [{"text": "a base retrieval excerpt"}]


@pytest.mark.parametrize("change", ["identity", "bounds", "text", "oversized"])
def test_selected_passage_identity_offsets_bytes_and_token_limit_are_checked(monkeypatch, change):
    engine = encoder()
    monkeypatch.setattr(rerank, "_get_engine", lambda: engine)
    text = " ".join(["evidence"] * (225 if change == "oversized" else 3))
    candidate = row(content="Preamble. " + text + " Tail.")
    candidate["selected_passage_ordinal"] = 2
    span = {key: candidate[key] for key in ("revision", "incarnation", "store_generation")}
    span.update(memory_id=candidate["id"], start=10, end=10 + len(text), text=text)
    candidate["supporting_spans"] = [span]
    if change == "identity":
        span["incarnation"] = "c" * 32
    elif change == "bounds":
        span["end"] += 100
    elif change == "text":
        span["text"] = "Different evidence."
    result = rerank.rerank("query", [candidate, row(1)], revalidate=lambda rows: rows)
    assert not engine.session.calls
    assert result[0]["rerank_status"] == ("selected_span_too_long" if change == "oversized" else "selected_span_invalid")


def test_selected_passage_scores_only_its_verbatim_source_span():
    engine = encoder()
    candidate = row(content="Preamble. Selected evidence. Tail.")
    candidate["selected_passage_ordinal"] = 2
    span = {key: candidate[key] for key in ("revision", "incarnation", "store_generation")}
    span.update(memory_id=candidate["id"], start=10, end=28, text="Selected evidence.")
    candidate["supporting_spans"] = [span]
    _, accounting = engine.prepare("query", [candidate])
    assert engine.tokenizer.pairs == [("query", "Selected evidence.")]
    assert accounting[0]["source"] == "exact_selected_source_span"
    assert (accounting[0]["start"], accounting[0]["end"]) == (10, 28)
    assert accounting[0]["removed_passage_characters"] == 0


def test_private_lock_and_quarantine_are_enforced_before_tokenization(monkeypatch):
    mesh = memory.Mesh(namespace="rerank-public")
    vector = seed(mesh, count=3)
    mesh.store.upsert("private", "Anchor private evidence.", vector,
                      {"embedding_model": "hash-384", "local_only": True})
    mesh.store.upsert("quarantined", "Anchor quarantined evidence.", vector,
                      {"embedding_model": "hash-384", "quarantined": True})
    engine = encoder()
    monkeypatch.setattr(rerank, "_get_engine", lambda: engine)
    monkeypatch.setenv("MESH_RERANK_ENABLED", "1")
    token = memory._PRIVATE_ACCESS_OVERRIDE.set(False)
    try:
        result = recall(mesh, vector, n=5, include_private=True)
    finally:
        memory._PRIVATE_ACCESS_OVERRIDE.reset(token)
    assert {item["id"] for item in result} == {"item-0", "item-1", "item-2"}
    assert len(engine.tokenizer.pairs) == 3
    assert all("private" not in passage and "quarantined" not in passage for _, passage in engine.tokenizer.pairs)


def test_candidate_becoming_private_during_tokenization_prevents_inference(monkeypatch):
    mesh = memory.Mesh(namespace="rerank-preflight")
    vector = seed(mesh, count=2)
    engine = encoder()
    original = engine.prepare

    def prepare(query, candidates):
        prepared = original(query, candidates)
        mesh.store.upsert("item-0", "Anchor record 0 is available.", vector,
                          {"embedding_model": "hash-384", "local_only": True})
        return prepared

    engine.prepare = prepare
    monkeypatch.setattr(rerank, "_get_engine", lambda: engine)
    monkeypatch.setenv("MESH_RERANK_ENABLED", "1")
    result = recall(mesh, vector, include_private=False)
    assert not engine.session.calls
    assert [item["id"] for item in result] == ["item-1"]
    assert result[0]["rerank_status"] == "candidates_changed"


@pytest.mark.parametrize("change", ["revision", "private", "quarantine", "reinsert", "generation"])
def test_changed_parent_identity_or_privacy_after_inference_is_removed(monkeypatch, change):
    mesh = memory.Mesh(namespace="rerank-postflight")
    vector = seed(mesh, count=3)

    def mutate():
        metadata = {"embedding_model": "hash-384"}
        if change == "generation":
            with mesh.store._connect() as connection:
                connection.execute("UPDATE mesh_meta SET value=? WHERE key='store_generation'", ("c" * 32,))
            return
        if change in ("private", "quarantine"):
            column = "local_only" if change == "private" else "quarantined"
            with mesh.store._connect() as connection:
                connection.execute(f"UPDATE memories SET {column}=1 WHERE id=?", ("item-2",))
            return
        if change == "reinsert":
            mesh.store.delete("item-2")
        metadata.update(local_only=change == "private", quarantined=change == "quarantine")
        mesh.store.upsert("item-2", "Anchor changed evidence.", vector, metadata)

    engine = encoder(on_run=mutate)
    monkeypatch.setattr(rerank, "_get_engine", lambda: engine)
    monkeypatch.setenv("MESH_RERANK_ENABLED", "1")
    if change == "generation":
        with pytest.raises(storage.StaleStoreError):
            recall(mesh, vector, n=3, include_private=False)
    else:
        result = recall(mesh, vector, n=3, include_private=False)
        assert [item["id"] for item in result] == ["item-1", "item-0"]
    assert len(engine.session.calls) == 1


def test_shared_busy_gate_has_no_queue_or_second_model_load(monkeypatch):
    monkeypatch.setattr(rerank, "_get_engine", lambda: pytest.fail("busy recall loaded another model"))
    assert rerank._GATE.acquire(blocking=False)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(rerank.rerank, "query", [row(0), row(1)],
                                     revalidate=lambda rows: rows[1:]).result(timeout=2)
        assert [item["id"] for item in result] == ["parent-1"]
        assert result[0]["rerank_status"] == "busy"
    finally:
        rerank._GATE.release()


@pytest.mark.parametrize("scores", [[float("nan"), 1.0], [float("inf"), 1.0], [1.0]])
def test_invalid_or_nonfinite_model_logits_retain_original_order(monkeypatch, scores):
    engine = encoder(scores=scores)
    monkeypatch.setattr(rerank, "_get_engine", lambda: engine)
    result = rerank.rerank("query", [row(0), row(1)], revalidate=lambda rows: rows)
    assert [item["id"] for item in result] == ["parent-0", "parent-1"]
    assert result[0]["rerank_status"] == "model_output_invalid"


def test_unload_releases_optional_session_without_importing_or_loading_one(monkeypatch):
    monkeypatch.setattr(rerank, "_engine", encoder())
    monkeypatch.setattr(memory, "_model", None)
    assert memory.unload_model()
    assert rerank._engine is None
    assert not memory.unload_model()


def test_rerank_access_touches_shared_idle_usage_under_hash_without_another_thread(assets, monkeypatch):
    engine = encoder()
    started = []
    clock = iter((8.0, 9.0))
    monkeypatch.setattr(memory, "_model", None)
    monkeypatch.setattr(memory, "_last_model_use_ts", 0.0)
    monkeypatch.setattr(memory, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    monkeypatch.setattr(memory, "_ensure_idle_watchdog", lambda: started.append(True))
    monkeypatch.setattr(rerank, "_LocalCrossEncoder", lambda *args: engine)
    assert rerank._get_engine() is engine
    assert memory._last_model_use_ts == 8.0
    assert rerank._get_engine() is engine
    assert memory._last_model_use_ts == 9.0
    assert len(started) == 2
    assert memory._model is None


def test_shared_idle_watchdog_unloads_reranker_when_primary_model_is_none(monkeypatch):
    class StopLoop(Exception):
        pass

    ticks = []

    def tick(seconds):
        ticks.append(seconds)
        if len(ticks) == 2:
            raise StopLoop

    monkeypatch.setattr(memory, "_model", None)
    monkeypatch.setattr(memory, "_last_model_use_ts", 1.0)
    monkeypatch.setattr(memory, "_IDLE_UNLOAD_SECONDS", 5.0)
    monkeypatch.setattr(memory, "time", SimpleNamespace(sleep=tick, monotonic=lambda: 10.0))
    monkeypatch.setattr(rerank, "_engine", encoder())
    with pytest.raises(StopLoop):
        memory._idle_watchdog_loop()
    assert not rerank.is_loaded()
    assert memory._model is None
    assert ticks == [2.0, 2.0]


def test_passage_only_backfill_keeps_resident_model_active_beyond_idle_timeout(monkeypatch):
    class StopLoop(Exception):
        pass

    clock = [190.0]
    ticks = []
    observed = []

    def tick(seconds):
        ticks.append(seconds)
        if len(ticks) % 2 == 0:
            raise StopLoop

    resident = object.__new__(memory._DirectOnnxEmbedding)
    resident.window_tokenizer = Tokenizer()

    def raw(texts, batch_size=16):
        # One synthetic raw call lasts longer than the configured idle timeout.
        clock[0] = 400.0
        with pytest.raises(StopLoop):
            memory._idle_watchdog_loop()
        observed.append((memory._model, memory._model_backend))
        return np.vstack([memory._hash_embed_384(text) for text in texts])

    resident._encode_raw_batches = raw
    monkeypatch.setenv("MESH_PASSAGE_INDEX", "1")
    monkeypatch.setattr(memory, "_model", resident)
    monkeypatch.setattr(memory, "_model_backend", "direct-onnx")
    monkeypatch.setattr(memory, "_last_model_use_ts", 1.0)
    monkeypatch.setattr(memory, "_IDLE_UNLOAD_SECONDS", 180.0)
    monkeypatch.setattr(memory, "time", SimpleNamespace(sleep=tick, monotonic=lambda: clock[0]))
    monkeypatch.setattr(memory, "_encode_texts_with_tag", lambda *args: pytest.fail("reused primary was re-encoded"))
    mesh = memory.Mesh(namespace="passage-idle-use")
    content = " ".join(f"source{index}" for index in range(300))
    mesh.store.upsert("long", content, memory._hash_embed_384(content),
                      {"embedding_model": "onnx-minilm-l6-v2", "source_agent": "test-agent"})
    job = mesh.store.list_embedding_jobs(1)[0]
    hint = memory._ReembedHint(str(mesh.store.db_path.parent), mesh.namespace, job["memory_id"],
                              job["revision"], job["target_model"], job["attempts"],
                              incarnation=job["incarnation"], store_generation=job["store_generation"],
                              target_representation=job["target_representation"])
    memory._process_reembed_batch([hint])
    assert observed == [(resident, "direct-onnx")]
    assert memory._last_model_use_ts == 400.0
    assert mesh.store.embedding_job_stats()["pending_count"] == 0
    assert mesh.store.get_indexing_status(["long"])["long"]["passage_indexing_state"] == "indexed"
    clock[0] = 581.0
    with pytest.raises(StopLoop):
        memory._idle_watchdog_loop()
    assert memory._model is None


def test_active_model_use_blocks_manual_unload_and_resets_time_even_after_error(monkeypatch):
    clock = [1.0]
    resident = object()
    monkeypatch.setattr(memory, "_model", resident)
    monkeypatch.setattr(memory, "_model_backend", "direct-onnx")
    monkeypatch.setattr(memory, "_IDLE_UNLOAD_SECONDS", 180.0)
    monkeypatch.setattr(memory, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    with pytest.raises(ValueError, match="synthetic failure"):
        with memory._using_model():
            clock[0] = 400.0
            assert not memory.unload_model()
            assert (memory._model, memory._model_backend) == (resident, "direct-onnx")
            raise ValueError("synthetic failure")
    assert memory._active_model_uses == 0
    assert memory._last_model_use_ts == 400.0
    assert not memory._unload_models(idle_only=True)
    clock[0] = 581.0
    assert memory._unload_models(idle_only=True)
    assert (memory._model, memory._model_backend) == (None, None)


def test_watchdog_rechecks_fresh_usage_after_its_initial_expiry_decision(monkeypatch):
    class StopLoop(Exception):
        pass

    ticks = []
    resident = object()
    original = memory._unload_models

    def tick(seconds):
        ticks.append(seconds)
        if len(ticks) == 2:
            raise StopLoop

    def interleave(*, idle_only):
        # The watcher decided to unload; another completed call refreshes usage.
        memory._touch_model_usage()
        assert memory._active_model_uses == 0
        return original(idle_only=idle_only)

    monkeypatch.setattr(memory, "_model", resident)
    monkeypatch.setattr(memory, "_model_backend", "direct-onnx")
    monkeypatch.setattr(memory, "_last_model_use_ts", 1.0)
    monkeypatch.setattr(memory, "_IDLE_UNLOAD_SECONDS", 5.0)
    monkeypatch.setattr(memory, "time", SimpleNamespace(sleep=tick, monotonic=lambda: 10.0))
    monkeypatch.setattr(memory, "_unload_models", interleave)
    with pytest.raises(StopLoop):
        memory._idle_watchdog_loop()
    assert (memory._model, memory._model_backend) == (resident, "direct-onnx")
    assert memory._last_model_use_ts == 10.0


def test_direct_raw_inference_has_an_active_model_lease(monkeypatch):
    clock = [1.0]
    resident = object.__new__(memory._DirectOnnxEmbedding)
    resident.input_names = {"input_ids", "attention_mask"}
    resident.tokenizer = SimpleNamespace(encode_batch=lambda texts: [
        SimpleNamespace(ids=[1, 2], attention_mask=[1, 1]) for _ in texts])

    def run(outputs, arrays):
        clock[0] = 400.0
        assert memory._active_model_uses > 0
        assert not memory.unload_model()
        assert (memory._model, memory._model_backend) == (resident, "direct-onnx")
        return [np.ones((len(arrays["input_ids"]), 384), dtype=np.float32)]

    resident.session = SimpleNamespace(run=run)
    monkeypatch.setattr(memory, "_model", resident)
    monkeypatch.setattr(memory, "_model_backend", "direct-onnx")
    monkeypatch.setattr(memory, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    vectors = resident._encode_raw_batches(["Synthetic source"])
    assert vectors.shape == (1, 384)
    assert memory._active_model_uses == 0
    assert memory._last_model_use_ts == 400.0


def test_offline_verifier_failure_is_content_free_without_traceback(assets, capsys):
    with pytest.raises(SystemExit) as error:
        rerank.main(["verify", str(assets / "missing")])
    assert error.value.code == 1
    assert capsys.readouterr().err == "model_unavailable\n"


def test_production_metadata_matches_frozen_c4_fixture():
    repository = Path(__file__).resolve().parents[1]
    namespace = {}
    exec((repository / "benchmarks" / "retrieval_c4_model.py").read_text(encoding="utf-8"), namespace)
    assert all(namespace["MODEL"][key] == value for key, value in rerank.MODEL.items())
    assert namespace["LIMITS"]["parent_cap"] == rerank.PARENT_CAP
    assert namespace["LIMITS"]["maximum_query_tokens"] == rerank.QUERY_TOKENS
    assert namespace["LIMITS"]["maximum_passage_tokens"] == rerank.PASSAGE_TOKENS
    assert namespace["LIMITS"]["maximum_pair_tokens"] == rerank.PAIR_TOKENS

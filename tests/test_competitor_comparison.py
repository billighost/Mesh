"""Offline comparison accounting and source identity regressions; no native models."""
from __future__ import annotations

import io
import json
import hashlib
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from benchmarks import competitor_comparison as comparison


def ranked(query, candidates):
    stream = io.StringIO()
    comparison.append_query(stream, {"id": query}, candidates, 1.25)
    return json.loads(stream.getvalue())


def test_public_scoring_preserves_all_positive_qrels_and_missing_documents():
    rows = [ranked("q1", ["a"]), ranked("q2", [])]
    labels = {"qrels": {"q1": {"a": 2, "b": 1, "zero": 0}, "q2": {"missing": 1}}}
    result = comparison.score_rows(rows, labels, [{"id": "q1"}, {"id": "q2"}])
    assert result["all_queries"] == result["answered_queries"] == 2
    assert result["hit_at_10"] == .5
    assert result["document_recall_at_10"] == .25
    assert result["mrr_at_10"] == .5
    assert 0 < result["ndcg_at_10"] < .5


def test_unfaithful_candidate_does_not_move_later_hit_to_first_rank():
    result = comparison.score_rows([ranked("q", [None, "gold"])], {"qrels": {"q": {"gold": 1}}}, [{"id": "q"}])
    assert result["hit_at_1"] == 0
    assert result["hit_at_5"] == 1
    assert result["mrr_at_10"] == .5


@pytest.mark.parametrize("rows", [[], [ranked("q", [])] * 2, [ranked("other", ["gold"])]])
def test_partial_duplicate_or_wrong_query_export_cannot_score(rows):
    with pytest.raises(comparison.ComparisonError, match="IncompleteRankedExport"):
        comparison.score_rows(rows, {"qrels": {"q": {"gold": 1}}}, [{"id": "q"}])


def test_duplicate_source_result_cannot_inflate_rank_or_recall():
    row = ranked("q", ["gold"])
    row["candidate_refs"] *= 2
    with pytest.raises(comparison.ComparisonError, match="DuplicateRankedIdentity"):
        comparison.score_rows([row], {"qrels": {"q": {"gold": 1}}}, [{"id": "q"}])


def test_auxly_unordered_output_has_no_rank_metrics():
    result = comparison.quality_metrics("auxly", [ranked("q", ["distractor", "gold"])], {"qrels": {"q": {"gold": 1}}}, [{"id": "q"}])
    assert result["returned_subset_hit"] == 1
    assert result["rank_metrics"] is None
    assert "mrr_at_10" not in result and "hit_at_1" not in result


def test_auxly_source_text_cannot_impersonate_native_document_header():
    text = "\n📄 d000001.md\n  ordinary source line\n  📄 d000002.md\n"
    assert comparison.auxly_candidates(text, {"d000001": "actual", "d000002": "body-spoof"}) == ["actual"]
    with pytest.raises(comparison.ComparisonError, match="UnknownReturnedIdentity"):
        comparison.auxly_candidates("\n📄 d999999.md\n", {})


def test_inspected_lme_keeps_six_corpus_names_and_all_queries():
    manifest, groups, queries = comparison.fixture(Path(comparison.REPO) / "benchmarks/retrieval_fixtures/longmemeval")
    assert manifest["query_count"] == len(queries) == 6
    assert manifest["corpus_count"] == sum(map(len, groups.values())) == 191
    assert all(query["corpus"] in groups for query in queries)


def test_unregistered_corpus_cannot_change_the_measured_input(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    corpus = data / "corpus.jsonl"
    corpus.write_text('{"id":"d","content":"fixture body"}\n', encoding="utf-8")
    queries = data / "queries-selected.jsonl"
    queries.write_text('{"id":"q","query":"fixture question"}\n', encoding="utf-8")
    manifest = {"name": "prospective", "corpus_count": 1, "query_count": 1, "files": {path.name: comparison.digest(path) for path in (corpus, queries)}}
    comparison.write_json(data / "manifest.json", manifest)
    (data / "corpus-added.jsonl").write_text('{"id":"extra","content":"extra body"}\n', encoding="utf-8")
    with pytest.raises(comparison.ComparisonError, match="UnregisteredCorpusFile"):
        comparison.fixture(data)


def test_isolation_removes_inherited_cloud_and_mesh_override_inputs(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "YOUR_TOKEN_HERE")
    monkeypatch.setenv("MESH_MAX_CONTENT_LENGTH", "999999999")
    monkeypatch.setenv("MESH_AUTO_APPROVE", "1")
    monkeypatch.setenv("GITHUB_TOKEN", "YOUR_TOKEN_HERE")
    for name in ("AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "HF_TOKEN"):
        monkeypatch.setenv(name, "YOUR_TOKEN_HERE")
    env = comparison.isolated_environment(tmp_path)
    assert "OPENAI_API_KEY" not in env and "GITHUB_TOKEN" not in env
    assert not any(name in env for name in ("AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "HF_TOKEN"))
    assert "MESH_MAX_CONTENT_LENGTH" not in env
    assert env["MESH_AUTO_APPROVE"] == "0"
    assert env["AUXLY_HOST_SELFHEAL"] == "off"
    assert env["BASIC_MEMORY_AUTO_UPDATE"] == "false"
    assert Path(env["BASIC_MEMORY_CONFIG_DIR"]).is_relative_to(tmp_path)


def test_native_execution_requires_disconnected_child_network_namespace(tmp_path, monkeypatch):
    monkeypatch.setattr(comparison.sys, "platform", "linux")
    monkeypatch.setattr(comparison.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(comparison.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(comparison.os, "getgid", lambda: 1000, raising=False)
    command = comparison.native_command(tmp_path / "config.json")
    assert command[:5] == ["sudo", "-n", "--preserve-env", "unshare", "--net"]
    assert all(value in command for value in ("--pid", "--fork", "--kill-child=SIGKILL", "--mount-proc"))
    assert "--no-new-privs" in command and "setpriv" in command
    monkeypatch.setattr(comparison.shutil, "which", lambda name: None)
    with pytest.raises(comparison.ComparisonError, match="NetworkIsolationUnavailable"):
        comparison.native_command(tmp_path / "config.json")


def test_native_guard_runs_before_opening_dataset_or_starting_worker(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(comparison.ComparisonError, match="NativeComparisonRequiresGitHubActions"):
        comparison.run(type("Args", (), {"output": tmp_path})())


def test_competitor_llm_factory_always_fails_closed():
    with pytest.raises(comparison.ComparisonError, match="LlmInferenceForbidden"):
        comparison.ForbiddenLlm().generate_response("fixture question")


def test_mesh_near_duplicate_replacement_never_credits_previous_source_body():
    adapter = comparison.MeshAdapter.__new__(comparison.MeshAdapter)
    adapter.ids, adapter.bodies, adapter.unfaithful = {}, {}, set()
    stored = {}
    def learn(body, **kwargs):
        stored.update(id="same-runtime-id", content=body, embedding_model="onnx-minilm-l6-v2", local_only=False, quarantined=False)
        return {"memory_id": "same-runtime-id"}
    adapter.mesh = SimpleNamespace(learn=learn, store=SimpleNamespace(get=lambda identifier: dict(stored)), recall=lambda *args, **kwargs: [dict(stored)])
    assert adapter.add({"id": "old-source", "content": "Port is 8080."})
    assert adapter.add({"id": "new-source", "content": "Port is 9090."})
    assert adapter.search("Which port?") == ["new-source"]
    assert adapter.add({"id": "identical-alias", "content": "Port is 9090."})
    assert adapter.search("Which port?") == ["new-source"]
    result = comparison.score_rows([ranked("q", adapter.search("Which port?"))], {"qrels": {"q": {"old-source": 1}}}, [{"id": "q"}])
    assert result["hit_at_10"] == 0 and result["answered_queries"] == 1


def test_qualification_lf_crlf_conversion_requires_same_pinned_git_blob(tmp_path, monkeypatch):
    monkeypatch.setattr(comparison, "REPO", tmp_path)
    path = tmp_path / "mesh/example.py"
    path.parent.mkdir()
    blob = b"first = 1\nsecond = 2\n"
    path.write_bytes(blob)
    monkeypatch.setattr(comparison, "git_blob", lambda commit, relative: blob)
    windows_hash = hashlib.sha256(blob.replace(b"\n", b"\r\n")).hexdigest()
    assert comparison.source_equivalence("mesh/example.py", windows_hash, "a" * 40, "b" * 40) == comparison.digest(path)
    path.write_bytes(blob.replace(b"second = 2", b"second = 3"))
    with pytest.raises(comparison.ComparisonError, match="QualificationSourceMismatch"):
        comparison.source_equivalence("mesh/example.py", windows_hash, "a" * 40, "b" * 40)
    path.write_bytes(blob)
    monkeypatch.setattr(comparison, "git_blob", lambda commit, relative: blob if commit == "a" * 40 else blob.replace(b"second = 2", b"second = 3"))
    with pytest.raises(comparison.ComparisonError, match="QualificationGitBlobMismatch"):
        comparison.source_equivalence("mesh/example.py", windows_hash, "a" * 40, "b" * 40)


def qualification_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(comparison, "REPO", tmp_path)
    blobs = {}
    for name in ("mesh/example.py", "pyproject.toml", "scripts/run_tests.py", "benchmarks/production_test_speed.py", *sorted(comparison.QUALIFICATION_HARNESS)):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        blobs[name] = b"fixture = 1\n"
        path.write_bytes(blobs[name])
    hashes = {name: hashlib.sha256(blob.replace(b"\n", b"\r\n")).hexdigest() for name, blob in blobs.items()}
    source = {"runtime_sources": {name: hashes[name] for name in ("mesh/example.py", "pyproject.toml")}, "harness_sources": {name: hashes[name] for name in comparison.QUALIFICATION_HARNESS}, "test_execution_sources": {"launcher_sha256": hashes["scripts/run_tests.py"], "controller_sha256": hashes["benchmarks/production_test_speed.py"]}, "runtime": {"python_version": "3.14.0", "system": "Windows", "packages": {"numpy": "2.3.5", "apsw": "3.51.3.0"}}}
    directories = [tmp_path / ".work" / kind for kind in ("primary", "floor")]
    for directory in directories:
        directory.mkdir(parents=True)
        comparison.write_json(directory / "source-runtime.json", source)
        (directory / "constraints.txt").write_text("numpy==2.3.5\napsw==3.51.3.0\npytest==9.0.2\n", encoding="utf-8")
        (directory / "full.xml").write_bytes(b"<testsuite tests='2' failures='0' errors='0'/>\n")
    (directories[0] / "coverage.xml").write_bytes(b"<coverage line-rate='0.75'/>\n")
    comparison.write_json(directories[0] / "package-final-validation.json", {"status": "passed"})
    monkeypatch.setattr(comparison, "git_blob", lambda commit, relative: blobs[relative])
    monkeypatch.setattr(comparison.subprocess, "check_output", lambda *args, **kwargs: "b" * 40)
    monkeypatch.setitem(sys.modules, "production_runner", SimpleNamespace(qualified_junit=lambda *args, **kwargs: {"tests": 2, "failures": 0, "errors": 0}))
    return directories


def test_qualification_retains_exact_junit_coverage_package_and_both_source_digests(tmp_path, monkeypatch):
    primary, floor = qualification_fixture(tmp_path, monkeypatch)
    result = comparison.verify_qualification(primary, floor, "a" * 40)
    assert result["primary"]["sha256"] == comparison.digest(primary / "full.xml")
    assert result["floor"]["sha256"] == comparison.digest(floor / "full.xml")
    assert result["coverage_sha256"] == comparison.digest(primary / "coverage.xml")
    assert result["package_report_sha256"] == comparison.digest(primary / "package-final-validation.json")
    assert result["qualification_source_commit"] == "a" * 40 and result["same_source"]
    assert result["primary"]["constraints_sha256"] == comparison.digest(primary / "constraints.txt")
    assert result["primary"]["runtime"]["system"] == "Windows"


def test_qualification_artifact_changed_mid_verification_cannot_pass(tmp_path, monkeypatch):
    primary, floor = qualification_fixture(tmp_path, monkeypatch)
    original = comparison.source_equivalence
    changed = False
    def mutate(relative, expected, commit, head):
        nonlocal changed
        if not changed:
            with (primary / "source-runtime.json").open("a", encoding="utf-8") as stream:
                stream.write(" ")
            changed = True
        return original(relative, expected, commit, head)
    monkeypatch.setattr(comparison, "source_equivalence", mutate)
    with pytest.raises(comparison.ComparisonError, match="QualificationEvidenceChangedDuringVerification"):
        comparison.verify_qualification(primary, floor, "a" * 40)


def test_orphan_mcp_session_is_stopped_after_leader_exit_without_killing_reused_pid(monkeypatch):
    monkeypatch.setattr(comparison.signal, "SIGKILL", 9, raising=False)
    class Gone(Exception):
        pass
    calls = []
    child = SimpleNamespace(create_time=lambda: 7.0, kill=lambda: calls.append("kill-child"), wait=lambda timeout: calls.append("wait-child"))
    reused = SimpleNamespace(create_time=lambda: 100.0)
    def process(pid):
        if pid == 22:
            raise Gone()
        return child if pid == 23 else reused
    fake = SimpleNamespace(Process=process, NoSuchProcess=Gone, AccessDenied=type("Denied", (Exception,), {}), TimeoutExpired=type("TimedOut", (Exception,), {}))
    monkeypatch.setattr(comparison.os, "getpgrp", lambda: 999, raising=False)
    monkeypatch.setattr(comparison.os, "getpgid", lambda pid: 33, raising=False)
    monkeypatch.setattr(comparison.os, "killpg", lambda group, sig: calls.append((group, sig)), raising=False)
    assert comparison.stop_observed_processes(fake, {(22, 6.0): 22, (23, 7.0): 33, (24, 8.0): 44})
    assert calls == [(33, comparison.signal.SIGKILL), "wait-child"]


def test_shared_dependencies_fail_on_upgrade_or_missing_native_package(tmp_path, monkeypatch):
    primary, floor = qualification_fixture(tmp_path, monkeypatch)
    evidence = comparison.verify_qualification(primary, floor, "a" * 40)
    report = comparison.dependency_report(evidence, {"NumPy": "2.3.5", "APSW": "3.51.3.0", "fastembed": "0.9.1"})
    assert report["compatible"] and report["unqualified_packages"] == {"fastembed": "0.9.1"}
    assert not comparison.dependency_report(evidence, {"numpy": "2.4.0", "apsw": "3.51.3.0"})["compatible"]
    assert comparison.dependency_report(evidence, {"numpy": "2.3.5"})["missing_native_packages"] == ["apsw"]
    with (primary / "constraints.txt").open("a", encoding="utf-8") as stream:
        stream.write("--extra-index-url https://example.invalid/\n")
    with pytest.raises(comparison.ComparisonError, match="InvalidQualifiedDependencyConstraint"):
        comparison.verify_qualification(primary, floor, "a" * 40)


def test_bm25_provisioning_is_immutable_static_bytes_and_native_cache_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(comparison, "REPO", tmp_path)
    bodies = {"config.json": b"{}", "english.txt": b"the\n"}
    monkeypatch.setattr(comparison, "BM25_FILES", {name: (len(body), hashlib.sha256(body).hexdigest()) for name, body in bodies.items()})
    urls = []
    def download(request, timeout):
        urls.append(request.full_url)
        return io.BytesIO(bodies[request.full_url.rsplit("/", 1)[1]])
    monkeypatch.setattr(comparison.urllib.request, "urlopen", download)
    directory = tmp_path / ".work" / "bm25"
    comparison.provision_bm25(directory)
    assert all("/resolve/" + comparison.BM25_REVISION + "/" in url for url in urls)
    worker_home = tmp_path / "worker"
    environment = comparison.isolated_environment(worker_home)
    snapshot = comparison.install_bm25_cache(directory, worker_home)
    assert snapshot == Path(environment["FASTEMBED_CACHE_PATH"]) / "models--Qdrant--bm25" / "snapshots" / comparison.BM25_REVISION
    assert (snapshot.parent.parent / "refs/main").read_bytes() == comparison.BM25_REVISION.encode("utf-8")
    (snapshot / "english.txt").write_bytes(b"bad\n")
    with pytest.raises(comparison.ComparisonError, match="Bm25AssetDigestMismatch"):
        comparison.verify_bm25(snapshot)


def fake_mem0(monkeypatch, *, slot=True):
    class Encoder:
        def embed(self, documents):
            return iter(SimpleNamespace(indices=[1], values=[1.0]) for _ in documents)
    encoder = Encoder()
    monkeypatch.setitem(sys.modules, "fastembed", SimpleNamespace(SparseTextEmbedding=Encoder))
    monkeypatch.setattr(comparison.importlib.metadata, "version", lambda name: comparison.FASTEMBED_VERSION)
    client = SimpleNamespace(get_collection=lambda name: SimpleNamespace(config=SimpleNamespace(params=SimpleNamespace(sparse_vectors={"bm25": {}} if slot else {}))), upsert=lambda **kwargs: None, close=lambda: None)
    store = SimpleNamespace(_bm25_encoder=encoder, _has_bm25_slot=slot, collection_name="comparison", client=client, _get_bm25_encoder=lambda: encoder)
    def keyword(query):
        list(encoder.embed([query]))
        return []
    store.keyword_search = keyword
    stored = {}
    def add(body, **kwargs):
        list(encoder.embed([body]))
        client.upsert(collection_name="comparison", points=[SimpleNamespace(vector={"": [1.0], "bm25": SimpleNamespace(indices=[1], values=[1.0])})])
        stored["memory"] = body
        return {"results": [{"id": "native-id"}]}
    def search(query, **kwargs):
        store.keyword_search(query)
        return {"results": [{"id": "native-id", "memory": stored["memory"]}]}
    adapter = comparison.Mem0Adapter.__new__(comparison.Mem0Adapter)
    adapter.memory = SimpleNamespace(vector_store=store, add=add, search=search, get=lambda identifier: dict(stored))
    adapter.ids, adapter.bodies = {}, {}
    adapter.monitor_bm25()
    return adapter


def test_mem0_native_sparse_operations_account_and_same_identity_preserves_canonical_body(monkeypatch):
    adapter = fake_mem0(monkeypatch)
    assert adapter.add({"id": "first", "content": "Port is 8080."})
    assert adapter.add({"id": "duplicate", "content": "Port is 8080."})
    assert adapter.search("Port?") == ["first"]
    assert adapter.add({"id": "replacement", "content": "Port is 9090."})
    assert adapter.search("Port?") == ["replacement"]
    assert adapter.bm25_counts == {"encoded": 5, "sparse_written": 3, "sparse_queried": 2, "fallback": False}


def test_mem0_missing_sparse_slot_cannot_measure(monkeypatch):
    with pytest.raises(comparison.ComparisonError, match="Bm25ReadinessFailed"):
        fake_mem0(monkeypatch, slot=False)


def test_mem0_silent_dense_query_fallback_cannot_measure(monkeypatch):
    adapter = fake_mem0(monkeypatch)
    adapter.memory.search = lambda *args, **kwargs: {"results": []}
    with pytest.raises(comparison.ComparisonError, match="Bm25IncompleteNativeAccounting"):
        adapter.search("fixture question")


def test_mem0_dense_only_write_cannot_measure(monkeypatch):
    adapter = fake_mem0(monkeypatch)
    adapter.memory.add = lambda *args, **kwargs: adapter.memory.vector_store.client.upsert(points=[SimpleNamespace(vector={"": [1.0]})])
    with pytest.raises(comparison.ComparisonError, match="Bm25SparseWriteFailed"):
        adapter.add({"id": "d", "content": "fixture body"})


def test_mem0_failed_encoding_marks_native_fallback_unscored(monkeypatch):
    adapter = fake_mem0(monkeypatch)
    adapter.bm25_counts["fallback"] = True
    with pytest.raises(comparison.ComparisonError, match="Bm25IncompleteNativeAccounting"):
        adapter.add({"id": "d", "content": "fixture body"})


def test_qualification_rejects_missing_profiler_and_mixed_checkout_line_endings(tmp_path, monkeypatch):
    primary, floor = qualification_fixture(tmp_path, monkeypatch)
    source_path = primary / "source-runtime.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    del source["harness_sources"]["benchmarks/storage_profile.py"]
    comparison.write_json(source_path, source)
    with pytest.raises(comparison.ComparisonError, match="QualificationHarnessSelectionMismatch"):
        comparison.verify_qualification(primary, floor, "a" * 40)
    path = tmp_path / "mesh/example.py"
    blob = b"first = 1\nsecond = 2\n"
    monkeypatch.setattr(comparison, "git_blob", lambda *args: blob)
    path.write_bytes(b"first = 1\r\nsecond = 2\n")
    with pytest.raises(comparison.ComparisonError, match="QualificationSourceMismatch"):
        comparison.source_equivalence("mesh/example.py", hashlib.sha256(blob).hexdigest(), "a" * 40, "b" * 40)

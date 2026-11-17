"""Offline comparison accounting and source identity regressions; no native models."""
from __future__ import annotations

import io
import json
import hashlib
from contextlib import asynccontextmanager
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


def test_native_namespace_uses_explicit_copied_child_interpreter(tmp_path, monkeypatch):
    monkeypatch.setattr(comparison, "REPO", tmp_path)
    monkeypatch.setattr(comparison.sys, "platform", "linux")
    monkeypatch.setattr(comparison.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(comparison.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(comparison.os, "getgid", lambda: 1000, raising=False)
    child = tmp_path / ".work/native/bin/python"
    child.parent.mkdir(parents=True)
    child.write_bytes(b"copied interpreter fixture")
    command = comparison.native_command(tmp_path / "config.json", child)
    assert command[command.index("--no-new-privs") + 1] == str(child)
    assert "--net" in command and "--kill-child=SIGKILL" in command
    outside = tmp_path / "outside-python"
    outside.write_bytes(b"outside fixture")
    with pytest.raises(comparison.ComparisonError, match="PathOutsideOwnedRoot"):
        comparison.native_command(tmp_path / "config.json", outside)


def test_native_mcp2_inventory_does_not_relax_mesh_host_dependencies():
    packages = {"basic-memory": "0.24.1", "mcp": "2.0.0", "psutil": "7.2.2"}
    report = {"tool": "basic-memory", "packages": packages, "python_version": "3.12.0", "encoder_versions": {"psutil": "7.2.2"}}
    comparison.verify_child_inventory(report, packages, "3.12.0")
    evidence = {"primary": {"dependency_constraints": {"mcp": "1.30.0", "psutil": "7.2.2"}, "runtime": {"packages": {"mcp": "1.30.0", "psutil": "7.2.2"}}}}
    assert comparison.dependency_report(evidence, packages)["compatible"] is False
    with pytest.raises(comparison.ComparisonError, match="ChildEnvironmentChanged"):
        comparison.verify_child_inventory(report, packages | {"mcp": "2.1.0"}, "3.12.0")
    with pytest.raises(comparison.ComparisonError, match="ChildEnvironmentChanged"):
        comparison.verify_child_inventory(report, packages, "3.13.0")


def test_mem0_child_encoder_and_native_bm25_versions_are_required():
    packages = {"mem0ai": "2.2.1", "fastembed": "0.9.1", "protobuf": "6.33.0", "numpy": "2.5.5"}
    report = {"tool": "mem0", "packages": packages, "python_version": "3.12.0", "encoder_versions": {"numpy": "2.5.5"}}
    comparison.verify_child_inventory(report, packages, "3.12.0")
    bad = report | {"encoder_versions": {"numpy": "2.2.0"}}
    with pytest.raises(comparison.ComparisonError, match="ChildEncoderVersionMismatch"):
        comparison.verify_child_inventory(bad, packages, "3.12.0")
    bad_packages = packages | {"fastembed": "0.8.0"}
    with pytest.raises(comparison.ComparisonError, match="FastembedVersionMismatch"):
        comparison.verify_child_inventory(report | {"packages": bad_packages}, bad_packages, "3.12.0")


def child_fixture(tmp_path, monkeypatch, *, extra_profile_error=False):
    monkeypatch.setattr(comparison, "REPO", tmp_path)
    directory = tmp_path / ".work/native"
    (directory / "bin").mkdir(parents=True)
    (directory / "bin/python").write_bytes(b"copied fixture interpreter")
    (directory / "bin/basic-memory").write_bytes(b"native fixture entrypoint")
    wheel = tmp_path / ".work/basic-memory.whl"
    wheel.write_bytes(b"immutable official fixture wheel")
    monkeypatch.setitem(comparison.PINS, "basic-memory", comparison.PINS["basic-memory"] | {"sha256": comparison.digest(wheel)})
    summary = tmp_path / ".work/summary.json"
    evidence = {"primary": {"runtime": {"packages": {"psutil": "7.2.2"}}}}
    comparison.write_json(summary, {"qualification": evidence})
    packages = {"basic-memory": "0.24.1", "mcp": "2.0.0", "psutil": "7.2.2"}
    snapshot = {"packages": packages, "python_version": "3.12.0", "platform": "linux", "machine": "x86_64", "abi": "cpython-312"}
    monkeypatch.setattr(comparison, "child_snapshot", lambda interpreter: snapshot)
    calls = []
    check_count = 0
    def command(args, **kwargs):
        nonlocal check_count
        calls.append(args)
        if args[-1] == "check":
            check_count += 1
            if check_count == 1:
                return SimpleNamespace(returncode=0, stdout=b"No broken requirements found.\n")
            text = b"basic-memory 0.24.1 requires sqlite-vec, which is not installed.\n"
            if extra_profile_error:
                text += b"fixture 1.0 requires other-package, which is not installed.\n"
            return SimpleNamespace(returncode=1, stdout=text)
        return SimpleNamespace(returncode=0, stdout=b"listed package fixture\n")
    monkeypatch.setattr(comparison.subprocess, "run", command)
    return SimpleNamespace(tool="basic-memory", directory=directory, wheel=wheel, summary=summary), evidence, calls, snapshot


def test_freeze_child_checks_native_dependencies_and_removes_vector_only_in_child(tmp_path, monkeypatch):
    args, qualification, calls, snapshot = child_fixture(tmp_path, monkeypatch)
    comparison.freeze_child(args)
    report = comparison.verify_child_environment(args.directory, qualification, "basic-memory")
    assert report["packages"] == snapshot["packages"]
    assert report["dependency_consistency"] == {"before_profile": "passed", "after_profile": "only_declared_sqlite_vec_absence"}
    assert json.loads(args.summary.read_text())["child_environment"] == report
    assert all(command[0] == str(args.directory / "bin/python") for command in calls)
    assert any(command[-3:] == ["uninstall", "-y", "sqlite-vec"] for command in calls)
    assert (args.directory / "removed-vector-files-private.txt").is_file()
    (args.directory / "bin/basic-memory").write_bytes(b"changed native entrypoint")
    with pytest.raises(comparison.ComparisonError, match="ChildEnvironmentFilesChanged"):
        comparison.verify_child_environment(args.directory, qualification, "basic-memory")


def test_keyword_profile_cannot_hide_unrelated_dependency_failure(tmp_path, monkeypatch):
    args, _, _, _ = child_fixture(tmp_path, monkeypatch, extra_profile_error=True)
    with pytest.raises(comparison.ComparisonError, match="UnexpectedKeywordProfileDependencyFailure"):
        comparison.freeze_child(args)
    assert not (args.directory / "environment.json").exists()


def test_child_inventory_cannot_change_the_qualified_resource_sampler(tmp_path, monkeypatch):
    args, qualification, _, _ = child_fixture(tmp_path, monkeypatch)
    comparison.freeze_child(args)
    qualification["primary"]["runtime"]["packages"]["psutil"] = "7.0.0"
    with pytest.raises(comparison.ComparisonError, match="ChildEnvironmentSelectionMismatch"):
        comparison.verify_child_environment(args.directory, qualification, "basic-memory")


def test_worker_failure_projection_excludes_exception_text_and_unknown_paths():
    marker = "YOUR_TOKEN_HERE private note body"
    try:
        raise RuntimeError(marker)
    except RuntimeError as error:
        report = comparison.worker_failure_projection(error)
    rendered = json.dumps(report)
    assert marker not in rendered and "test_competitor_comparison.py" not in rendered
    assert report["failures"] == [{"exception_type": "RuntimeError", "code": None, "frames": []}]
    comparison.checked_worker_failure(report)
    code = comparison.worker_failure_projection(comparison.ComparisonError("SourceBodyChangedDuringSearch"))
    assert code["failures"][0]["code"] == "SourceBodyChangedDuringSearch"
    unknown = comparison.worker_failure_projection(comparison.ComparisonError(marker))
    assert unknown["failures"][0]["code"] is None and marker not in json.dumps(unknown)


def test_worker_failure_projection_bounds_nested_errors_and_rejects_full_paths():
    class Group(Exception):
        exceptions = tuple(RuntimeError("private fixture body") for _ in range(100))
    report = comparison.worker_failure_projection(Group("private group body"))
    assert len(report["failures"]) == 8 and "private" not in json.dumps(report)
    comparison.checked_worker_failure(report)
    report["failures"][0]["frames"] = [{"source": "private/path/main.py", "line": 1}]
    with pytest.raises(comparison.ComparisonError, match="InvalidWorkerDiagnostic"):
        comparison.checked_worker_failure(report)


def test_worker_diagnostic_uses_registered_exception_types_only():
    custom = type("PrivateNoteClass", (Exception,), {})
    report = comparison.worker_failure_projection(custom("private fixture body"))
    assert report["failures"][0]["exception_type"] == "Exception"
    assert "PrivateNoteClass" not in json.dumps(report)
    report["failures"][0]["exception_type"] = "PrivateNoteClass"
    with pytest.raises(comparison.ComparisonError, match="InvalidWorkerDiagnostic"):
        comparison.checked_worker_failure(report)


@pytest.mark.parametrize("failure", [None, {"exception_type": [], "code": None, "frames": []}, {"exception_type": "Exception", "code": [], "frames": []}, {"exception_type": "Exception", "code": None, "frames": [None]}])
def test_malformed_worker_diagnostics_have_a_fixed_rejection(failure):
    with pytest.raises(comparison.ComparisonError, match="InvalidWorkerDiagnostic"):
        comparison.checked_worker_failure({"protocol": "comparison-worker-failure-v1", "failures": [failure]})


@pytest.mark.parametrize("newline_mutation", [False, True])
def test_native_note_metadata_does_not_trigger_permalink_rewrite_or_change_parent_body(tmp_path, monkeypatch, newline_mutation):
    monkeypatch.setattr(comparison, "REPO", tmp_path)
    interpreter = tmp_path / ".work/native/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"copied fixture interpreter")
    (interpreter.parent / "basic-memory").write_bytes(b"native fixture entrypoint")
    monkeypatch.setattr(comparison.sys, "executable", str(interpreter))
    root = tmp_path / ".work/worker"
    root.mkdir()
    body = "Original parent body.\n\n---\nThis remains source content."
    def index(command, **kwargs):
        note = root / "notes/d000000.md"
        text = note.read_text(encoding="utf-8")
        # The official batch indexer adds missing canonical permalink metadata.
        if "\npermalink: d000000\n" not in text:
            note.write_text(text.replace("title: d000000\n", "title: d000000\npermalink: d000000\n"), encoding="utf-8")
        if newline_mutation:
            note.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(comparison.subprocess, "run", index)
    @asynccontextmanager
    async def stdio(params):
        assert params.command == str(interpreter.parent / "basic-memory")
        yield None, None
    class Session:
        def __init__(self, *args):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def initialize(self):
            pass
        async def call_tool(self, name, arguments):
            assert name == "search_notes" and arguments["search_type"] == "text"
            return SimpleNamespace(isError=False, structuredContent={"results": [{"type": "entity", "file_path": "d000000.md", "title": "d000000"}]})
    monkeypatch.setitem(sys.modules, "mcp", SimpleNamespace(ClientSession=Session, StdioServerParameters=lambda **kwargs: SimpleNamespace(**kwargs)))
    monkeypatch.setitem(sys.modules, "mcp.client.stdio", SimpleNamespace(stdio_client=stdio))
    accounting = {"ingested": 0, "faithful": 0, "ingest_seconds": 0, "queried": 0}
    output = io.StringIO()
    invoke = comparison.mcp_group("basic-memory", root, [{"id": "source", "content": body}], [{"id": "query", "query": "fixture question"}], output, accounting, lambda: None)
    if newline_mutation:
        with pytest.raises(comparison.ComparisonError, match="SourceBodyChangedDuringSearch"):
            comparison.asyncio.run(invoke)
        return
    comparison.asyncio.run(invoke)
    assert (root / "notes/d000000.md").read_text(encoding="utf-8") == "---\ntitle: d000000\ntype: note\npermalink: d000000\n---\n" + body + "\n"
    assert accounting["queried"] == 1


def test_mesh_comparison_wait_uses_actual_supported_timeout_and_invalid_config_is_unscored():
    from mesh.memory import _validate_index_wait
    adapter = comparison.MeshAdapter.__new__(comparison.MeshAdapter)
    adapter.ids, adapter.bodies, adapter.unfaithful = {}, {}, set()
    stored = {"id": "native", "content": "Original source parent.", "embedding_model": "onnx-minilm-l6-v2", "local_only": False, "quarantined": False}
    def learn(body, **kwargs):
        _validate_index_wait(kwargs["wait_for_indexed"], kwargs["index_timeout"])
        return {"memory_id": "native"}
    adapter.mesh = SimpleNamespace(learn=learn, store=SimpleNamespace(get=lambda identifier: stored))
    assert adapter.add({"id": "source", "content": stored["content"]}) is True
    adapter.mesh.learn = lambda *args, **kwargs: _validate_index_wait(True, 120)
    with pytest.raises(comparison.ComparisonError, match="MeshLearnFailed"):
        adapter.add({"id": "source", "content": stored["content"]})


def test_only_mesh_content_boundary_rejection_counts_as_rejected_parent():
    from mesh.memory import Mesh
    mesh = Mesh.__new__(Mesh)
    mesh.namespace, mesh.agent_id, mesh.trusted_local = "comparison-fixture", "comparison", True
    adapter = comparison.MeshAdapter.__new__(comparison.MeshAdapter)
    adapter.mesh = SimpleNamespace(learn=lambda body, **kwargs: mesh._prepare_learn(body))
    with pytest.raises(comparison.ParentRejected):
        adapter.add({"id": "source", "content": ""})
    adapter.mesh.learn = lambda body, **kwargs: mesh._prepare_learn(body, local_only="invalid fixture flag")
    with pytest.raises(comparison.ComparisonError, match="MeshLearnFailed"):
        adapter.add({"id": "source", "content": "Original source parent."})
    adapter.mesh.learn = lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("fixture internal failure"))
    with pytest.raises(comparison.ComparisonError, match="MeshLearnFailed"):
        adapter.add({"id": "source", "content": "Original source parent."})

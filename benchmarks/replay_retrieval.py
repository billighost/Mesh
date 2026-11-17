"""Replay an accounted guarded checkpoint through an immutable current Mesh copy.

Only ranking/storage is measured: saved neural query vectors avoid model inference.
The source must be a named isolated benchmark home, never a real user Mesh home.
No relevance label is read here. Compact exports can be scored separately.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.metadata
import json
import os
import shutil
import stat
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

FIELDS = ("raw_hybrid_top64", "final_sdk_top10", "authorized_sdk_rerank_top24", "dense_public_top64")
ISOLATED_PREFIXES = ("mesh-frozen-quality-", "mesh-current-default-quality-", "mesh-passage-quality-",
                     "mesh-retrieval-ci-", "mesh-passages-ci-")
PROVIDER_PREFIXES = ("OPENAI_", "ANTHROPIC_", "AZURE_OPENAI_", "AZURE_AI_", "GOOGLE_", "GEMINI_",
                     "COHERE_", "MISTRAL_", "GROQ_", "TOGETHER_", "FIREWORKS_", "OPENROUTER_",
                     "DEEPSEEK_", "PERPLEXITY_", "REPLICATE_", "HF_", "HUGGINGFACE_", "AWS_",
                     "BEDROCK_", "VERTEX_", "OLLAMA_", "CLAUDE_CODE_", "CODEX_")


def checked_path(value, *, root=None, must_exist=False, directory=None):
    """Reject links and Windows reparse ancestors before canonicalizing a path."""
    original = Path(value)
    if ".." in original.parts:
        raise ValueError("Parent traversal in artifact path")
    absolute = original.absolute()
    for part in (*reversed(absolute.parents), absolute):
        try:
            metadata = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise ValueError("Linked or reparse artifact ancestor")
        if part != absolute and not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("Artifact ancestor is not a directory")
    path = absolute.resolve(strict=must_exist)
    if path != absolute:
        raise ValueError("Artifact path changed during validation")
    if root is not None:
        path.relative_to(checked_path(root, must_exist=True, directory=True))
    if directory is True and path.exists() and not path.is_dir():
        raise ValueError("Artifact directory is not a directory")
    if directory is False and path.exists() and not path.is_file():
        raise ValueError("Artifact file is not a regular file")
    return path


def safe_member(root, relative, *, must_exist=True):
    """Manifest members are portable, normalized relative filenames only."""
    if (not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative
            or "\x00" in relative or PureWindowsPath(relative).is_absolute()):
        raise ValueError("Unsafe manifest filename")
    parts = PurePosixPath(relative)
    if parts.is_absolute() or any(part in {".", ".."} for part in relative.split("/")) or parts.as_posix() != relative:
        raise ValueError("Unsafe manifest filename")
    return checked_path(Path(root) / relative, root=root, must_exist=must_exist, directory=False)


def isolated_artifact_path(value, repository=None, *, must_exist=False):
    path = checked_path(value, must_exist=must_exist)
    if repository is not None:
        work = checked_path(Path(repository) / ".work", directory=True)
        if path.is_relative_to(work) and path != work:
            return path
    temporary = checked_path(Path(tempfile.gettempdir()).resolve(strict=True), must_exist=True, directory=True)
    if path.is_relative_to(temporary):
        relative = path.relative_to(temporary)
        if relative.parts and relative.parts[0].startswith(ISOLATED_PREFIXES):
            return path
    raise ValueError("Artifact must remain in repository .work or a named isolated temporary root")


def verify_exports(directory, manifest):
    exports = manifest.get("output_sha256")
    if not isinstance(exports, dict) or not exports:
        raise ValueError("Missing source export accounting")
    for relative, expected in exports.items():
        if not isinstance(expected, str) or len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
            raise ValueError("Invalid source export digest")
        if digest_file(safe_member(directory, relative)) != expected:
            raise ValueError("Guarded source export changed")


def digest_file(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def read_jsonl(path: Path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")


def isolated_source_home(value: str) -> Path:
    path = isolated_artifact_path(value, must_exist=True)
    if path.name != "mesh-home" or not path.parent.name.startswith("mesh-frozen-quality-"):
        raise ValueError("Source must be the named isolated guarded quality home")
    return path


def configure_isolation(root: Path) -> None:
    root = isolated_artifact_path(root, must_exist=True)
    for name in list(os.environ):
        upper = name.upper()
        if (upper.startswith(("MESH_", *PROVIDER_PREFIXES))
                or upper.endswith(("_API_KEY", "_API_TOKEN", "_AUTH_TOKEN", "_ACCESS_TOKEN"))
                or upper in {"API_KEY", "API_TOKEN", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"}):
            os.environ.pop(name)
    for name, folder in (("MESH_HOME", "mesh-home"), ("HOME", "home"), ("USERPROFILE", "home"),
                         ("APPDATA", "appdata"), ("LOCALAPPDATA", "localappdata"), ("XDG_CONFIG_HOME", "xdg/config"),
                         ("XDG_CACHE_HOME", "xdg/cache"), ("XDG_DATA_HOME", "xdg/data"), ("CODEX_HOME", "codex"),
                         ("CLAUDE_CONFIG_DIR", "claude"), ("CLAUDE_HOME", "claude"), ("HF_HOME", "hf"),
                         ("HUGGINGFACE_HUB_CACHE", "hf/hub")):
        path = checked_path(root / folder, root=root, directory=True)
        path.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(path)
    os.environ.update(MESH_NO_AUTO_DAEMON="1", MESH_NO_MODEL_DOWNLOAD="1", MESH_NO_UPDATE_CHECK="1",
                      MESH_FORCE_HASH_EMBED="1", MESH_BG_ONNX="0", MESH_BG_ONNX_DRAIN_ON_EXIT="0",
                      MESH_AUTO_APPROVE="0", MESH_VAULT_SECRETS="0", MESH_AUTO_HEAL_ON_CALL="0",
                      MESH_NO_IMPLICIT_CLOUD="1", MESH_ENABLE_AUTO_SYNC="0", MESH_ALLOW_CLOUD_CAPTURE="0",
                      MESH_ALLOW_CLOUD_SYNC="0", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")


def freeze_runtime(repository: Path, destination: Path) -> dict[str, str]:
    repository = checked_path(repository, must_exist=True, directory=True)
    destination = checked_path(destination, directory=True)
    hashes = {}
    for source in sorted((repository / "mesh").rglob("*.py")):
        source = checked_path(source, root=repository, must_exist=True, directory=False)
        relative = source.relative_to(repository)
        target = checked_path(destination / relative, directory=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        before = digest_file(source)
        shutil.copyfile(source, target)
        if before != digest_file(target) or before != digest_file(source):
            raise ValueError("Repository source changed during immutable snapshot creation")
        hashes[relative.as_posix()] = before
    if not hashes:
        raise ValueError("No Mesh runtime source files found")
    return hashes


def compact_results(rows: list[dict], source_ids: dict[str, list[str]]) -> list[dict]:
    return [{
        "id": row["id"], "faithful_document_ids": source_ids.get(row["id"], []),
        "similarity": row.get("similarity"), "rank_score": row.get("rank_score"),
        "embedding_model": row.get("embedding_model"), "revision": row.get("revision"),
        "retrieval_mode": row.get("retrieval_mode"),
    } for row in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_run", type=Path)
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--dependency-directory", type=Path)
    parser.add_argument("--source-home", type=Path, help="Explicit named isolated home for a sanitized source manifest")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    repository = checked_path(args.repository, must_exist=True, directory=True)
    source_run = isolated_artifact_path(args.source_run, repository, must_exist=True)
    source_manifest_path = safe_member(source_run, "run-manifest.json")
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    if (source_manifest["status"] != "complete" or source_manifest.get("smoke_limit_cases")
            or source_manifest["embedding_model"] != "minilm"):
        raise ValueError("Source must be a complete untruncated guarded MiniLM quality run")
    verify_exports(source_run, source_manifest)
    required = {"ingestion-fidelity.jsonl", "query-vectors.jsonl", "results-C64.jsonl", "results-C96.jsonl"}
    if not required.issubset(source_manifest["output_sha256"]):
        raise ValueError("Source is missing a required complete baseline export")
    source_home = isolated_source_home(str(args.source_home or source_manifest["temporary_mesh_home"]))
    records = list(read_jsonl(source_run / "ingestion-fidelity.jsonl"))
    queries = list(read_jsonl(source_run / "query-vectors.jsonl"))
    query_ids = [row["query_id"] for row in queries]
    if len(queries) != source_manifest["query_target"] or len(set(query_ids)) != len(queries):
        raise ValueError("Saved neural vectors do not account for every query exactly once")
    model_tag = source_manifest["query_model_space_tag"]
    if any(row["embedding_model"] != model_tag for row in queries):
        raise ValueError("Saved query vector provenance is mixed")
    baseline = {}
    for cap in (64, 96):
        baseline[cap] = {row["query_id"]: {field: [item["id"] for item in row[field]] for field in FIELDS}
                         for row in read_jsonl(source_run / f"results-C{cap}.jsonl")}
        if set(baseline[cap]) != set(query_ids):
            raise ValueError("Frozen source ranks do not account for every query")
    output = isolated_artifact_path(args.output, repository)
    if output.exists():
        raise ValueError("Refusing to overwrite an existing replay")
    output.mkdir(parents=True)
    manifest = {
        "status": "running", "dataset": source_manifest["dataset"],
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "query_target": len(queries), "source_run_manifest_sha256": digest_file(source_manifest_path),
        "source_output_sha256": source_manifest["output_sha256"],
        "source_dataset_manifest_sha256": source_manifest["dataset_manifest_sha256"],
        "implementation_sha256": digest_file(Path(__file__)),
        "scope": "Immutable current runtime, production default 256MiB cache, guarded MiniLM corpus and saved neural query vectors. Exact ordered ranks compare with the identified source condition; no fresh-inference or performance claim.",
        "source_runtime_files": source_manifest["runtime_files"],
        "source_cache_condition_bytes": source_manifest.get("cache_condition_bytes"),
        "labels_in_ranking_inputs": False, "neural_inference_invoked": False,
        "model_tag": model_tag, "candidate_caps": [64, 96],
        "ranking_comparison_fields": list(FIELDS),
        "timing_scope": "Loaded development run duration is diagnostic only, not quiet comparative latency.",
    }
    write_json(output / "run-manifest.json", manifest)
    streams = {cap: (output / f"results-C{cap}.jsonl").open("w", encoding="utf-8", newline="\n") for cap in (64, 96)}
    comparisons = {str(cap): {field: {"identical_ranked_queries": 0, "top1_equal_queries": 0,
                                     "top10_equal_queries": 0, "changed_query_ids": []} for field in FIELDS} for cap in (64, 96)}
    started = time.monotonic()
    try:
        with contextlib.ExitStack() as cleanup:
            temporary = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="mesh-current-default-quality-"))
            isolated = Path(temporary).resolve(strict=True)
            configure_isolation(isolated)
            implementation = isolated / "implementation"
            manifest["runtime_files"] = freeze_runtime(repository, implementation)
            if args.dependency_directory:
                sys.path.insert(0, str(checked_path(args.dependency_directory, must_exist=True, directory=True)))
            sys.path.insert(0, str(implementation))
            import mesh.audit as audit_module
            import mesh.memory as memory_module
            import mesh.store as store_module
            cleanup.callback(store_module._cleanup_open_lock_fds)

            def close_owned_pools() -> None:
                # The replay process owns only connections in its named
                # temporary home, including the separate pooled audit files.
                audit_module._close_all_audit_connections()
                store_module.close_store_readers()
                for path in list(store_module._CONNECTION_POOL):
                    store_module.close_store_connection(path)

            cleanup.callback(close_owned_pools)
            if Path(store_module.__file__).resolve().parent != implementation / "mesh":
                raise ValueError("Replay imported another checkout")
            if store_module.DEFAULT_MAX_VECTOR_CACHE_BYTES != 256 * 1024 * 1024:
                raise ValueError("Production default cache budget changed")
            manifest["schema_version"] = store_module.SCHEMA_VERSION
            manifest["dependencies"] = {name: importlib.metadata.version(name) for name in ("numpy", "apsw")}
            memory_module._trigger_background_bootstrap = lambda: None
            memory_module._get_model = lambda: (memory_module._hash_embed_384, "hash-fallback")

            def no_query_inference(*_args, **_kwargs):
                raise RuntimeError("Replay unexpectedly attempted fresh query inference")

            memory_module._embed_query_with_tag = no_query_inference
            grouped_queries = defaultdict(list)
            grouped_records = defaultdict(list)
            faithful_ids = defaultdict(list)
            for query in queries:
                grouped_queries[query["namespace"]].append(query)
            for record in records:
                grouped_records[record["corpus_id"]].append(record)
                if record["faithful_public"]:
                    faithful_ids[record["acceptance"]["memory_id"]].append(record["document_id"])
            total_queries = 0
            manifest["copied_corpora"] = {}
            for namespace, group in grouped_queries.items():
                namespace = store_module.validate_namespace(namespace)
                database = safe_member(source_home, "mesh_" + namespace + ".db")
                destination = isolated / "mesh-home" / database.name
                source = store_module.sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
                target = store_module.sqlite3.connect(str(destination))
                try:
                    source.backup(target)
                finally:
                    target.close()
                    source.close()
                mesh = memory_module.Mesh(namespace=namespace, agent_id="offline-default-replay")
                try:
                    with mesh.store._read_connect() as connection:
                        rows = {row["id"]: row for row in connection.execute("SELECT * FROM memories WHERE namespace=?", (namespace,))}
                        if int(connection.execute("PRAGMA synchronous").fetchone()[0]) != 2:
                            raise ValueError("Default replay is not FULL-synchronous")
                    corpus_records = grouped_records[group[0]["corpus_id"]]
                    accepted = {record["acceptance"]["memory_id"] for record in corpus_records if record["acceptance"].get("memory_id")}
                    if set(rows) != accepted:
                        raise ValueError("Copied checkpoint changed full accepted-memory accounting")
                    for record in corpus_records:
                        identifier = record["acceptance"].get("memory_id")
                        if not identifier:
                            continue
                        row = rows[identifier]
                        body_hash = hashlib.sha256(row["content"].encode("utf-8")).hexdigest()
                        if (body_hash != record["stored_sha256"] or int(row["revision"]) != record["current_revision"]
                                or bool(row["local_only"]) != record["private"] or bool(row["quarantined"]) != record["quarantined"]
                                or row["embedding_model"] != model_tag or row["embedding"] is None or not row["incarnation"]
                                or record.get("current_incarnation", row["incarnation"]) != row["incarnation"]
                                or record.get("store_generation", mesh.store.store_generation) != mesh.store.store_generation):
                            raise ValueError("Copied content, policy, revision, model or incarnation differs")
                    for offset in range(0, len(accepted), 128):
                        batch = list(accepted)[offset:offset + 128]
                        status = mesh.wait_for_indexed(
                            batch, timeout=0, include_private=True, target_model=model_tag,
                            expected_revisions={identifier: int(rows[identifier]["revision"]) for identifier in batch},
                            expected_incarnations={identifier: rows[identifier]["incarnation"] for identifier in batch},
                            expected_store_generation=mesh.store.store_generation)
                        if status["status"] != "indexed" or set(status["indexed"]) != set(batch):
                            raise ValueError("Copied source identity-bound readiness failed")
                    if mesh.store.embedding_job_stats()["pending_count"]:
                        raise ValueError("Copied checkpoint neural indexing is incomplete")
                    manifest["copied_corpora"][namespace] = {"source_submissions": len(corpus_records), "unique_memories": len(rows),
                                                              "full_fidelity_preserved": True, "pending_embedding_jobs": 0}
                    del rows
                    for query in group:
                        for cap in (64, 96):
                            os.environ["MESH_RECALL_CANDIDATES"] = str(cap)
                            vector = query["vector"]
                            raw = mesh.store.hybrid_query(query["query"], vector, n=64,
                                                         query_embedding_model=model_tag, include_private=False, include_quarantined=False)
                            final = mesh.recall(query["query"], n=10, include_private=False, include_quarantined=False,
                                                record_access=False, _precomputed_embedding=vector, _precomputed_model_tag=model_tag)
                            rerank = mesh.recall(query["query"], n=24, include_private=False, include_quarantined=False,
                                                 record_access=False, _precomputed_embedding=vector, _precomputed_model_tag=model_tag)
                            dense = mesh.store.hybrid_query("", vector, n=64, query_embedding_model=model_tag,
                                                           include_private=False, include_quarantined=False)
                            payload = {"query_id": query["query_id"], "candidate_cap": cap, "corpus_id": query["corpus_id"]}
                            for field, results in zip(FIELDS, (raw, final, rerank, dense)):
                                if any(row.get("local_only") or row.get("quarantined") for row in results):
                                    raise ValueError("Private candidate in public replay")
                                payload[field] = compact_results(results, faithful_ids)
                                actual = [row["id"] for row in results]
                                expected = baseline[cap][query["query_id"]][field]
                                comparison = comparisons[str(cap)][field]
                                comparison["identical_ranked_queries"] += int(actual == expected)
                                comparison["top1_equal_queries"] += int(actual[:1] == expected[:1])
                                comparison["top10_equal_queries"] += int(actual[:10] == expected[:10])
                                if actual != expected:
                                    comparison["changed_query_ids"].append(query["query_id"])
                            streams[cap].write(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n")
                            streams[cap].flush()
                        total_queries += 1
                        if total_queries % 10 == 0:
                            write_json(output / "progress.json", {"completed_queries": total_queries, "query_target": len(queries),
                                                                    "cache_statistics": store_module.get_vector_cache_stats()})
                            print(json.dumps({"completed_queries": total_queries, "query_target": len(queries)}), flush=True)
                    manifest["cache_statistics"] = store_module.get_vector_cache_stats()
                finally:
                    mesh.close()
                    store_module._evict_vector_cache_for_db_path(mesh.store.db_path)
                    store_module.close_store_readers(mesh.store.db_path)
                    store_module.close_store_connection(mesh.store.db_path)
            store_module._cleanup_open_lock_fds()
            manifest["all_queries_accounted"] = total_queries == len(queries)
            if not manifest["all_queries_accounted"]:
                raise ValueError("Default replay is missing query results")
            manifest["status"] = "complete"
    except BaseException as exc:
        manifest.update(status="failed", failure_type=type(exc).__name__)
        raise
    finally:
        for stream in streams.values():
            stream.close()
        manifest["rank_comparisons"] = comparisons
        manifest["diagnostic_loaded_wall_seconds"] = time.monotonic() - started
        manifest["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
        manifest["output_sha256"] = {path.name: digest_file(path) for path in output.glob("results-*.jsonl")}
        write_json(output / "run-manifest.json", manifest)
    print(json.dumps({"status": manifest["status"], "dataset": manifest["dataset"], "queries": len(queries),
                      "cache_statistics": manifest["cache_statistics"]}), flush=True)


if __name__ == "__main__":
    main()

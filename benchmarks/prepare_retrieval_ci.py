"""Provision fixed public data and allowlisted local model files for CI only.

The downloads are static archives/weights, never cloud LLM inference. This
stdlib-only preparation command does not import Mesh or read agent profiles.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import stat
import time
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from replay_retrieval import checked_path, isolated_artifact_path, safe_member

MODEL_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
MODEL = {
    "name": "sentence-transformers/all-MiniLM-L6-v2", "revision": MODEL_REVISION,
    "onnx_sha256": "6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452",
    "tokenizer_sha256": "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037",
}
DATASETS = {
    "nfcorpus": {
        "archive_sha256": "efe5be03f8c5b86a5870102d0599d227c8c6e2484328e68c6522560385671b0b",
        "corpus_count": 3633, "query_count": 323, "positive_judgments": 12334,
        "files": {
            "corpus.jsonl": "10cc83ef1826b1425e6a87090b5140b39b27755d5a27e48215a88611c899991f",
            "queries.jsonl": "d024e6621b84925d485ae473d316a0c3af31c62c8068a59fb29d22f7613aef2a",
            "qrels/test.tsv": "f8fba6ef3d4dd9c3a242a8ba4ae38276fc3622fce7dcbae764766d564542fd2a",
            "queries-selected.jsonl": "20c4349031851471dd1353e480e6f1d2f4444a1191010a60a2a0ae584c147779",
            "labels.json": "2e779079b4053d605230d98964ebee355b3fe387c9611d98c51f2b271a325600",
        },
    },
    "scifact": {
        "archive_sha256": "536e14446a0ba56ed1398ab1055f39fe852686ecad24a6306c80c490fa8e0165",
        "corpus_count": 5183, "query_count": 300, "positive_judgments": 339,
        "files": {
            "corpus.jsonl": "dec31c8182f3d744c7d2c09423756fd1d17cbef75808db13ba01cc0aab4d1ac6",
            "queries.jsonl": "8ff84a7c903f722981cd8d595c022660140c51867b27608a6d4910db86080313",
            "qrels/test.tsv": "0864bb985e0ca2367ba217977e72004d549054b2b06666ed9d4825ac7c21284c",
            "queries-selected.jsonl": "20ded7eef653593fde34060aee46700e3f4e9d043e84004e581488aae5a85b45",
            "labels.json": "9306bf40a0ff5f3bec248a2aef0b0f0631e8ab89dcfc1da6b8fb90e3c7993857",
        },
    },
}
BOUNDED_DATASETS = ("prospective", "longtext-prospective", "longmemeval")
MODEL_BYTES = {"model.onnx": 90405214, "tokenizer.json": 466247}


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def write_json(path, value):
    path = checked_path(path, directory=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def read_jsonl(path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def verified_download(url, destination, expected_sha256, *, maximum_bytes, local_only=False):
    destination = checked_path(destination, directory=False)
    if destination.exists():
        if digest(destination) != expected_sha256:
            raise ValueError("Existing fixture does not match its pinned hash")
        return
    if local_only:
        raise ValueError("Required verified local fixture is absent")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = checked_path(destination.with_name(destination.name + ".download"), directory=False)
    if temporary.exists():
        raise ValueError("Incomplete static download already exists")
    started = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": "Mesh-fixed-retrieval-CI"})
    with urllib.request.urlopen(request, timeout=30) as response, temporary.open("xb") as output:
        size = 0
        while block := response.read(1024 * 1024):
            size += len(block)
            if size > maximum_bytes or time.monotonic() - started > 360:
                raise ValueError("Fixture exceeds its bounded download size")
            output.write(block)
        output.flush()
        os.fsync(output.fileno())
    if digest(temporary) != expected_sha256:
        raise ValueError("Downloaded fixture does not match its allowlisted hash")
    checked_path(destination, directory=False)
    if destination.exists():
        raise ValueError("Static destination appeared during provisioning")
    print(json.dumps({"verified_temporary_file": temporary.name, "bytes": size, "sha256": expected_sha256}), flush=True)
    os.replace(temporary, destination)


def prepare_dataset(name, root, *, local_only=False):
    root = checked_path(root, must_exist=True, directory=True)
    pinned = DATASETS[name]
    destination = checked_path(root / "data" / name, root=root, directory=True)
    destination.mkdir(parents=True, exist_ok=True)
    archive = checked_path(root / "archives" / (name + ".zip"), root=root, directory=False)
    url = "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/" + name + ".zip"
    verified_download(url, archive, pinned["archive_sha256"], maximum_bytes=128 * 1024 * 1024, local_only=local_only)
    expected_files = ("corpus.jsonl", "queries.jsonl", "qrels/test.tsv")
    with zipfile.ZipFile(archive) as compressed:
        if sum(entry.file_size for entry in compressed.infolist()) > 128 * 1024 * 1024:
            raise ValueError("Unsafe total archive size")
        for entry in compressed.infolist():
            path = PurePosixPath(entry.filename)
            if (path.is_absolute() or ".." in path.parts or "\\" in entry.filename or ":" in entry.filename
                    or stat.S_ISLNK(entry.external_attr >> 16) or entry.file_size > 128 * 1024 * 1024):
                raise ValueError("Unsafe or unexpectedly large archive member")
        for relative in expected_files:
            target = safe_member(destination, relative, must_exist=False)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink():
                raise ValueError("Symlinked dataset destination")
            if target.exists():
                if digest(target) != pinned["files"][relative]:
                    raise ValueError("Existing dataset member changed")
                continue
            with compressed.open(name + "/" + relative) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            if digest(target) != pinned["files"][relative]:
                raise ValueError("Extracted dataset member differs from its pinned hash")
    query_ids, qrels = set(), {}
    with (destination / "qrels/test.tsv").open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream, delimiter="\t"):
            query, document, grade = row["query-id"], row["corpus-id"], int(row["score"])
            query_ids.add(query)
            if grade > 0:
                qrels.setdefault(query, {})[document] = grade
    queries = [row for row in read_jsonl(destination / "queries.jsonl") if str(row["_id"]) in query_ids]
    if len(queries) != len(query_ids) or len(queries) != pinned["query_count"]:
        raise ValueError("Published query accounting changed")
    selected = safe_member(destination, "queries-selected.jsonl", must_exist=False)
    if selected.is_symlink():
        raise ValueError("Symlinked selected-query output")
    with selected.open("w", encoding="utf-8", newline="\n") as stream:
        for row in queries:
            stream.write(json.dumps({"id": str(row["_id"]), "query": row["text"]}, ensure_ascii=False) + "\n")
    labels = safe_member(destination, "labels.json", must_exist=False)
    if labels.is_symlink():
        raise ValueError("Symlinked relevance output")
    write_json(labels, {"qrels": qrels, "query_ids": sorted(query_ids)})
    for relative, expected_hash in pinned["files"].items():
        if digest(destination / relative) != expected_hash:
            raise ValueError("Prepared input differs from the fixed protocol")
    corpus_count = sum(1 for _ in read_jsonl(destination / "corpus.jsonl"))
    judgments = sum(len(values) for values in qrels.values())
    if corpus_count != pinned["corpus_count"] or judgments != pinned["positive_judgments"]:
        raise ValueError("Published corpus/gold accounting changed")
    write_json(destination / "manifest.json", {
        "name": name, "source_url": url, "prepared_at_utc": datetime.now(timezone.utc).isoformat(),
        "archive_sha256": pinned["archive_sha256"], "corpus_count": corpus_count, "query_count": len(queries),
        "positive_judgments": judgments, "files": {relative: {"sha256": expected, "bytes": (destination / relative).stat().st_size}
                                                   for relative, expected in pinned["files"].items()},
        "hard_negatives": "Every published document remains available; no label-selected candidate subsets.",
    })
    return destination


def prepare_bounded_dataset(name, root, fixture_directory=None):
    """Copy the already inspected fixture bytes; do not select by new scores."""
    root = checked_path(root, must_exist=True, directory=True)
    fixtures = checked_path(fixture_directory or Path(__file__).parent / "retrieval_fixtures", must_exist=True, directory=True)
    source = checked_path(fixtures / name, root=fixtures, must_exist=True, directory=True)
    source_manifest = safe_member(source, "manifest.json")
    manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    if manifest["name"] != name or manifest.get("inspection_status") != "previously_inspected":
        raise ValueError("Bounded regression provenance is missing")
    destination = checked_path(root / "data" / name, root=root, directory=True)
    destination.mkdir(parents=True, exist_ok=True)
    for relative, expected in {**manifest["files"], "manifest.json": digest(source_manifest)}.items():
        expected = expected["sha256"] if isinstance(expected, dict) else expected
        original = safe_member(source, relative)
        target = safe_member(destination, relative, must_exist=False)
        if digest(original) != expected:
            raise ValueError("Bundled regression source changed")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if digest(target) != expected:
                raise ValueError("Existing bounded regression member changed")
        else:
            shutil.copyfile(original, target)
        if digest(target) != expected:
            raise ValueError("Copied bounded regression member changed")
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", choices=(*DATASETS, *BOUNDED_DATASETS))
    parser.add_argument("--repository", type=Path, default=Path("."), help="Repository containing the allowed .work output root")
    parser.add_argument("--output-root", required=True, type=Path, help="Explicit isolated CI artifact directory")
    parser.add_argument("--model-directory", required=True, type=Path, help="Existing pair verified by benchmarks/provision_production_model.py; this command never fetches weights")
    parser.add_argument("--local-only", action="store_true", help="Verify existing inputs and refuse any download")
    args = parser.parse_args()
    repository = checked_path(args.repository, must_exist=True, directory=True)
    root = isolated_artifact_path(args.output_root, repository)
    model_directory = isolated_artifact_path(args.model_directory, repository, must_exist=True)
    root.mkdir(parents=True, exist_ok=True)
    for filename, key in (("model.onnx", "onnx_sha256"), ("tokenizer.json", "tokenizer_sha256")):
        source = safe_member(model_directory, filename)
        if source.stat().st_size != MODEL_BYTES[filename] or digest(source) != MODEL[key]:
            raise ValueError("Existing local model pair differs from the fixed pin")
    destination = (prepare_dataset(args.dataset, root, local_only=args.local_only) if args.dataset in DATASETS
                   else prepare_bounded_dataset(args.dataset, root))
    protocol = {
        "protocol_version": 1, "model": MODEL, "fusion": "calibrated", "candidate_caps": [64, 96],
        "query_mode": "neural", "privacy": "public only; quarantined excluded", "durability": "WAL and FULL on patched SQLite",
        "heldout_policy": "No model, weight or window-size tuning from these fixed test scores. Labels are opened only by offline scoring.",
        "candidate_cap_note": "C64 and C96 are fixed protocol cells; production default is evaluated separately by query-only.",
    }
    write_json(root / "protocol.json", protocol)
    prepared = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    print(json.dumps({"prepared_dataset": args.dataset, "corpus_count": prepared["corpus_count"],
                      "query_count": prepared["query_count"], "manifest_sha256": digest(destination / "manifest.json"),
                      "model_revision": MODEL_REVISION, "cloud_llm_provider_called": False}), flush=True)


if __name__ == "__main__":
    main()

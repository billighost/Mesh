"""Tiny export and verified static-cache contracts; no model runtime or downloads."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))
import production_retrieval as production
import production_runner as runner
import provision_production_model as embedding
import provision_retrieval_c4 as c4
import replay_retrieval as replay


def results():
    return [{"id": "a" * 32, "revision": 1, "incarnation": "b" * 32, "store_generation": "c" * 32,
             "embedding_model": "onnx-minilm-l6-v2", "similarity": 0.9, "rank_score": 0.8,
             "retrieval_mode": "cached", "local_only": False, "quarantined": False,
             "selected_passage_ordinal": None, "supporting_spans": []},
            {"id": "missing", "revision": 1, "local_only": False, "quarantined": False}]


def records():
    return {"a" * 32: [{"document_id": "second-alias", "faithful_public": True},
                        {"document_id": "unfaithful-source", "faithful_public": False},
                        {"document_id": "first-alias", "faithful_public": True}],
            "unused": [{"document_id": "unused-source", "faithful_public": True}]}


def test_returned_source_alias_order_and_complete_export_bytes_are_preserved():
    rows, source = results(), records()
    aliases = {identifier: [item["document_id"] for item in items if item["faithful_public"]]
               for identifier, items in source.items()}
    expected = replay.compact_results(rows, aliases)
    for row, value in zip(rows, expected):
        value.update(incarnation=row.get("incarnation"), store_generation=row.get("store_generation"),
            local_only=bool(row.get("local_only")), quarantined=bool(row.get("quarantined")),
            selected_passage_ordinal=row.get("selected_passage_ordinal"), supporting_spans=[])
    actual = production.decorate_results(rows, source)
    assert json.dumps(actual) == json.dumps(expected)
    assert actual[0]["faithful_document_ids"] == ["second-alias", "first-alias"]
    assert actual[1]["faithful_document_ids"] == []


def test_export_work_does_not_iterate_unreturned_corpus_records():
    class LookupOnly(dict):
        def items(self):
            raise AssertionError("The result exporter scanned the corpus")

        def get(self, key, default=None):
            seen.append(key)
            return super().get(key, default)

    seen = []
    actual = production.decorate_results(results(), LookupOnly(records()))
    assert seen == [row["id"] for row in results()]
    assert actual[0]["faithful_document_ids"] == ["second-alias", "first-alias"]


@pytest.mark.parametrize("flag", ["local_only", "quarantined"])
def test_private_or_quarantined_results_still_fail_before_source_lookup(flag):
    rows = results()
    rows[0][flag] = True
    with pytest.raises(RuntimeError, match="Private or quarantined"), mock.patch.object(
            production, "compact_results", side_effect=AssertionError("Private rows reached export")):
        production.decorate_results(rows, records())


def fake_embedding(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REPO", tmp_path)
    (tmp_path / "mesh").mkdir()
    directory = tmp_path / ".work/models"
    directory.mkdir(parents=True)
    payloads = {"model.onnx": b"fake embedding model", "tokenizer.json": b"fake tokenizer"}
    pins = {name: (name, len(body), hashlib.sha256(body).hexdigest()) for name, body in payloads.items()}
    monkeypatch.setattr(embedding, "FILES", pins)
    (tmp_path / "mesh/memory.py").write_text(
        f"_ALLOWED_MODEL_SHA256 = {{{pins['model.onnx'][2]!r}}}\n"
        f"_ALLOWED_TOKENIZER_SHA256 = {{{pins['tokenizer.json'][2]!r}}}\n", encoding="utf-8")
    for name, body in payloads.items():
        (directory / name).write_bytes(body)
    monkeypatch.setattr(sys, "argv", ["provision-model", "--directory", str(directory)])
    return directory


def test_complete_cached_embedding_pair_is_verified_without_network(tmp_path, monkeypatch):
    fake_embedding(tmp_path, monkeypatch)
    with mock.patch.object(embedding.urllib.request, "urlopen", side_effect=AssertionError("Network reached")) as network:
        assert embedding.main() == 0
    network.assert_not_called()


@pytest.mark.parametrize("filename", ["model.onnx", "tokenizer.json"])
def test_modified_cached_embedding_file_is_terminal_before_network(tmp_path, monkeypatch, filename):
    directory = fake_embedding(tmp_path, monkeypatch)
    original = (directory / filename).read_bytes()
    (directory / filename).write_bytes(b"x" * len(original))
    with mock.patch.object(embedding.urllib.request, "urlopen", side_effect=AssertionError("Network reached")) as network:
        with pytest.raises(runner.ControlError, match="CachedFixtureDigestMismatch"):
            embedding.main()
    network.assert_not_called()


def fake_c4(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    directory = tmp_path / "models"
    directory.mkdir()
    model = dict(c4.MODEL)
    for kind, body in (("model", b"fake C4 weights"), ("tokenizer", b"fake C4 tokenizer")):
        filename = model[kind + "_file"]
        model[kind + "_bytes"] = len(body)
        model[kind + "_sha256"] = hashlib.sha256(body).hexdigest()
        (directory / filename).write_bytes(body)
    monkeypatch.setattr(c4, "MODEL", model)
    return directory


def test_complete_cached_c4_pair_is_verified_without_network(tmp_path, monkeypatch):
    directory = fake_c4(tmp_path, monkeypatch)
    with mock.patch.object(c4.urllib.request, "urlopen", side_effect=AssertionError("Network reached")) as network:
        actual = c4.provision(directory, replay)
    assert actual == {c4.MODEL[kind + "_file"]: c4.MODEL[kind + "_sha256"] for kind in ("model", "tokenizer")}
    network.assert_not_called()


@pytest.mark.parametrize("filename", ["model.onnx", "tokenizer.json"])
def test_modified_cached_c4_file_is_terminal_before_network(tmp_path, monkeypatch, filename):
    directory = fake_c4(tmp_path, monkeypatch)
    original = (directory / filename).read_bytes()
    (directory / filename).write_bytes(b"x" * len(original))
    with mock.patch.object(c4.urllib.request, "urlopen", side_effect=AssertionError("Network reached")) as network:
        with pytest.raises(ValueError, match="CachedC4FixtureHashMismatch"):
            c4.provision(directory, replay)
    network.assert_not_called()

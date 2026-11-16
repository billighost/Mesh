"""Model-free bounds on reranker preparation and authorization work."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import re
from types import SimpleNamespace

import numpy as np
import pytest

from mesh import local_rerank


class Encoding:
    def __init__(self, ids, offsets=()):
        self._ids = ids
        self.offsets = offsets
        self.attention_mask = [1] * len(ids)
        self.type_ids = [0] * len(ids)
        self.id_reads = 0

    @property
    def ids(self):
        self.id_reads += 1
        return list(self._ids)


class Tokenizer:
    def __init__(self):
        self.calls = Counter()
        self.vocabulary = {}
        self.pairs = []
        self.pair_encodings = []

    def tokens(self, text):
        offsets = [match.span() for match in re.finditer(r"\S+", text)]
        ids = [self.vocabulary.setdefault(text[start:end].casefold(), len(self.vocabulary) + 1)
               for start, end in offsets]
        return ids, offsets

    def encode(self, text, *, add_special_tokens=False):
        assert not add_special_tokens
        self.calls[text] += 1
        return Encoding(*self.tokens(text))

    def enable_padding(self, **kwargs):
        assert kwargs == {"pad_id": 0, "pad_token": "[PAD]"}

    def no_padding(self):
        pass

    def encode_batch(self, pairs):
        self.pairs = pairs
        ids = [[101, *self.tokens(query)[0], 102, *self.tokens(passage)[0], 102]
               for query, passage in pairs]
        width = max(map(len, ids))
        self.pair_encodings = [Encoding(row + [0] * (width - len(row))) for row in ids]
        return self.pair_encodings


def candidate(index, content="Unchanged original evidence."):
    return {"id": f"parent-{index}", "revision": 1, "incarnation": "a" * 32,
            "store_generation": "b" * 32, "content": content,
            "supporting_spans": [], "selected_passage_ordinal": None}


@pytest.mark.parametrize("length", [3, 40])
def test_compact_query_reuses_the_checked_prefix_encoding(length):
    tokenizer = Tokenizer()
    engine = object.__new__(local_rerank._LocalCrossEncoder)
    engine.np, engine.tokenizer = np, tokenizer
    engine.names = {"input_ids", "attention_mask", "token_type_ids"}
    query = " ".join(f"query-{index}" for index in range(length))
    source = " ".join(["noise"] * 60 + ["query-0"] + ["tail"] * 60)
    arrays, accounts = engine.prepare(query, [candidate(0, source), candidate(1)], profile="compact-80")
    prefix = " ".join(query.split()[:29])

    assert tokenizer.calls[query] == 1
    assert tokenizer.calls[prefix] == 1
    assert all(item.id_reads == 1 for item in tokenizer.pair_encodings)
    assert tokenizer.pairs[0][0] == prefix
    assert "query-0" in tokenizer.pairs[0][1].split()
    assert arrays["input_ids"].shape[0] == 2
    assert arrays["input_ids"].shape[1] <= 80
    assert accounts[0]["query_tokens"] == length
    assert accounts[0]["kept_query_tokens"] == min(29, length)
    assert source[accounts[0]["start"]:accounts[0]["end"]] == tokenizer.pairs[0][1]


@pytest.mark.parametrize("changed", [None, "head-prepare", "tail-prepare", "tail-inference", "head-inference"])
def test_only_model_inputs_need_preinference_revalidation_and_every_return_is_guarded(monkeypatch, changed):
    rows = [candidate(index) for index in range(32)]
    original = deepcopy(rows)
    blocked = set()
    observations = []
    inference_calls = []

    def guard(values):
        observations.append([row["id"] for row in values])
        return [row for row in values if row["id"] not in blocked]

    def prepare(query, head, **kwargs):
        assert len(head) == local_rerank.PARENT_CAP
        if changed == "head-prepare":
            blocked.add("parent-0")
        elif changed == "tail-prepare":
            blocked.add("parent-10")
        return {}, [{} for _ in head]

    def score(arrays, count):
        inference_calls.append(count)
        if changed == "head-inference":
            blocked.add("parent-0")
        elif changed == "tail-inference":
            blocked.add("parent-10")
        return list(range(count))

    monkeypatch.setenv("MESH_RERANK_PROFILE", "compact-80")
    monkeypatch.setattr(local_rerank, "_get_engine", lambda: SimpleNamespace(prepare=prepare, score=score))
    result = local_rerank._rerank_locked("query", rows, guard)

    assert rows == original
    assert [len(values) for values in observations] == [32, 32, 4, 32]
    assert all(row["id"] not in blocked for row in result)
    assert inference_calls == ([] if changed == "head-prepare" else [4])
    if changed == "head-prepare":
        assert [row["id"] for row in result] == [row["id"] for row in rows[1:]]
        assert all(row["rerank_status"] == "candidates_changed" for row in result)
    else:
        expected = ["parent-3", "parent-2", "parent-1", "parent-0"]
        expected += [row["id"] for row in rows[4:]]
        assert [row["id"] for row in result] == [mid for mid in expected if mid not in blocked]
        assert all(row["rerank_status"] == "outside_parent_cap" for row in result if int(row["id"].split("-")[1]) >= 4)

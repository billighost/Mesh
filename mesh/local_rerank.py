"""Optional, bounded local C4 reranking. Model files are supplied and verified offline."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import os
from pathlib import Path
import stat
import threading
from typing import Any, Callable


MODEL = {
    "repository": "Xenova/ms-marco-MiniLM-L-6-v2",
    "revision": "a09144355adeed5f58c8ed011d209bf8ee5a1fec",
    "model_file": "model.onnx",
    "model_remote": "onnx/model_quantized.onnx",
    "model_bytes": 23143499,
    "model_sha256": "e9d8ebf845c413e981c175bfe49a3bfa9b3dcce2a3ba54875ee5df5a58639fbe",
    "tokenizer_file": "tokenizer.json",
    "tokenizer_remote": "tokenizer.json",
    "tokenizer_bytes": 711396,
    "tokenizer_sha256": "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
}
PARENT_CAP = 4
QUERY_TOKENS = 29
PASSAGE_TOKENS = 224
PAIR_TOKENS = 256
INPUT_PROFILES = {"full-256": (PASSAGE_TOKENS, PAIR_TOKENS),
                  "compact-128": (96, 128), "compact-96": (64, 96)}

# One gate covers cold loading, tokenizer mutation, and inference across namespaces.
# Busy recalls keep their existing order instead of building an unbounded queue.
_GATE = threading.Lock()
_engine = None
_engine_key = None


class RerankUnavailable(RuntimeError):
    """Only fixed, content-free failure codes reach callers."""


def _fingerprint(metadata):
    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


def _checked_paths(directory):
    if not directory:
        raise RerankUnavailable("model_not_configured")
    path = Path(directory).absolute()
    if ".." in path.parts:
        raise RerankUnavailable("model_path_invalid")
    try:
        for ancestor in reversed((path, *path.parents)):
            metadata = ancestor.lstat()
            if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
                raise RerankUnavailable("model_path_linked")
            if not stat.S_ISDIR(metadata.st_mode):
                raise RerankUnavailable("model_path_invalid")
        paths, state = [], []
        for kind in ("model", "tokenizer"):
            member = path / MODEL[kind + "_file"]
            metadata = member.lstat()
            if (not stat.S_ISREG(metadata.st_mode)
                    or getattr(metadata, "st_file_attributes", 0) & 0x400):
                raise RerankUnavailable("model_path_linked")
            if metadata.st_size != MODEL[kind + "_bytes"]:
                raise RerankUnavailable("model_size_mismatch")
            paths.append(member)
            state.append(_fingerprint(metadata))
        return paths, (str(path), tuple(state))
    except OSError:
        raise RerankUnavailable("model_unavailable") from None


def _read_assets(directory):
    paths, key = _checked_paths(directory)
    data = []
    try:
        for kind, path in zip(("model", "tokenizer"), paths):
            with path.open("rb") as handle:
                before = os.fstat(handle.fileno())
                if before.st_size != MODEL[kind + "_bytes"]:
                    raise RerankUnavailable("model_changed")
                body = handle.read(MODEL[kind + "_bytes"] + 1)
                if (len(body) != MODEL[kind + "_bytes"]
                        or hashlib.sha256(body).hexdigest() != MODEL[kind + "_sha256"]):
                    raise RerankUnavailable("model_hash_mismatch")
                # Compare descriptor metadata to itself: Windows may report path
                # and descriptor timestamps differently after a recent write.
                if _fingerprint(os.fstat(handle.fileno())) != _fingerprint(before):
                    raise RerankUnavailable("model_changed")
            data.append(body)
        if _checked_paths(directory)[1] != key:
            raise RerankUnavailable("model_changed")
        return data, key
    except OSError:
        raise RerankUnavailable("model_unavailable") from None


def verify_model_files(directory):
    """Verify both pinned files without importing a tokenizer or model runtime."""
    _read_assets(directory)


def _bounded_prefix(text, tokenizer, maximum):
    encoded = tokenizer.encode(text, add_special_tokens=False)
    count = len(encoded.ids)
    if count <= maximum:
        return text, count, count
    end = encoded.offsets[maximum - 1][1]
    prefix = text[:end]
    kept = len(tokenizer.encode(prefix, add_special_tokens=False).ids)
    if not prefix.strip() or kept > maximum:
        raise RerankUnavailable("tokenizer_prefix_invalid")
    return prefix, count, kept


def _profile_limits(profile):
    if not isinstance(profile, str) or profile not in INPUT_PROFILES:
        raise RerankUnavailable("profile_invalid")
    return INPUT_PROFILES[profile]


def _bounded_excerpt(text, tokenizer, maximum, query_ids):
    encoded = tokenizer.encode(text, add_special_tokens=False)
    # Encoding properties materialize lists; retain each once before scanning.
    ids = encoded.ids
    count = len(ids)
    if count <= maximum:
        return text, count, count, 0
    matches = Counter(token for token in ids[:maximum] if token in query_ids)
    hits = sum(matches.values())
    positions = sum(index for index, token in enumerate(ids[:maximum]) if token in query_ids)

    def score(start):
        # Balance context around matches; exact ties retain the earliest source window.
        distance = abs(2 * positions - (2 * start + maximum - 1) * hits)
        return len(matches), hits, -distance

    best_start, best_score = 0, score(0)
    for index in range(maximum, count):
        removed = index - maximum
        token = ids[removed]
        if token in query_ids:
            matches[token] -= 1
            if not matches[token]:
                del matches[token]
            hits -= 1
            positions -= removed
        token = ids[index]
        if token in query_ids:
            matches[token] += 1
            hits += 1
            positions += index
        start = removed + 1
        current = score(start)
        if current > best_score:
            best_start, best_score = start, current
    offsets = encoded.offsets
    start = 0 if best_start == 0 else offsets[best_start][0]
    end = offsets[best_start + maximum - 1][1]
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
        raise RerankUnavailable("tokenizer_excerpt_invalid")
    # A wordpiece at the new boundary can tokenize differently in isolation.
    # Recheck the verbatim substring rather than relying on silent pair truncation.
    excerpt, _count, kept = _bounded_prefix(text[start:end], tokenizer, maximum)
    if not excerpt.strip():
        raise RerankUnavailable("tokenizer_excerpt_invalid")
    return excerpt, count, kept, start


def _source(row):
    body = row.get("content")
    identity = (row.get("id"), row.get("revision"), row.get("incarnation"), row.get("store_generation"))
    if (not isinstance(body, str) or not body.strip() or not isinstance(identity[0], str)
            or type(identity[1]) is not int or identity[1] < 1
            or not all(isinstance(value, str) and value for value in identity[2:])):
        raise RerankUnavailable("candidate_identity_invalid")
    ordinal = row.get("selected_passage_ordinal")
    if ordinal is None:
        return body, "accepted_original_body", 0
    spans = row.get("supporting_spans", [])
    if type(ordinal) is not int or ordinal < 0 or len(spans) != 1:
        raise RerankUnavailable("selected_span_invalid")
    span = spans[0]
    if any(span.get(key) != value for key, value in zip(
            ("memory_id", "revision", "incarnation", "store_generation"), identity)):
        raise RerankUnavailable("selected_span_invalid")
    start, end = span.get("start"), span.get("end")
    if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(body)
            or span.get("text") != body[start:end]):
        raise RerankUnavailable("selected_span_invalid")
    return body[start:end], "exact_selected_source_span", start


class _LocalCrossEncoder:
    def __init__(self, model_bytes, tokenizer_bytes):
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer

            self.np = np
            self.tokenizer = Tokenizer.from_str(tokenizer_bytes.decode("utf-8"))
            self.tokenizer.no_truncation()
            self.tokenizer.no_padding()
            if self.tokenizer.num_special_tokens_to_add(is_pair=True) != 3:
                raise RerankUnavailable("model_interface_invalid")
            options = ort.SessionOptions()
            options.intra_op_num_threads = options.inter_op_num_threads = 1
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            options.log_severity_level = 3
            options.add_session_config_entry("session.intra_op.allow_spinning", "0")
            options.add_session_config_entry("session.inter_op.allow_spinning", "0")
            # Loading verified bytes prevents a path replacement between hashing and ORT.
            self.session = ort.InferenceSession(model_bytes, sess_options=options, providers=["CPUExecutionProvider"])
            entries, outputs = self.session.get_inputs(), self.session.get_outputs()
            self.names = {entry.name for entry in entries}
            if (not {"input_ids", "attention_mask"}.issubset(self.names)
                    or not self.names.issubset({"input_ids", "attention_mask", "token_type_ids"})
                    or any(entry.type != "tensor(int64)" for entry in entries)
                    or len(outputs) != 1 or outputs[0].type != "tensor(float)"
                    or self.session.get_providers() != ["CPUExecutionProvider"]):
                raise RerankUnavailable("model_interface_invalid")
        except RerankUnavailable:
            raise
        except ImportError:
            raise RerankUnavailable("runtime_unavailable") from None
        except Exception:
            raise RerankUnavailable("model_load_failed") from None

    def prepare(self, query, candidates, *, profile="full-256"):
        passage_limit, pair_limit = _profile_limits(profile)
        if not 1 <= len(candidates) <= PARENT_CAP:
            raise RerankUnavailable("candidate_cap_exceeded")
        query_text, query_count, kept_query = _bounded_prefix(query, self.tokenizer, QUERY_TOKENS)
        query_ids = set(self.tokenizer.encode(query_text, add_special_tokens=False).ids) if profile != "full-256" else None
        pairs, accounting = [], []
        for row in candidates:
            source, mode, start = _source(row)
            if profile == "full-256":
                passage, count, kept = _bounded_prefix(source, self.tokenizer, passage_limit)
                if mode == "exact_selected_source_span" and count != kept:
                    raise RerankUnavailable("selected_span_too_long")
            else:
                passage, count, kept, relative_start = _bounded_excerpt(source, self.tokenizer, passage_limit, query_ids)
                start += relative_start
                if count != kept:
                    mode = ("verbatim_selected_source_excerpt" if mode == "exact_selected_source_span"
                            else "verbatim_original_body_excerpt")
            if not passage.strip():
                raise RerankUnavailable("selected_span_empty")
            pairs.append((query_text, passage))
            accounting.append({"source": mode, "start": start, "end": start + len(passage),
                               "query_tokens": query_count, "kept_query_tokens": kept_query,
                               "removed_query_characters": len(query) - len(query_text),
                               "passage_tokens": count, "kept_passage_tokens": kept,
                               "removed_passage_characters": len(source) - len(passage)})
            if profile != "full-256":
                accounting[-1]["profile"] = profile
        try:
            self.tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
            encoded = self.tokenizer.encode_batch(pairs)
        finally:
            self.tokenizer.no_padding()
        if len(encoded) != len(candidates) or any(len(item.ids) > pair_limit for item in encoded):
            raise RerankUnavailable("pair_token_cap_exceeded")
        for item, account in zip(encoded, accounting):
            account["pair_tokens"] = len(item.ids)
        arrays = {"input_ids": self.np.asarray([item.ids for item in encoded], dtype=self.np.int64),
                  "attention_mask": self.np.asarray([item.attention_mask for item in encoded], dtype=self.np.int64),
                  "token_type_ids": self.np.asarray([item.type_ids for item in encoded], dtype=self.np.int64)}
        return {name: arrays[name] for name in self.names}, accounting

    def score(self, arrays, count):
        try:
            outputs = self.session.run(None, arrays)
            if len(outputs) != 1:
                raise RerankUnavailable("model_output_invalid")
            logits = self.np.asarray(outputs[0])
            if logits.shape == (count, 1):
                logits = logits[:, 0]
            if logits.shape != (count,) or not self.np.isfinite(logits).all():
                raise RerankUnavailable("model_output_invalid")
            return [float(value) for value in logits]
        except RerankUnavailable:
            raise
        except Exception:
            raise RerankUnavailable("inference_failed") from None


def _get_engine():
    global _engine, _engine_key
    directory = os.environ.get("MESH_RERANK_MODEL_DIR", "").strip()
    _, key = _checked_paths(directory)
    if _engine is None or key != _engine_key:
        _engine = _engine_key = None
        data, key = _read_assets(directory)
        _engine = _LocalCrossEncoder(*data)
        _engine_key = key
    from .memory import _touch_model_usage

    _touch_model_usage()
    return _engine


def is_loaded():
    """Let the existing idle watchdog inspect residency without acquiring the gate."""
    return _engine is not None


def unload_reranker():
    """Release the optional session when idle; never block an active recall."""
    global _engine, _engine_key
    if not _GATE.acquire(blocking=False):
        return False
    try:
        loaded = _engine is not None
        _engine = _engine_key = None
        return loaded
    finally:
        _GATE.release()


def _fallback(candidates, code):
    return [dict(row, rerank_status=code) for row in candidates]


def rerank(query: str, candidates: list[dict[str, Any]], *,
           revalidate: Callable[[list[dict[str, Any]]], list[dict[str, Any]]]):
    """Reorder at most four authorized parents; keep scores, IDs and evidence intact."""
    if not _GATE.acquire(blocking=False):
        return _fallback(revalidate(candidates), "busy")
    try:
        from .memory import _using_model

        with _using_model():
            return _rerank_locked(query, candidates, revalidate)
    finally:
        _GATE.release()


def _rerank_locked(query, candidates, revalidate):
    guarded = revalidate(candidates)
    if len(guarded) < 2:
        return _fallback(guarded, "insufficient_candidates")
    try:
        profile = os.environ.get("MESH_RERANK_PROFILE", "full-256")
        _profile_limits(profile)
        engine = _get_engine()
    except RerankUnavailable as exc:
        return _fallback(revalidate(guarded), str(exc))
    guarded = revalidate(guarded)
    head = guarded[:PARENT_CAP]
    if len(head) < 2:
        return _fallback(guarded, "insufficient_candidates")
    try:
        if len({row.get("id") for row in head}) != len(head):
            raise RerankUnavailable("candidate_identity_invalid")
        if profile == "full-256":
            arrays, accounting = engine.prepare(query, head)
        else:
            arrays, accounting = engine.prepare(query, head, profile=profile)
    except RerankUnavailable as exc:
        return _fallback(revalidate(guarded), str(exc))
    except Exception:
        return _fallback(revalidate(guarded), "tokenization_failed")
    current = revalidate(guarded)
    if current[:len(head)] != head:
        return _fallback(current, "candidates_changed")
    try:
        scores = engine.score(arrays, len(head))
    except RerankUnavailable as exc:
        return _fallback(revalidate(current), str(exc))
    ranked = [dict(row, rerank_status="applied", rerank_score=score,
                   rerank_original_rank=index + 1, rerank_input=account)
              for index, (row, score, account) in enumerate(zip(head, scores, accounting))]
    ranked.sort(key=lambda row: (-row["rerank_score"], row["rerank_original_rank"]))
    tail = _fallback(current[len(head):], "outside_parent_cap")
    return revalidate(ranked + tail)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Verify the pinned optional local reranker files; no downloads.")
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify", help="Verify model.onnx and tokenizer.json offline, without loading a model")
    verify.add_argument("directory", help="Directory containing the two pinned local files")
    args = parser.parse_args(argv)
    try:
        verify_model_files(args.directory)
    except RerankUnavailable as exc:
        parser.exit(1, str(exc) + "\n")
    print("Pinned local reranker files verified.")


if __name__ == "__main__":
    main()

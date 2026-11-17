"""Content-free passage query observations; interrupted exports remain unscored."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re

from replay_retrieval import digest_file, safe_member

CACHE_FIELDS = ("limit_bytes", "charged_bytes", "cached_generations",
                "live_generations_and_builds", "oversized_versions")
RESOURCE_FIELDS = ("query_ordinal", "elapsed_ms", "process_cpu_ms", "sampled_owned_process_rss_bytes")
MODE_FIELDS = ("cached", "streaming", "unreported")
PROVENANCE_FIELDS = ("runtime_files", "model_file_sha256", "dataset_manifest_sha256",
                     "protocol_sha256", "source_run_manifest_sha256")


def cache_projection(value):
    if (not isinstance(value, dict) or set(value) != set(CACHE_FIELDS)
            or any(type(value[key]) is not int or value[key] < 0 for key in CACHE_FIELDS)):
        raise ValueError("Invalid numeric retrieval cache observation")
    return {key: value[key] for key in CACHE_FIELDS}


def query_observation(results, cache_stats):
    counts = dict.fromkeys(MODE_FIELDS, 0)
    for row in results:
        mode = row.get("retrieval_mode")
        counts[mode if mode in MODE_FIELDS[:2] else "unreported"] += 1
    return {"result_mode_counts": counts, "cache_stats": cache_projection(cache_stats)}


def checkpoint_manifest(path, metadata):
    # A killed benchmark must not leave a half-written provenance checkpoint.
    temporary = path.with_name("run-manifest.checkpoint.json")
    temporary.write_text(json.dumps(metadata, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def query_resources_projection(rows, query_target, *, complete, version=None):
    if (type(query_target) is not int or query_target <= 0 or type(complete) is not bool
            or version is not None and (type(version) is not int or version != 1)
            or len(rows) > query_target or complete and len(rows) != query_target):
        raise ValueError("Invalid retrieval query resource accounting")
    total_modes, query_modes = dict.fromkeys(MODE_FIELDS, 0), dict.fromkeys((*MODE_FIELDS, "mixed", "empty"), 0)
    maxima = dict.fromkeys(CACHE_FIELDS, 0)
    last = None
    for ordinal, row in enumerate(rows, 1):
        expected = set(RESOURCE_FIELDS) | ({"result_mode_counts", "cache_stats"} if version == 1 else set())
        if (not isinstance(row, dict) or set(row) != expected
                or type(row["query_ordinal"]) is not int or row["query_ordinal"] != ordinal
                or any(type(row[key]) not in {int, float} or not math.isfinite(row[key]) or row[key] < 0
                       for key in RESOURCE_FIELDS[1:]) or row["sampled_owned_process_rss_bytes"] <= 0):
            raise ValueError("Invalid retrieval query resource row")
        if version == 1:
            counts = row["result_mode_counts"]
            if (not isinstance(counts, dict) or set(counts) != set(MODE_FIELDS)
                    or any(type(counts[key]) is not int or counts[key] < 0 for key in MODE_FIELDS)
                    or sum(counts.values()) > 10):
                raise ValueError("Invalid retrieval query mode accounting")
            active = [key for key in MODE_FIELDS if counts[key]]
            query_modes["empty" if not active else active[0] if len(active) == 1 else "mixed"] += 1
            for key in MODE_FIELDS:
                total_modes[key] += counts[key]
            last = cache_projection(row["cache_stats"])
            for key in CACHE_FIELDS:
                maxima[key] = max(maxima[key], last[key])
    resource = {"query_count": len(rows), "elapsed_ms_total": sum(row["elapsed_ms"] for row in rows),
                "elapsed_ms_max": max((row["elapsed_ms"] for row in rows), default=0),
                "process_cpu_ms_total": sum(row["process_cpu_ms"] for row in rows),
                "sampled_owned_process_rss_bytes_max": max((row["sampled_owned_process_rss_bytes"] for row in rows), default=0)}
    if any(not math.isfinite(value) for value in resource.values()):
        raise ValueError("Retrieval resource totals are not finite")
    telemetry = None if version is None else {"version": 1, "query_mode_counts": query_modes,
        "result_mode_counts": total_modes, "cache_stats_max": maxima, "cache_stats_last": last}
    return resource, telemetry


def cache_observations_projection(value):
    if not isinstance(value, dict):
        raise ValueError("Invalid retrieval cache observations")
    # Corpus keys can identify source fixtures. Only numeric observations leave the worker.
    return [cache_projection(item) for item in value.values()]


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Invalid retrieval progress provenance")
    return value


def _expected_provenance(expected):
    if (not isinstance(expected, dict) or set(expected) != {*PROVENANCE_FIELDS, "query_target"}
            or type(expected["query_target"]) is not int or expected["query_target"] <= 0):
        raise ValueError("Invalid retrieval progress expectation")
    sources, model = expected["runtime_files"], expected["model_file_sha256"]
    if (not isinstance(sources, dict) or not sources
            or any(not isinstance(name, str) or not re.fullmatch(r"mesh/(?:[A-Za-z0-9_]+/)*[A-Za-z0-9_]+\.py", name)
                   for name in sources) or not isinstance(model, dict) or set(model) != {"onnx_sha256", "tokenizer_sha256"}):
        raise ValueError("Invalid retrieval progress source or model")
    for value in (*sources.values(), *model.values(), *(expected[key] for key in PROVENANCE_FIELDS[2:])):
        _digest(value)
    return {key: expected[key] for key in PROVENANCE_FIELDS}


def progress_expectation(source, source_manifest_sha256):
    expected = {key: source[key] for key in (*PROVENANCE_FIELDS[:-1], "query_target")}
    expected["source_run_manifest_sha256"] = source_manifest_sha256
    _expected_provenance(expected)
    return expected


def checkpoint_query_provenance(path, metadata):
    expected = {key: metadata[key] for key in (*PROVENANCE_FIELDS, "query_target")}
    _expected_provenance(expected)
    observations = cache_observations_projection(metadata.get("cache_observations", {}))
    checkpoint_manifest(path, expected | {"status": "running", "labels_in_ranking_inputs": False,
        "passage_index_enabled": True, "query_telemetry_version": 1,
        "cache_observations": {str(index): item for index, item in enumerate(observations)}})


def progress_from_run(directory, expected):
    provenance = _expected_provenance(expected)
    manifest_path = safe_member(directory, "run-manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get("status") not in {"running", "failed", "complete"}
            or manifest.get("labels_in_ranking_inputs") is not False or manifest.get("passage_index_enabled") is not True
            or any(manifest.get(key) != expected[key] for key in (*PROVENANCE_FIELDS, "query_target"))):
        raise ValueError("Retrieval progress is not bound to its expected source")
    path = safe_member(directory, "query-resources.jsonl")
    raw = path.read_bytes()
    if len(raw) > min(8 * 1024**2, (expected["query_target"] + 1) * 2048):
        raise ValueError("Retrieval progress export exceeds its declared query bound")
    # Only newline-terminated records are accounted after abrupt process death.
    pieces = raw.split(b"\n")
    tail_ignored = bool(pieces[-1])
    rows = [json.loads(line) for line in pieces[:-1]]
    resources, telemetry = query_resources_projection(rows, expected["query_target"], complete=False,
                                                      version=manifest.get("query_telemetry_version"))
    result = {"status": "unscored_progress", "query_target": expected["query_target"],
              "complete_query_resource_export": len(rows) == expected["query_target"] and not tail_ignored,
              "unterminated_tail_ignored": tail_ignored,
              "provenance": provenance | {"run_manifest_sha256": digest_file(manifest_path),
                                           "query_resources_sha256": hashlib.sha256(raw).hexdigest()},
              "query_resources": resources,
              "cache_observations": cache_observations_projection(manifest.get("cache_observations", {}))}
    if telemetry is not None:
        result["query_telemetry"] = telemetry
    return project_progress(result, expected)


def matched_progress_after_stop(directory, expected):
    try:
        manifest = json.loads(safe_member(directory, "run-manifest.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("Invalid optional native parent manifest")
    except (ValueError, OSError, KeyError, TypeError):
        manifest = {}
    progress = manifest.get("matched_query_progress", {})
    if not isinstance(progress, dict) or set(progress) - {"baseline", "c4"}:
        raise ValueError("Invalid native matched query progress")
    projected = {name: project_progress(item, expected) for name, item in progress.items()}
    for name in ("baseline", "c4"):
        if name in projected:
            continue
        try:
            projected[name] = progress_from_run(directory / "query-progress" / name, expected)
        except (ValueError, OSError, KeyError, TypeError):
            pass
    return projected


def project_progress(value, expected):
    provenance = _expected_provenance(expected)
    try:
        keys = {"status", "query_target", "complete_query_resource_export", "unterminated_tail_ignored",
                "provenance", "query_resources", "cache_observations"}
        if (not isinstance(value, dict) or set(value) not in (keys, keys | {"query_telemetry"})
                or value["status"] != "unscored_progress" or type(value["query_target"]) is not int
                or value["query_target"] != expected["query_target"]
                or type(value["complete_query_resource_export"]) is not bool or type(value["unterminated_tail_ignored"]) is not bool
                or set(value["provenance"]) != {*PROVENANCE_FIELDS, "run_manifest_sha256", "query_resources_sha256"}
                or any(value["provenance"][key] != provenance[key] for key in PROVENANCE_FIELDS)):
            raise ValueError("Invalid public retrieval progress")
        for key in ("run_manifest_sha256", "query_resources_sha256"):
            _digest(value["provenance"][key])
        resource = value["query_resources"]
        resource_keys = {"query_count", "elapsed_ms_total", "elapsed_ms_max", "process_cpu_ms_total", "sampled_owned_process_rss_bytes_max"}
        if (set(resource) != resource_keys or type(resource["query_count"]) is not int
                or not 0 <= resource["query_count"] <= expected["query_target"]
                or any(type(resource[key]) not in {int, float} or not math.isfinite(resource[key]) or resource[key] < 0 for key in resource_keys - {"query_count"})
                or resource["elapsed_ms_max"] > resource["elapsed_ms_total"]
                or value["complete_query_resource_export"] != (resource["query_count"] == expected["query_target"] and not value["unterminated_tail_ignored"])):
            raise ValueError("Invalid public retrieval resource counters")
        if not isinstance(value["cache_observations"], list) or len(value["cache_observations"]) > expected["query_target"]:
            raise ValueError("Invalid public retrieval cache counters")
        for item in value["cache_observations"]:
            cache_projection(item)
        if "query_telemetry" in value:
            telemetry = value["query_telemetry"]
            if (set(telemetry) != {"version", "query_mode_counts", "result_mode_counts", "cache_stats_max", "cache_stats_last"}
                    or type(telemetry["version"]) is not int or telemetry["version"] != 1):
                raise ValueError("Invalid public retrieval query telemetry")
            for name, names in (("query_mode_counts", (*MODE_FIELDS, "mixed", "empty")), ("result_mode_counts", MODE_FIELDS)):
                counts = telemetry[name]
                if set(counts) != set(names) or any(type(counts[key]) is not int or counts[key] < 0 for key in names):
                    raise ValueError("Invalid public retrieval mode counters")
            if (sum(telemetry["query_mode_counts"].values()) != resource["query_count"]
                    or not resource["query_count"] - telemetry["query_mode_counts"]["empty"] + telemetry["query_mode_counts"]["mixed"]
                        <= sum(telemetry["result_mode_counts"].values()) <= 10 * (resource["query_count"] - telemetry["query_mode_counts"]["empty"])
                    or any(telemetry["result_mode_counts"][key] < telemetry["query_mode_counts"][key] for key in MODE_FIELDS)):
                raise ValueError("Invalid public retrieval mode totals")
            cache_projection(telemetry["cache_stats_max"])
            if telemetry["cache_stats_last"] is not None:
                cache_projection(telemetry["cache_stats_last"])
                if any(telemetry["cache_stats_last"][key] > telemetry["cache_stats_max"][key] for key in CACHE_FIELDS):
                    raise ValueError("Invalid public retrieval cache maxima")
            if (telemetry["cache_stats_last"] is None) != (resource["query_count"] == 0):
                raise ValueError("Invalid public retrieval last observation")
        return value
    except (KeyError, TypeError, AttributeError, OverflowError):
        raise ValueError("Invalid public retrieval progress") from None

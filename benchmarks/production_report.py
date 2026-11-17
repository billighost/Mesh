"""Render a short candidate RESULTS addition from the sanitized numerical ledger."""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import production_ledger as ledger
import production_runner as runner


def metric(record: dict, field: str, digits: int = 1) -> str:
    if record["status"] != "completed":
        return f'{record["status"]}({record["reason"]})'
    value = record["result"]["timing"].get(field)
    return f"{value:,.{digits}f}" if type(value) in (int, float) else "unobserved"


def render(value: dict) -> str:
    primary = [record for record in value["cells"] if record["cell"]["phase"] == "primary"]
    scale = [record for record in value["cells"] if record["cell"]["phase"] == "scale"]
    if len(primary) != 48 or any(record["status"] in {"planned", "dispatching"} for record in value["cells"]):
        raise runner.ControlError("FinishRegisteredCellsBeforeResultsRendering")
    versions = value["runtime"]
    date = value["created_utc"][:10]
    filename = "production_performance_" + date.replace("-", "") + ".json"
    native_versions = sorted({version for record in primary if "result" in record
                              for version in record["result"]["native_runtime"]["sqlite_versions"]})
    total_ram = value["hardware"]["total_ram_bytes"] / (1024 ** 3)
    limit = value["declared_resource_limits"]["max_tree_rss_bytes"] / (1024 ** 3)
    lines = [
        f'### Windows production matrix — Mesh {versions["mesh_version"]} ({date})', "",
        f'A preregistered single serial pass on Windows {versions["release"]}, {value["hardware"]["logical_cpus"]} logical CPUs, '
        f'{total_ram:.2f} GiB RAM; Python {versions["python_version"]}, APSW {versions["packages"]["apsw"]}, '
        f'SQLite {", ".join(native_versions) or "unobserved"}, ONNX Runtime {versions["packages"]["onnxruntime"]}. '
        'Each primary cell uses 512 measured facts plus twelve anchors; atomic calls contain eight facts. '
        'The receipt SDK and authenticated HTTP use the same public fixture, ownership policy and WAL/FULL durability. '
        f'The process-tree RSS cap was {limit:.2f} GiB, with 2 GiB available-RAM headroom for resource qualification.',
        "",
        'The tested working tree is identified by the relative source and harness manifests in '
        f'[the numerical ledger](results/{filename}) '
        '(`runtime_source_sha256` and `harness_source_sha256`); it does not imply a clean commit or tag. '
        'The ledger also binds the local model files, fixture digests and qualification reports.',
        "",
        'Acknowledged measured facts per second, excluding anchors and process close:', "",
        "| Backend | Surface | Call | Group | 1 agent | 4 agents | 8 agents | 16 agents |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for backend in ("hash", "neural"):
        for mode in ("scalar", "atomic"):
            for surface, groups in (("receipt-sdk", (0,)), ("http", (0, 1))):
                for group in groups:
                    records = [record for record in primary
                               if all(record["cell"][key] == target for key, target in (
                                   ("backend", backend), ("mode", mode), ("surface", surface), ("group_commit", group)
                               ))]
                    by_agents = {record["cell"]["agents"]: record for record in records}
                    rates = [metric(by_agents[n], "acknowledged_rows_per_second") for n in (1, 4, 8, 16)]
                    label = "Receipt SDK" if surface == "receipt-sdk" else "HTTP"
                    call = "Scalar" if mode == "scalar" else "Atomic 8"
                    lines.append(f'| {backend.title()} | {label} | {call} | {group} | ' + " | ".join(rates) + " |")
    counts = Counter(record["status"] for record in primary)
    accounting = {}
    for name in ("attempted_rows", "acknowledged_rows", "failed_acknowledgements", "unknown_outcome_rows",
                 "unacknowledged_committed_row_count"):
        accounting[name] = sum(record.get("result", {}).get("writes", {}).get(name) or 0 for record in primary)
    missing_accounting = sum(record["status"] == "failed" and (
        "result" not in record or not record["result"]["checks"]["attempt_accounting_complete"]
    ) for record in primary)
    unverified_committed = sum("result" in record and record["result"]["writes"]["unacknowledged_committed_row_count"] is None
                              for record in primary)
    accounting_limits = (
        f' {missing_accounting} failed cells lack complete attempt accounting; '
        f'{unverified_committed} measured cells lack committed-without-acknowledgement verification.'
        if missing_accounting or unverified_committed else ""
    )
    lines += [
        "",
        f'Primary coverage: 48 planned; {counts["completed"]} completed, {counts["failed"]} failed, '
        f'{counts["deferred"]} deferred, {counts["unavailable"]} unavailable. '
        f'Known accounting: {accounting["attempted_rows"]:,} attempted, {accounting["acknowledged_rows"]:,} acknowledged, '
        f'{accounting["failed_acknowledgements"]:,} failed acknowledgements, {accounting["unknown_outcome_rows"]:,} unknown outcomes '
        f'and {accounting["unacknowledged_committed_row_count"]:,} observed committed facts without acknowledgement.'
        f'{accounting_limits} '
        'All completed cells passed independent ID/content/revision, receipt, integrity, policy, resource and cleanup checks. '
        'Every missing cell retains its planned dimensions and reason in the ledger; failed rates are diagnostic.',
        "",
        'The separate sixteen-agent HTTP scalar scale extension uses 10,000 measured facts plus twelve anchors:', "",
        "| Backend | Group | Status | Acknowledged facts/s | Indexed facts/s | Peak summed RSS MiB |",
        "| --- | --- | --- | ---: | ---: | ---: |",
    ]
    for record in scale:
        result = record.get("result", {})
        eligible = record["status"] == "completed"
        rate = metric(record, "acknowledged_rows_per_second") if eligible else "—"
        indexed = result.get("verification", {}).get("dense", {}).get("fully_indexed_input_rows_per_second")
        indexed_text = f"{indexed:,.1f}" if eligible and type(indexed) in (int, float) else "—"
        rss = result.get("resources", {}).get("sampled_peak_process_tree_rss_bytes")
        rss_text = f"{rss / (1024 ** 2):,.1f}" if type(rss) in (int, float) else "—"
        status = "completed" if eligible else f'{record["status"]}({record["reason"]})'
        lines.append(f'| {record["cell"]["backend"].title()} | {record["cell"]["group_commit"]} | '
                     f'{status} | {rate} | {indexed_text} | {rss_text} |')
    lines += [
        "",
        'Five planned 128-fact resource pilots are recorded separately and excluded from primary and scale rates. '
        'The ledger preserves independently verified insertion rates, neural indexing boundaries, successful scalar or batch-call '
        'p50/p95/p99, first/repeated public recall latency, per-anchor Recall@1/@5, actual group coalescing counters and '
        'RSS/CPU by phase and role, plus the post-close footprint. Summed RSS can double-count shared pages; '
        'sampling can miss short-lived processes and phase CPU attribution is approximate. '
        'One pass establishes coverage, with no run-to-run repeatability claim. The twelve anchors are functional retrieval checks; '
        'they do not establish held-out competitor superiority. Same-source crash tests establish process recovery, '
        'without establishing physical power-loss durability.',
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    options = parser.parse_args()
    plan = runner.load_plan(options.plan)
    if not (Path(plan["run_root"]) / "seal.json").exists():
        raise runner.ControlError("FinalSourceQualificationSealRequired")
    runner.snapshot(plan)
    value = runner.read_json(Path(plan["run_root"]) / "numeric/ledger.json")
    ledger.assert_safe_output(value)
    text = render(value)
    destination = runner.private_path(Path(plan["run_root"]) / "candidate")
    output = destination / ("benchmarks/results/production_performance_" + value["created_utc"][:10].replace("-", "") + ".json")
    runner.write_json(output, value)
    (destination / "RESULTS-addition.md").write_text(text, encoding="utf-8")
    print({"status": "candidate_rendered", "primary_cells": 48, "candidate": str(destination)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

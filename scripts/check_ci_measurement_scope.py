"""Validate narrow Actions measurement scopes against the complete push diff."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MARKERS = {"[retrieval-only]": "retrieval", "[qualification-only]": "qualification"}
DOCUMENTS = {
    "ARCHITECTURE.md", "CHANGELOG.md", "CONTRIBUTING.md", "DEVELOPER_GUIDE.md",
    "DOCS.md", "README.md", "benchmarks/RESULTS.md", "benchmarks/PRODUCTION.md",
    "benchmarks/RETRIEVAL.md", "benchmarks/RETRIEVAL_C4.md",
}
RETRIEVAL_SOURCES = {
    "benchmarks/production_retrieval.py", "benchmarks/replay_retrieval.py",
    "benchmarks/retrieval_ci.py", "benchmarks/passage_retrieval.py",
    "benchmarks/retrieval_c4.py", "benchmarks/retrieval_c4_model.py",
    "benchmarks/provision_retrieval_c4.py", "benchmarks/prepare_retrieval_ci.py",
    "benchmarks/check_passage_contract.py", ".github/workflows/retrieval-quality.yml",
    "benchmarks/runtime_rerank_c4.py", "benchmarks/retrieval_telemetry.py",
    ".github/workflows/production-performance.yml", "scripts/check_ci_measurement_scope.py",
    "tests/test_ci_measurement_scope.py", "tests/test_retrieval_ci_contracts.py",
    "tests/test_retrieval_c4_contracts.py", "tests/test_retrieval_scoring_contracts.py",
    "tests/test_passage_retrieval_contracts.py", "tests/test_runtime_passage_retrieval_contracts.py",
    "tests/test_production_retrieval.py",
    "tests/test_runtime_reranker_ci.py", "tests/test_runtime_reranker_diagnostics.py",
    "tests/test_retrieval_query_telemetry.py",
}


def requested_scope(message: str) -> str:
    if not isinstance(message, str):
        raise ValueError("Invalid push message")
    # GitHub's contains() ignores case; the validator must make the same choice.
    scopes = {scope for marker, scope in MARKERS.items() if marker in message.lower()}
    if len(scopes) > 1:
        raise ValueError("Conflicting measurement scopes")
    return next(iter(scopes), "full")


def validate_changed_paths(scope: str, paths: list[str]) -> None:
    if scope == "full":
        return
    if scope not in {"retrieval", "qualification"}:
        raise ValueError("Unknown measurement scope")
    for name in paths:
        if (not isinstance(name, str) or not name or "\\" in name or ":" in name
                or PurePosixPath(name).is_absolute() or any(part in {"", ".", ".."} for part in name.split("/"))):
            raise ValueError("Invalid changed path")
        documentation = (name in DOCUMENTS or (name.startswith("docs/") and name.endswith(".md"))
                         or (name.startswith("benchmarks/results/") and name.endswith(".json")))
        retrieval = name in RETRIEVAL_SOURCES or name.startswith("benchmarks/retrieval_fixtures/")
        if not documentation and not (scope == "retrieval" and retrieval):
            raise ValueError("Scoped measurements require unchanged runtime, storage harness and test launcher")


def git(*arguments: str) -> str:
    result = subprocess.run(["git", *arguments], cwd=ROOT, capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError("Push base unavailable; use full scope or fetch its history")
    return result.stdout.decode("utf-8")


def main() -> int:
    if os.environ.get("GITHUB_EVENT_NAME") != "push":
        print(json.dumps({"measurement_scope": "manual_inputs"}))
        return 0
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    scope = requested_scope(event.get("head_commit", {}).get("message", ""))
    if scope == "full":
        print(json.dumps({"measurement_scope": scope}))
        return 0
    # Inspect the entire push, so earlier runtime changes cannot hide behind its last trailer.
    before, head = event.get("before"), os.environ.get("GITHUB_SHA")
    if (not isinstance(before, str) or not isinstance(head, str)
            or not re.fullmatch(r"[0-9a-f]{40}", before) or before == "0" * 40
            or not re.fullmatch(r"[0-9a-f]{40}", head) or event.get("after") != head
            or git("rev-parse", "HEAD").strip() != head):
        raise ValueError("Scoped push identity mismatch")
    # Renames must expose both the removed source and the new destination.
    paths = [name for name in git("diff", "--no-renames", "--name-only", "-z", before, head).split("\0") if name]
    validate_changed_paths(scope, paths)
    print(json.dumps({"measurement_scope": scope, "changed_paths": len(paths), "storage_sources_unchanged": True}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"measurement_scope_valid": False, "reason": type(error).__name__}))
        raise SystemExit(1) from None

"""Validate build artifacts and the isolated wheel; optionally collect the sdist in Actions."""
from __future__ import annotations

import argparse
import hashlib
import json
import ntpath
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from email.parser import Parser

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.run_tests import isolated_environment

ESSENTIALS = (
    "mesh/brief.py", "mesh/client.py", "mesh/execution.py", "mesh/group_commit.py",
    "mesh/namespace_maintenance.py", "mesh/operation_contract.py", "mesh/operations.py",
    "mesh/sqlite_runtime.py", "mesh/skills/mesh-context-layer/SKILL.md",
    "mesh/skills/mesh-context-layer/rules_block.md",
    "mesh/skills/mesh-context-layer/references/transport_fallbacks.md",
)
SDIST_HELPERS = (
    "benchmark_all_surfaces.py",
    "benchmarks/concurrency_stress.py", "benchmarks/isolation.py", "benchmarks/sleep_guard.py",
    "benchmarks/production_bench.py", "benchmarks/production_transport.py",
    "benchmarks/production_runner.py", "benchmarks/production_ledger.py", "benchmarks/production_test_speed.py",
    "benchmarks/production_report.py", "benchmarks/freeze_production_dependencies.py",
    "benchmarks/provision_production_model.py",
    "benchmarks/production_retrieval.py", "benchmarks/prepare_retrieval_ci.py",
    "benchmarks/replay_retrieval.py", "benchmarks/retrieval_ci.py", "benchmarks/passage_retrieval.py",
    "benchmarks/check_passage_contract.py", "benchmarks/retrieval_c4.py",
    "benchmarks/retrieval_c4_model.py", "benchmarks/provision_retrieval_c4.py",
    "benchmarks/runtime_rerank_c4.py",
)
SDIST_FIXTURES = (
    "benchmarks/retrieval_fixtures/README.md",
    *(f"benchmarks/retrieval_fixtures/{dataset}/{name}"
      for dataset in ("prospective", "longtext-prospective", "longmemeval")
      for name in ("manifest.json", "labels.json", "queries-selected.jsonl")),
    *(f"benchmarks/retrieval_fixtures/{dataset}/corpus.jsonl"
      for dataset in ("prospective", "longtext-prospective")),
    *(f"benchmarks/retrieval_fixtures/longmemeval/corpus-{index:03d}.jsonl" for index in range(6)),
)
SDIST_ESSENTIALS = (
    *ESSENTIALS, *SDIST_HELPERS, *SDIST_FIXTURES,
    "pyproject.toml", "scripts/run_tests.py", "scripts/validate_production_package.py", "tests/conftest.py",
    "tests/test_production_package_contract.py",
)
COLLECTION_MARKER = "MESH_SDIST_COLLECTION="
SDIST_COLLECTION = '''
import json, sys
from pathlib import Path
source = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(source))
import mesh
assert Path(mesh.__file__).resolve().is_relative_to(source)
import pytest
class Collection:
    def pytest_collection_finish(self, session):
        assert session.config.rootpath.resolve() == source
        assert all(Path(item.path).resolve().is_relative_to(source) for item in session.items)
        self.tests = len(session.items)
        self.files = len({str(item.path) for item in session.items})
collection = Collection()
code = pytest.main([
    "--collect-only", "-q", "-o", "addopts=", "--rootdir", str(source), "--confcutdir", str(source),
    "-p", "pytest_asyncio.plugin", "-p", "pytest_cov.plugin", "-p", "_hypothesis_pytestplugin",
    "-p", "anyio.pytest_plugin", str(source / "tests"),
], plugins=[collection])
if code:
    raise SystemExit(code)
assert collection.tests > 0 and collection.files > 0
project_names = {"mesh", "benchmarks", "scripts", *json.loads(sys.argv[2])}
for name, module in list(sys.modules.items()):
    filename = getattr(module, "__file__", None)
    if name.split(".")[0] in project_names and filename:
        assert Path(filename).resolve().is_relative_to(source), f"Source import escaped sdist: {name}"
print("MESH_SDIST_COLLECTION=" + json.dumps({
    "status": "passed", "collected_tests": collection.tests, "collected_files": collection.files,
    "extracted_source_import_verified": True,
}))
'''
SMOKE = '''
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import mesh
assert Path(mesh.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve())
assert mesh.__version__ == sys.argv[2]
assert not any(name in sys.modules for name in ("numpy", "onnxruntime", "fastapi", "sqlite3", "apsw"))
from mesh.client import ConnectionProfile, MeshClient
from mesh.sqlite_runtime import sqlite3, is_patched_sqlite
assert is_patched_sqlite(sqlite3.sqlite_version)
profile = ConnectionProfile(namespace="package-smoke", mesh_home=Path(os.environ["MESH_HOME"]), agent_id="package-test")
client = MeshClient(profile, transport="sdk")
first = client.call("mesh_learn", {"content": "The package fixture service listens on port 8042.", "deduplicate": False}, operation_id="package-fact-8042")
assert first["memory_id"] and first["operation_id"] == "package-fact-8042"
replayed = client.call("mesh_learn", {"content": "The package fixture service listens on port 8042.", "deduplicate": False}, operation_id="package-fact-8042")
assert replayed["memory_id"] == first["memory_id"] and replayed["replayed"] is True
found = client.call("mesh_recall", {"query": "fixture service port", "n": 5})
assert first["memory_id"] in {item["id"] for item in found["results"]}
with mesh.Mesh(namespace="package-smoke") as memory:
    many = memory.learn_many([{"content": "The package fixture stores state locally."}, {"content": "The package fixture uses bounded workers."}], deduplicate=False)
    assert len(many) == 2 and all(item["status"] == "stored" for item in many)
    task = memory.task_create("Validate the package fixture", description="Use isolated test data.")
    memory.task_complete(task["id"], summary="The package fixture was verified.", evidence=["fixture-test"])
    brief = memory.project_brief(max_tokens=2000, max_facts=20, include_private=False)
    assert brief["facts_included"] >= 3 and brief["untrusted_reference_data"] is True
    assert len(brief["brief_markdown"]) <= 8000
print(json.dumps({"status": "passed", "version": mesh.__version__, "sqlite_version": sqlite3.sqlite_version,
                  "wheel_import_verified": True, "lazy_import_verified": True, "receipts_verified": True,
                  "bulk_verified": True, "retrieval_verified": True, "tasks_and_brief_verified": True}))
'''


def run(arguments: list[str], environment: dict[str, str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(arguments, env=environment, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError((result.stdout + result.stderr)[-6000:])
    return result


def _reserved_windows_member(name: str) -> bool:
    checker = getattr(ntpath, "isreserved", None)
    if checker is not None:
        return checker(name)
    return PureWindowsPath(name).is_reserved()


def extract_sdist(archive: Path, owned_root: Path) -> Path:
    """Preflight all tar members, then copy only files/directories into a fresh owned root."""
    attributes = owned_root.lstat()
    if stat.S_ISLNK(attributes.st_mode) or getattr(attributes, "st_file_attributes", 0) & 0x400:
        raise ValueError("Linked extraction root rejected")
    owned_root = owned_root.resolve(strict=True)
    extraction_root = owned_root / "sdist-source"
    if extraction_root.exists() or extraction_root.is_symlink():
        raise ValueError("Source extraction requires a fresh destination")
    with tarfile.open(archive) as package:
        entries = []
        roots: set[str] = set()
        seen: set[tuple[str, ...]] = set()
        for member in package.getmembers():
            name = member.name[:-1] if member.isdir() and member.name.endswith("/") else member.name
            parts = tuple(name.split("/"))
            if (not name or PurePosixPath(name).is_absolute() or PureWindowsPath(name).drive
                    or "\\" in name or any(part in ("", ".", "..") for part in parts)
                    or any(part.endswith((".", " ")) or _reserved_windows_member(part)
                           or any(character in '<>:"|?*' or ord(character) < 32 for character in part)
                           for part in parts)):
                raise ValueError("Unsafe source archive member path")
            if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE):
                raise ValueError("Source archive links and special files are forbidden")
            if len(parts) == 1 and not member.isdir():
                raise ValueError("Source archive must have one directory root")
            key = tuple(part.casefold() for part in parts)
            if key in seen:
                raise ValueError("Duplicate source archive member")
            seen.add(key)
            roots.add(parts[0])
            target = extraction_root.joinpath(*parts).resolve()
            if not target.is_relative_to(extraction_root) or not target.is_relative_to(owned_root):
                raise ValueError("Source archive target escaped the owned root")
            entries.append((member, target))
        if len(roots) != 1:
            raise ValueError("Source archive must have one directory root")
        extraction_root.mkdir()
        for member, target in entries:
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                source = package.extractfile(member)
                if source is None:
                    raise ValueError("Source archive file has no content")
                with source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
    return extraction_root / next(iter(roots))


def collect_sdist(archive: Path, owned_root: Path, environment: dict[str, str]) -> dict:
    source_root = extract_sdist(archive, owned_root)
    collection_environment = isolated_environment(owned_root / "sdist-isolated", inherited=environment)
    collection_environment["HF_HUB_OFFLINE"] = "1"
    collection_environment["TRANSFORMERS_OFFLINE"] = "1"
    helper_names = [PurePosixPath(name).stem for name in SDIST_HELPERS]
    result = run([sys.executable, "-I", "-B", "-c", SDIST_COLLECTION, str(source_root), json.dumps(helper_names)],
                 collection_environment, source_root, timeout=300)
    payloads = [line[len(COLLECTION_MARKER):] for line in result.stdout.splitlines() if line.startswith(COLLECTION_MARKER)]
    if len(payloads) != 1:
        raise ValueError("Source collection did not report its counts")
    report = json.loads(payloads[0])
    if (report.get("status") != "passed" or report.get("extracted_source_import_verified") is not True
            or any(type(report.get(key)) is not int or report[key] < 1 for key in ("collected_tests", "collected_files"))):
        raise ValueError("Source collection did not verify extracted tests")
    return report


def parse_options(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact_root", type=Path, help="Directory containing one wheel and one source archive.")
    parser.add_argument("--collect-sdist", action="store_true",
                        help="Collect tests from the extracted sdist in GitHub Actions; requires installed test extras (default: off).")
    options = parser.parse_args(argv)
    if options.collect_sdist and os.environ.get("GITHUB_ACTIONS") != "true":
        parser.error("--collect-sdist requires GitHub Actions")
    return options


def main(argv: list[str] | None = None) -> int:
    options = parse_options(argv)
    evidence_root = ROOT / ".work"
    evidence_root.mkdir(parents=True, exist_ok=True)
    artifact_root = options.artifact_root.resolve()
    wheels = sorted(artifact_root.glob("*.whl"))
    sdists = sorted(artifact_root.glob("*.tar.gz"))
    assert len(wheels) == len(sdists) == 1
    wheel, sdist = wheels[0], sdists[0]
    assert wheel.name.endswith("-py3-none-any.whl")
    with zipfile.ZipFile(wheel) as package:
        wheel_members = package.namelist()
        assert all(member in wheel_members for member in ESSENTIALS)
        assert not any(name.startswith(("tests/", "benchmarks/", "scripts/")) or name == "benchmark_all_surfaces.py"
                       for name in wheel_members)
        metadata_name = next(name for name in wheel_members if name.endswith(".dist-info/METADATA"))
        metadata = Parser().parsestr(package.read(metadata_name).decode("utf-8"))
        expected_version = metadata["Version"]
        assert expected_version and any(value.startswith("apsw") for value in metadata.get_all("Requires-Dist", []))
    with tarfile.open(sdist) as package:
        sdist_members = package.getnames()
        assert all(any(name.endswith("/" + member) for name in sdist_members) for member in SDIST_ESSENTIALS)
    for name in [*wheel_members, *sdist_members]:
        assert not name.lower().endswith((".dll", ".so", ".dylib", ".pyd", ".exe", ".mp4", ".mp3", ".wav", ".onnx", ".safetensors"))
        assert not any(part in {".env", ".work", ".mesh", ".codex", "node_modules", "marketing"} for part in Path(name).parts)
    with tempfile.TemporaryDirectory(prefix="mesh-package-validation-") as temporary:
        temporary_root = Path(temporary).resolve()
        assert temporary_root.parent == Path(tempfile.gettempdir()).resolve()
        environment = isolated_environment(temporary_root / "isolated")
        environment["PIP_CONFIG_FILE"] = os.devnull
        environment["MESH_NO_OUTBOX_WORKER"] = "1"
        environment["MESH_FORCE_HASH_EMBED"] = "1"
        for name in ("MESH_ENCRYPT_AT_REST", "MESH_ENCRYPTION_KEY", "MESH_PASSPHRASE", "MESH_LOCKED_NAMESPACE", "MESH_NAMESPACE", "MESH_CONNECTION_PROFILE", "MESH_TOKEN_FILE"):
            environment.pop(name, None)
        for name in ("MESH_BG_ONNX", "MESH_GROUP_COMMIT"):
            environment[name] = "0"
        site_path = temporary_root / "wheel-site"
        try:
            run([sys.executable, "-m", "pip", "--disable-pip-version-check", "install", "--no-index",
                 "--no-deps", "--no-cache-dir", "--target", str(site_path), str(wheel)], environment, temporary_root)
            smoke = json.loads(run([sys.executable, "-I", "-c", SMOKE, str(site_path), expected_version], environment, temporary_root).stdout.splitlines()[-1])
            cli_code = "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); sys.argv[0]='mesh'; runpy.run_module('mesh',run_name='__main__')"
            help_result = run([sys.executable, "-I", "-c", cli_code, str(site_path), "call", "--help"], environment, temporary_root)
            assert "--profile" in help_result.stdout and "--args-file" in help_result.stdout
            run([sys.executable, "-I", "-c", cli_code, str(site_path), "doctor"], environment, temporary_root)
            run([sys.executable, "-I", "-c", cli_code, str(site_path), "install", "--dry-run"], environment, temporary_root)
            collection = collect_sdist(sdist, temporary_root, environment) if options.collect_sdist else {"status": "not_requested"}
            report = {"status": "passed", "wheel": {"file": wheel.name, "bytes": wheel.stat().st_size,
                      "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(), "members": len(wheel_members)},
                      "sdist": {"file": sdist.name, "bytes": sdist.stat().st_size,
                      "sha256": hashlib.sha256(sdist.read_bytes()).hexdigest(), "members": len(sdist_members)},
                      "smoke": smoke, "cli_help_verified": True, "doctor_verified": True,
                      "installer_dry_run_verified": True, "sdist_collection": collection, "python": sys.version.split()[0]}
        finally:
            inventory = sorted(path.relative_to(temporary_root).as_posix() for path in temporary_root.rglob("*"))
            (evidence_root / "package-cleanup-inventory.json").write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
    destination = evidence_root / "package-final-validation.json"
    destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

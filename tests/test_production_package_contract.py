"""Tiny stdlib-only source packaging and archive safety contracts."""
from __future__ import annotations

import ast
import fnmatch
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import validate_production_package as validator


def build_archive(path: Path, entries: list[tuple[str, bytes, bytes]]) -> None:
    with tarfile.open(path, "w:gz") as output:
        for name, kind, content in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = len(content) if kind == tarfile.REGTYPE else 0
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                member.linkname = "../outside"
            output.addfile(member, io.BytesIO(content) if kind == tarfile.REGTYPE else None)


def table_lists(name: str) -> dict[str, list[str]]:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    section = re.search(r"^\[" + re.escape(name) + r"\]\s*\n(.*?)(?=^\[|\Z)", text, re.M | re.S)
    assert section is not None
    return {key: ast.literal_eval(value) for key, value in
            re.findall(r"^(include|exclude|packages)\s*=\s*(\[.*?\])", section.group(1), re.M | re.S)}


def selected(member: str, patterns: list[str]) -> bool:
    return any(member.startswith(pattern.lstrip("/") + "/")
               or fnmatch.fnmatchcase(member, pattern.lstrip("/")) for pattern in patterns)


class SourceManifestContracts(unittest.TestCase):
    def test_sdist_declares_transitive_sibling_helper_imports(self):
        # A new benchmark import must fail here instead of during archive collection.
        for helper in validator.SDIST_HELPERS:
            tree = ast.parse((ROOT / helper).read_text(encoding="utf-8"))
            imports = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    imports.add(node.module.split(".")[0])
            for name in sorted(imports):
                sibling = Path(helper).parent / (name + ".py")
                if (ROOT / sibling).is_file():
                    with self.subTest(helper=helper, dependency=sibling.as_posix()):
                        self.assertIn(sibling.as_posix(), validator.SDIST_HELPERS)

    def test_sdist_includes_every_imported_helper_and_bounded_fixture(self):
        config = table_lists("tool.hatch.build.targets.sdist")
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines()
        includes = [pattern for line in manifest if line.startswith("include ") for pattern in line.split()[1:]]
        prunes = [line.split()[1] for line in manifest if line.startswith("prune ")]
        for member in (*validator.SDIST_HELPERS, *validator.SDIST_FIXTURES):
            with self.subTest(member=member):
                self.assertTrue((ROOT / member).is_file())
                self.assertTrue(selected(member, config["include"]))
                self.assertFalse(selected(member, config["exclude"]))
                self.assertTrue(selected(member, includes))
                self.assertFalse(selected(member, prunes))

    def test_downloaded_data_results_and_models_are_outside_the_source_allowlist(self):
        config = table_lists("tool.hatch.build.targets.sdist")
        for member in ("benchmarks/data/downloaded.jsonl", "benchmarks/results/run.json",
                       "benchmarks/models/tokenizer.json", "benchmarks/models/model.onnx",
                       "benchmarks/retrieval_fixtures/prospective/model.onnx",
                       "benchmarks/retrieval_fixtures/prospective/tokenizer.json",
                       "benchmarks/retrieval_fixtures/prospective/downloaded.jsonl",
                       "benchmarks/unreviewed_helper.py"):
            with self.subTest(member=member):
                self.assertFalse(selected(member, config["include"]))
        wheel = table_lists("tool.hatch.build.targets.wheel")
        self.assertEqual(wheel["packages"], ["mesh"])
        self.assertTrue(all(pattern.startswith("mesh/") for pattern in wheel["include"]))


class ArchiveContracts(unittest.TestCase):
    def test_valid_archive_is_copied_inside_owned_fresh_root(self):
        with tempfile.TemporaryDirectory(prefix="mesh-package-contract-") as temporary:
            root = Path(temporary).resolve()
            archive = root / "fixture.tar.gz"
            build_archive(archive, [("mesh-fixture", tarfile.DIRTYPE, b""),
                                    ("mesh-fixture/tests/test_fixture.py", tarfile.REGTYPE, b"# fixture\n")])
            source = validator.extract_sdist(archive, root)
            self.assertEqual(source, root / "sdist-source/mesh-fixture")
            self.assertTrue(source.resolve().is_relative_to(root))
            self.assertEqual((source / "tests/test_fixture.py").read_bytes(), b"# fixture\n")

    def test_unsafe_paths_are_rejected_before_any_extraction(self):
        names = ("../outside", "/absolute", "mesh-fixture/../outside", "mesh-fixture/a/../../outside",
                 "C:/FAKE/outside", "C:outside", "//FAKE/share/outside", "mesh-fixture/C:/outside",
                 "mesh-fixture\\outside", "mesh-fixture//outside", "mesh-fixture/./outside",
                 "mesh-fixture/stream:secret", "mesh-fixture/CON", "mesh-fixture/trailing.")
        for name in names:
            with self.subTest(name=name), tempfile.TemporaryDirectory(prefix="mesh-package-contract-") as temporary:
                root = Path(temporary).resolve()
                archive = root / "fixture.tar.gz"
                build_archive(archive, [("mesh-fixture/first.py", tarfile.REGTYPE, b"# fixture\n"),
                                        (name, tarfile.REGTYPE, b"fake")])
                with self.assertRaises(ValueError):
                    validator.extract_sdist(archive, root)
                self.assertFalse((root / "sdist-source").exists())

    def test_links_special_files_duplicate_members_and_multiple_roots_are_rejected(self):
        bad_entries = [[("mesh-fixture/link", kind, b"")] for kind in
                       (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE)]
        bad_entries.extend([
            [("mesh-fixture/first.py", tarfile.REGTYPE, b"duplicate")],
            [("mesh-fixture/FIRST.py", tarfile.REGTYPE, b"case alias")],
            [("other-root/second.py", tarfile.REGTYPE, b"other")],
        ])
        for entries in bad_entries:
            with self.subTest(entries=entries), tempfile.TemporaryDirectory(prefix="mesh-package-contract-") as temporary:
                root = Path(temporary).resolve()
                archive = root / "fixture.tar.gz"
                build_archive(archive, [("mesh-fixture/first.py", tarfile.REGTYPE, b"# fixture\n"), *entries])
                with self.assertRaises(ValueError):
                    validator.extract_sdist(archive, root)
                self.assertFalse((root / "sdist-source").exists())

    def test_reused_destination_and_linked_owned_root_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="mesh-package-contract-") as temporary:
            root = Path(temporary).resolve()
            archive = root / "fixture.tar.gz"
            build_archive(archive, [("mesh-fixture/first.py", tarfile.REGTYPE, b"fake")])
            destination = root / "sdist-source"
            destination.mkdir()
            sentinel = destination / "sentinel"
            sentinel.write_bytes(b"preserved")
            with self.assertRaisesRegex(ValueError, "fresh"):
                validator.extract_sdist(archive, root)
            self.assertEqual(sentinel.read_bytes(), b"preserved")
            attributes = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            with mock.patch.object(Path, "lstat", return_value=attributes), self.assertRaisesRegex(ValueError, "Linked"):
                validator.extract_sdist(archive, root)

    def test_resolved_target_cannot_escape_even_if_path_resolution_is_redirected(self):
        with tempfile.TemporaryDirectory(prefix="mesh-package-contract-") as temporary:
            root = Path(temporary).resolve()
            archive = root / "fixture.tar.gz"
            build_archive(archive, [("mesh-fixture/first.py", tarfile.REGTYPE, b"fake")])
            original = Path.resolve

            def redirected(path, *args, **kwargs):
                if path == root / "sdist-source/mesh-fixture/first.py":
                    return root.parent / "outside-fixture.py"
                return original(path, *args, **kwargs)

            with mock.patch.object(Path, "resolve", redirected), self.assertRaisesRegex(ValueError, "escaped"):
                validator.extract_sdist(archive, root)
            self.assertFalse((root / "sdist-source").exists())


class CollectionContracts(unittest.TestCase):
    def test_collection_is_default_off_and_requires_actions_when_requested(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(validator.parse_options(["fake-dist"]).collect_sdist)
            with mock.patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
                validator.main(["fake-dist", "--collect-sdist"])
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=True):
            self.assertTrue(validator.parse_options(["fake-dist", "--collect-sdist"]).collect_sdist)

    def test_collection_uses_extracted_source_and_reports_verified_counts(self):
        with tempfile.TemporaryDirectory(prefix="mesh-package-contract-") as temporary:
            root = Path(temporary).resolve()
            archive = root / "fixture.tar.gz"
            build_archive(archive, [("mesh-fixture/tests/test_fixture.py", tarfile.REGTYPE, b"# fixture\n")])
            report = {"status": "passed", "collected_tests": 3, "collected_files": 1,
                      "extracted_source_import_verified": True}
            result = subprocess.CompletedProcess([], 0, stdout=validator.COLLECTION_MARKER + json.dumps(report), stderr="")
            inherited = {"HOME": "FAKE_REAL_HOME", "MESH_HOME": "FAKE_REAL_MESH", "PYTHONPATH": "FAKE_CHECKOUT"}
            with mock.patch.object(validator, "run", return_value=result) as child:
                self.assertEqual(validator.collect_sdist(archive, root, inherited), report)
            command, environment, cwd = child.call_args.args
            self.assertEqual(command[1:4], ["-I", "-B", "-c"])
            self.assertEqual(Path(command[5]), cwd)
            self.assertTrue(cwd.is_relative_to(root / "sdist-source"))
            for name in ("MESH_HOME", "HOME", "USERPROFILE", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "MESH_TEST_RUN_ROOT"):
                self.assertTrue(Path(environment[name]).is_relative_to(root), name)
            for name in ("MESH_NO_AUTO_DAEMON", "MESH_NO_MODEL_DOWNLOAD", "MESH_NO_IMPLICIT_CLOUD",
                         "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
                self.assertEqual(environment[name], "1", name)
            self.assertEqual(environment["MESH_AUTO_APPROVE"], "0")

    def test_missing_zero_or_unverified_counts_cannot_pass_collection(self):
        reports = [None,
                   {"status": "passed", "collected_tests": 0, "collected_files": 1, "extracted_source_import_verified": True},
                   {"status": "passed", "collected_tests": 3, "collected_files": True, "extracted_source_import_verified": True},
                   {"status": "passed", "collected_tests": 3, "collected_files": 1, "extracted_source_import_verified": False}]
        with tempfile.TemporaryDirectory(prefix="mesh-package-contract-") as temporary:
            root = Path(temporary).resolve()
            source = root / "fake-extracted-source"
            source.mkdir()
            for report in reports:
                with self.subTest(report=report):
                    stdout = "" if report is None else validator.COLLECTION_MARKER + json.dumps(report)
                    result = subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")
                    with mock.patch.object(validator, "extract_sdist", return_value=source), \
                            mock.patch.object(validator, "run", return_value=result), self.assertRaises(ValueError):
                        validator.collect_sdist(root / "fake.tar.gz", root, {})


if __name__ == "__main__":
    unittest.main()

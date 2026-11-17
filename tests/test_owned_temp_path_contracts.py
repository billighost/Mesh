"""Stdlib contracts for canonical owned temps and guarded external artifacts."""
from __future__ import annotations

import contextlib
import argparse
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BENCHMARKS = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCHMARKS))

from benchmarks import passage_retrieval as passage
from benchmarks import replay_retrieval as replay
from benchmarks import retrieval_ci as ci


class OwnedTemporaryPathContracts(unittest.TestCase):
    def _check_supervisor_alias(self, base, canonical, alias):
        with self.assertRaises(ValueError):
            replay.checked_path(alias, must_exist=True)
        repository = base / "repository"
        benchmark = repository / "benchmarks"
        benchmark.mkdir(parents=True)
        for name in ci.HARNESS_FILES:
            (benchmark / name).write_text("# synthetic supervisor source\n", encoding="utf-8")
        output = repository / ".work/run"
        output.mkdir(parents=True)
        captured = []

        def before_phase(_command, **kwargs):
            captured.append(kwargs["environment"])
            raise ValueError("FAKE_SECRET_VALUE C:/Users/EXAMPLE")

        args = argparse.Namespace(dataset="prospective", qualification_directory=output,
                                  floor_qualification_directory=output, model_directory=repository / ".work/models",
                                  wall_seconds=10, rss_mib=64, cpu_seconds=10)
        for experiment in ("off", "c4"):
            args.reranker_experiment = experiment
            with self.subTest(experiment=experiment), mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch.object(tempfile, "tempdir", str(base)), \
                    mock.patch.object(tempfile, "TemporaryDirectory", return_value=contextlib.nullcontext(str(alias))), \
                    mock.patch.object(ci, "hashes", return_value={}), \
                    mock.patch.object(ci, "qualify", return_value={"same_runtime_sources": True}), \
                    mock.patch.object(ci, "supervise", side_effect=before_phase), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ci.run(args, repository, output), 1)
            environment = captured[-1]
            for name in ("TMPDIR", "TMP", "TEMP"):
                self.assertEqual(environment[name], str(canonical))
            self.assertEqual(Path(environment["MESH_HOME"]), canonical / "mesh-home")
            report_text = (output / "job-summary.json").read_text(encoding="utf-8")
            report = json.loads(report_text)
            self.assertEqual(report["failure_details"]["stage"], "provision")
            self.assertEqual(report["failure_details"]["exception_type"], "ValueError")
            self.assertEqual(report["phases"][0]["status"], "failed")
            self.assertTrue(all(phase["status"] == "not_run" for phase in report["phases"][1:]))
            self.assertNotIn("FAKE_SECRET_VALUE", report_text)
            self.assertNotIn("C:/Users", report_text)

    def test_supervisor_accepts_its_owned_system_alias_before_starting_phases(self):
        with tempfile.TemporaryDirectory(prefix="mesh-owned-temp-contract-") as temporary:
            base = Path(temporary).resolve(strict=True)
            canonical = base / "mesh-retrieval-ci-supervisor-owned"
            canonical.mkdir()
            alias = base / "linked-root"
            try:
                alias.symlink_to(canonical, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("Directory symlinks are unavailable")
            self._check_supervisor_alias(base, canonical, alias)

    @unittest.skipUnless(os.name == "nt", "Windows short path contract")
    def test_supervisor_accepts_only_its_owned_windows_short_path_alias(self):
        import ctypes
        from ctypes import wintypes

        short_path = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
        short_path.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
        short_path.restype = wintypes.DWORD
        with tempfile.TemporaryDirectory(prefix="mesh-owned-temp-contract-") as temporary:
            base = Path(temporary).resolve(strict=True)
            canonical = base / "mesh-retrieval-ci-supervisor-owned"
            canonical.mkdir()
            buffer = ctypes.create_unicode_buffer(32768)
            length = short_path(str(canonical), buffer, len(buffer))
            if not length or length >= len(buffer) or Path(buffer.value) == canonical:
                self.skipTest("Windows short names are unavailable")
            alias = Path(buffer.value)
            self.assertEqual(alias.resolve(strict=True), canonical)
            self._check_supervisor_alias(base, canonical, alias)

    def test_owned_factory_accepts_temp_alias_while_external_paths_stay_guarded(self):
        with tempfile.TemporaryDirectory(prefix="mesh-owned-temp-contract-") as temporary:
            base = Path(temporary).resolve(strict=True)
            physical = base / "physical"
            physical.mkdir()
            alias = base / "alias"
            try:
                alias.symlink_to(physical, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("Directory symlinks are unavailable")

            output = io.StringIO()
            with mock.patch.object(tempfile, "tempdir", str(alias)), contextlib.redirect_stdout(output):
                with passage.owned_runtime_directory() as root:
                    self.assertEqual(root.parent, physical)
                    self.assertEqual(replay.isolated_artifact_path(root, must_exist=True), root)
                    artifact = root / "artifact.json"
                    artifact.write_text("{}", encoding="utf-8")
                    self.assertEqual(replay.checked_path(artifact, root=root, must_exist=True), artifact)

                    external = alias / root.name / artifact.name
                    for check in (replay.checked_path, replay.isolated_artifact_path):
                        with self.subTest(check=check.__name__), self.assertRaisesRegex(ValueError, "Linked|reparse"):
                            check(external, must_exist=True)
                    with self.assertRaisesRegex(ValueError, "Artifact must remain"):
                        replay.isolated_artifact_path(physical, must_exist=True)

                self.assertFalse(root.exists())
            self.assertEqual(json.loads(output.getvalue())["owned_cleanup_files"], ["artifact.json"])
            # Enumerate the remaining owned paths before the outer temp cleanup.
            self.assertEqual(sorted(path.name for path in base.iterdir()), ["alias", "physical"])

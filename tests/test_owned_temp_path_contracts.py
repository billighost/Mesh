"""Stdlib contracts for canonical owned temps and guarded external artifacts."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from benchmarks import passage_retrieval as passage
from benchmarks import replay_retrieval as replay


class OwnedTemporaryPathContracts(unittest.TestCase):
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

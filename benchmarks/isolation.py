"""Temporary full-home isolation shared by developer benchmark entry points."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.run_tests import isolated_environment


@contextmanager
def isolated_benchmark_home(model_dir: Path | None = None) -> Iterator[tuple[Path, bool]]:
    """Bind synthetic data/config before Mesh imports, then close only this run's caches."""
    with tempfile.TemporaryDirectory(prefix="mesh-benchmark-") as temporary:
        root = Path(temporary)
        original = dict(os.environ)
        environment = isolated_environment(root, original)
        environment["PYTHONPATH"] = str(REPO_ROOT) + (
            os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else ""
        )
        environment["MESH_NO_DAEMON_PROXY"] = "1"
        copied = False
        if model_dir is not None:
            source = model_dir.expanduser().absolute()
            if not source.is_dir() or source.is_symlink():
                raise ValueError("Benchmark model directory must be an explicit regular directory.")
            shutil.copytree(source, root / "mesh" / "models")
            copied = True
        changed_keys = {key for key in original.keys() | environment.keys() if original.get(key) != environment.get(key)}
        for key in changed_keys:
            if key in environment:
                os.environ[key] = environment[key]
            else:
                os.environ.pop(key, None)
        try:
            yield root, copied
        finally:
            try:
                if "mesh.audit" in sys.modules:
                    sys.modules["mesh.audit"].flush_audit_log()
                if "mesh.store" in sys.modules:
                    sys.modules["mesh.store"].flush_all_telemetry()
                if "mesh.namespaces" in sys.modules:
                    evict = sys.modules["mesh.namespaces"]._evict_namespace_caches
                    for database in (root / "mesh").glob("mesh_*.db"):
                        evict(database.stem[5:], database)
            finally:
                for key in changed_keys:
                    if key in original:
                        os.environ[key] = original[key]
                    else:
                        os.environ.pop(key, None)

import os
import tempfile
from pathlib import Path

from scripts.run_tests import apply_isolated_environment, isolated_environment


# Apply isolation before test modules import Mesh or inspect agent installation paths.
# Function fixtures may replace these roots, but must never fall through to real homes.
_worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
_run_root = os.environ.get("MESH_TEST_RUN_ROOT")
_worker_environment = isolated_environment(Path(tempfile.mkdtemp(prefix=f"mesh-pytest-{_worker}-", dir=_run_root)))
_test_root = Path(_worker_environment["MESH_TEST_RUN_ROOT"])
apply_isolated_environment(_worker_environment)
tempfile.tempdir = _worker_environment["TMPDIR"]

from hypothesis import settings  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_test_environment():
    environment = isolated_environment(Path(tempfile.mkdtemp(prefix="case-", dir=_test_root)), _worker_environment)
    apply_isolated_environment(environment)
    tempfile.tempdir = environment["TMPDIR"]
    try:
        yield
    finally:
        # Production helpers can assign env directly, outside monkeypatch's undo
        # tracking. Keep case files until the outer runner can safely clean up.
        apply_isolated_environment(_worker_environment)
        tempfile.tempdir = _worker_environment["TMPDIR"]

settings.register_profile("ci", max_examples=2000, deadline=None)
settings.register_profile("default", max_examples=250)
if "HYPOTHESIS_PROFILE" in os.environ:
    settings.load_profile(os.environ["HYPOTHESIS_PROFILE"])

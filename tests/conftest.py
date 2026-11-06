import os
import site
import tempfile
from pathlib import Path

from hypothesis import settings


# Apply isolation before test modules import Mesh or inspect agent installation paths.
# Function fixtures may replace these roots, but must never fall through to real homes.
_original_user_base = site.getuserbase()
_worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
_run_root = os.environ.get("MESH_TEST_RUN_ROOT")
_test_root = Path(tempfile.mkdtemp(prefix=f"mesh-pytest-{_worker}-", dir=_run_root))
os.environ.setdefault("PYTHONUSERBASE", _original_user_base)
for _name, _directory in (
    ("MESH_HOME", "mesh"),
    ("MESH_DIR", "mesh"),
    ("HOME", "home"),
    ("USERPROFILE", "home"),
    ("APPDATA", "appdata"),
    ("LOCALAPPDATA", "localappdata"),
    ("XDG_CONFIG_HOME", "xdg"),
    ("CODEX_HOME", "home/.codex"),
    ("CLAUDE_CONFIG_DIR", "home/.claude"),
    ("HYPOTHESIS_STORAGE_DIRECTORY", "hypothesis"),
):
    _path = _test_root / _directory
    _path.mkdir(parents=True, exist_ok=True)
    os.environ[_name] = str(_path)
for _name in ("MESH_NO_AUTO_DAEMON", "MESH_NO_MODEL_DOWNLOAD", "MESH_NO_UPDATE_CHECK", "MESH_NO_IMPLICIT_CLOUD"):
    os.environ[_name] = "1"
for _name in (
    "MESH_AUTO_APPROVE", "MESH_AUTO_START_DAEMON", "MESH_ALLOW_CLOUD_CAPTURE",
    "MESH_ALLOW_CLOUD_SYNC", "MESH_ALLOW_LOG_SYNC", "MESH_ALLOW_DAEMON_DOWNLOAD", "MESH_ENABLE_AUTO_SYNC",
):
    os.environ[_name] = "0"
for _name in (
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GROQ_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
    "AZURE_OPENAI_API_KEY", "COHERE_API_KEY", "MISTRAL_API_KEY", "DEEPSEEK_API_KEY",
):
    os.environ.pop(_name, None)

settings.register_profile("ci", max_examples=2000, deadline=None)
settings.register_profile("default", max_examples=250)
if "HYPOTHESIS_PROFILE" in os.environ:
    settings.load_profile(os.environ["HYPOTHESIS_PROFILE"])

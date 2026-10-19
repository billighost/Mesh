"""
tests/test_dependency_matrix.py — Verification of dependency boundaries,
graceful fallback degradation, PEP 562 lazy loading, and version compatibility.
"""
from __future__ import annotations

import sys
import uuid
import pytest
from unittest.mock import patch


import subprocess


def test_pep562_lazy_import_isolation():
    """Importing `mesh` in a fresh process must not eagerly import heavy runtime dependencies."""
    code = """
import sys
import mesh

assert hasattr(mesh, "__version__")
for heavy in ("onnxruntime", "fastapi", "uvicorn", "keyring", "cryptography"):
    assert heavy not in sys.modules, f"Heavy dependency '{heavy}' was eagerly loaded on `import mesh`!"
print("OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "OK"


def test_keyring_fallback_when_unavailable(tmp_path, monkeypatch):
    """When the optional 'keyring' package is missing or fails, secrets must fall back to local vault."""
    monkeypatch.setenv("MESH_HOME", str(tmp_path / "mesh_home"))
    monkeypatch.setenv("MESH_NO_AUTO_DAEMON", "1")
    monkeypatch.setenv("MESH_AUTO_APPROVE", "0")

    from mesh.secrets import store_secret_in_keyring, get_secret_from_keyring

    orig_import = __import__

    def mock_import(name, *args, **kwargs):
        if name == "keyring":
            raise ImportError("No module named 'keyring'")
        return orig_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=mock_import):
        loc = store_secret_in_keyring("test-ns", "API_KEY", "secret-val-987")
        assert loc.provider == "keyring"
        assert "keyring://mesh/test-ns/API_KEY" in loc.uri

        # Retrieve should succeed via local vault fallback
        retrieved = get_secret_from_keyring("test-ns", "API_KEY")
        assert retrieved == "secret-val-987"


def test_version_compatibility_logic():
    """Verify semantic version parsing and compatibility checking."""
    from mesh.version import is_version_compatible, parse_version_tuple, VERSION

    # Self-compatibility
    assert is_version_compatible(VERSION, VERSION) is True

    # An older file can be read by a newer engine
    assert is_version_compatible("0.6.5", "0.7.0") is True

    # A newer file cannot be read by an older engine during 0.x
    assert is_version_compatible("0.8.0", "0.7.0") is False

    # Major version mismatch is incompatible
    assert is_version_compatible("1.0.0", "0.7.0") is False

    # Pre-release sorting
    rc_ver = parse_version_tuple("0.7.0rc1")
    final_ver = parse_version_tuple("0.7.0")
    assert rc_ver < final_ver


def test_engine_fallback_behavior(tmp_path, monkeypatch):
    """Verify engine status reports valid backend and degraded flags gracefully."""
    monkeypatch.setenv("MESH_HOME", str(tmp_path / "mesh_home"))
    monkeypatch.setenv("MESH_NO_AUTO_DAEMON", "1")
    monkeypatch.setenv("MESH_AUTO_APPROVE", "0")

    from mesh.memory import get_engine_status

    status = get_engine_status()
    assert isinstance(status, dict)
    assert "backend" in status
    assert "model_tag" in status
    assert "model_loaded" in status

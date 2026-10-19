import os
import shutil
import tempfile
from pathlib import Path
import pytest

from mesh.skill_installer import (
    _SKILL_NAME,
    _skill_source,
    _candidate_skill_roots,
    install_skill,
    install_all,
)


def test_bundled_skill_source_exists():
    src = _skill_source()
    assert src.is_dir(), f"Bundled skill source directory does not exist: {src}"
    skill_md = src / "SKILL.md"
    assert skill_md.is_file(), f"Bundled SKILL.md missing in {src}"
    content = skill_md.read_text(encoding="utf-8")
    assert "mesh-context-layer" in content
    assert "mesh_learn" in content
    assert "mesh_recall" in content


def test_install_skill_to_custom_dir():
    with tempfile.TemporaryDirectory() as tmp_dir:
        target_root = Path(tmp_dir) / "skills"
        target_root.mkdir()

        # First install
        msg, changed = install_skill(target_root)
        assert changed is True
        assert "installed ->" in msg
        dest_skill = target_root / _SKILL_NAME
        assert dest_skill.is_dir()
        assert (dest_skill / "SKILL.md").is_file()

        # Second install without overwrite
        msg2, changed2 = install_skill(target_root, overwrite=False)
        assert changed2 is False
        assert "already installed" in msg2

        # Second install with dry_run: already up to date, and a dry run must
        # never report that it changed anything.
        msg3, changed3 = install_skill(target_root, dry_run=True)
        assert changed3 is False
        assert "would install to" in msg3


def test_install_all_dry_run():
    results = install_all(dry_run=True, assume_yes=True)
    assert isinstance(results, list)
    for r in results:
        assert "agent" in r
        assert "path" in r
        assert "status" in r


def test_check_skill_script():
    """Verify that scripts/check_skill.py succeeds with exit code 0."""
    import importlib.util

    script_path = Path(__file__).resolve().parent.parent / "scripts" / "check_skill.py"
    assert script_path.exists()
    spec = importlib.util.spec_from_file_location("check_skill", script_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main() == 0


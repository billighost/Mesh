import os
import sys
import json
import time
import shutil
import tempfile
import hashlib
import subprocess
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from event_logger import log_event

def hash_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()

def test_uninstall_reversibility():
    test_dir = Path(tempfile.mkdtemp(prefix="uninst_test_"))
    mesh_exe = str(Path(sys.prefix) / "Scripts" / "mesh.exe")

    # Initial state: create a pre-existing GEMINI.md and AGENTS.md with user content
    gemini_file = test_dir / "GEMINI.md"
    initial_content = "# Existing Project Rules\n\n- rule 1: follow conventions\n"
    gemini_file.write_text(initial_content, encoding="utf-8")
    initial_gemini_hash = hash_file(gemini_file)

    agents_file = test_dir / "AGENTS.md"
    agents_content = "# Custom Agents\n\nUse standard tool protocols.\n"
    agents_file.write_text(agents_content, encoding="utf-8")
    initial_agents_hash = hash_file(agents_file)

    # Run mesh install --scope project --dir test_dir --agents antigravity --apply --json
    t0 = time.perf_counter()
    p_inst = subprocess.run([
        mesh_exe, "install", "--scope", "project", "--dir", str(test_dir), "--agents", "antigravity", "--apply", "--json"
    ], capture_output=True, text=True)
    assert p_inst.returncode == 0, f"Install failed: {p_inst.stderr}"

    # Verify files created / updated
    assert (test_dir / ".agents" / "mcp_config.json").exists()
    assert (test_dir / ".agents" / "skills" / "mesh-context-layer").exists()
    assert "<!-- mesh:begin" in gemini_file.read_text(encoding="utf-8")

    # Run mesh uninstall
    t_uninst_start = time.perf_counter()
    p_uninst = subprocess.run([
        mesh_exe, "uninstall", "--scope", "project", "--dir", str(test_dir), "--agents", "antigravity", "--apply", "--json"
    ], capture_output=True, text=True)
    assert p_uninst.returncode == 0, f"Uninstall failed: {p_uninst.stderr}"

    # Verify reversibility
    skill_removed = not (test_dir / ".agents" / "skills" / "mesh-context-layer").exists()
    final_gemini_hash = hash_file(gemini_file)
    gemini_exact = (final_gemini_hash == initial_gemini_hash)

    dur = (time.perf_counter() - t0) * 1000
    log_event("phase4", "tester", "installer_uninstall_reversibility", "mesh uninstall", dur, f"Gemini: {gemini_exact}, Skill removed: {skill_removed}")

    results = {
        "status": "PASS" if (gemini_exact and skill_removed) else "FAIL",
        "initial_gemini_hash": initial_gemini_hash,
        "final_gemini_hash": final_gemini_hash,
        "gemini_byte_exact": gemini_exact,
        "skill_removed": skill_removed
    }

    out_file = PROJECT_DIR / "report" / "data" / "installer_uninstall_results.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Uninstall reversibility test: {results['status']}")
    print(f"GEMINI.md exact match: {gemini_exact}")
    print(f"Skill directory removed: {skill_removed}")

    shutil.rmtree(test_dir, ignore_errors=True)
    return results

if __name__ == "__main__":
    test_uninstall_reversibility()

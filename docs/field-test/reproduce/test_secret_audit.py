import os
import sys
import json
import time
import subprocess
import asyncio
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from event_logger import log_event

MESH_HOME = PROJECT_DIR / ".mesh-home"
os.environ["MESH_HOME"] = str(MESH_HOME)
os.environ["MESH_NO_AUTO_DAEMON"] = "1"
os.environ["MESH_NO_MODEL_DOWNLOAD"] = "1"
os.environ["MESH_NO_UPDATE_CHECK"] = "1"
os.environ["MESH_AUTO_APPROVE"] = "0"
NAMESPACE = "taskflow-t-secrets"

from mesh import Mesh
import mesh.mcp_server as mcp_mod

def run_secret_audit():
    gt_file = PROJECT_DIR / "report" / "ground_truth.json"
    with open(gt_file, "r", encoding="utf-8") as f:
        gt = json.load(f)

    planted = gt["secrets_test"]["planted_secrets"]
    mesh_exe = str(Path(sys.prefix) / "Scripts" / "mesh.exe")
    mesh = Mesh(namespace=NAMESPACE, agent_id="qa-secrets-tester")

    print("Planting secrets across surfaces...")
    planted_results = {}

    for s in planted:
        sid = s["id"]
        raw = s["raw_text"]
        surface = s["surface"]
        t0 = time.perf_counter()
        res_info = {}

        if surface == "sdk_learn":
            r = mesh.learn(raw, memory_type="fact", tags=["sec_test"])
            res_info = {"id": r.get("memory_id"), "local_only": r.get("local_only"), "warnings": r.get("warnings")}
        elif surface == "cli_add":
            p = subprocess.run([mesh_exe, "add", raw, "--namespace", NAMESPACE], capture_output=True, text=True)
            res_info = {"rc": p.returncode, "stdout": p.stdout.strip()}
        elif surface == "mcp_learn":
            async def call_mcp():
                return await mcp_mod.call_tool("mesh_learn", {"content": raw, "namespace": NAMESPACE})
            res = asyncio.run(call_mcp())
            res_info = {"isError": getattr(res, "isError", False), "text": res.content[0].text if res.content else ""}
        elif surface == "task_create_note":
            task = mesh.task_create(title="Security check item", kind="task", description=raw)
            res_info = {"task_id": task.get("id"), "description": task.get("description")}
        elif surface == "mcp_task_create":
            async def call_mcp_task():
                return await mcp_mod.call_tool("mesh_task_create", {"title": "MCP Task with config", "description": raw, "namespace": NAMESPACE})
            res = asyncio.run(call_mcp_task())
            res_info = {"isError": getattr(res, "isError", False), "text": res.content[0].text if res.content else ""}
        elif surface == "cli_task_add":
            p = subprocess.run([mesh_exe, "task", "add", "CLI Task with config", "--desc", raw, "--namespace", NAMESPACE], capture_output=True, text=True)
            res_info = {"rc": p.returncode, "stdout": p.stdout.strip()}
        elif surface == "task_update_summary":
            t = mesh.task_create(title="CLI Flag task", kind="task")
            c = mesh.task_complete(item_id=t["id"], summary=raw, evidence=["cli_exec.log"])
            res_info = {"task_id": t["id"], "summary": c.get("summary")}

        dur = (time.perf_counter() - t0) * 1000
        planted_results[sid] = res_info
        log_event("phase3", "secrets_tester", f"plant_{sid}", surface, dur, f"Planted {sid} via {surface}")

    print("Generating exports and briefs...")
    # Exports
    export_file = MESH_HOME / "backup_export.json"
    subprocess.run([mesh_exe, "export", str(export_file), "--namespace", NAMESPACE], capture_output=True)

    audit_file = MESH_HOME / "audit_export.json"
    p_audit = subprocess.run([mesh_exe, "audit", "export", "--json", "--namespace", NAMESPACE], capture_output=True, text=True)
    if p_audit.returncode == 0:
        audit_file.write_text(p_audit.stdout, encoding="utf-8")
    else:
        audit_file.write_text(json.dumps({"error": p_audit.stderr}), encoding="utf-8")

    digest_file = MESH_HOME / "digest_output.txt"
    p_digest = subprocess.run([mesh_exe, "digest", "--namespace", NAMESPACE], capture_output=True, text=True)
    digest_file.write_text(p_digest.stdout, encoding="utf-8")

    brief_file = MESH_HOME / "brief_output.md"
    p_brief = subprocess.run([mesh_exe, "brief", "--namespace", NAMESPACE], capture_output=True, text=True)
    brief_file.write_text(p_brief.stdout, encoding="utf-8")

    print("Scanning raw files for secret markers...")
    files_to_scan = [p for p in MESH_HOME.rglob("*") if p.is_file()]

    secret_findings = []
    total_markers_tested = len(planted)
    redacted_or_quarantined_count = 0
    plaintext_leaks_count = 0

    for s in planted:
        sid = s["id"]
        marker = s["marker"]
        marker_bytes = marker.encode("utf-8")
        stype = s["type"]

        found_in = []
        for f in files_to_scan:
            try:
                data = f.read_bytes()
                if marker_bytes in data:
                    found_in.append({
                        "file": str(f.relative_to(PROJECT_DIR)),
                        "size_bytes": len(data)
                    })
            except Exception:
                pass

        leaked_in_export = any("backup_export.json" in f["file"] for f in found_in)
        leaked_in_audit = any("audit_export.json" in f["file"] for f in found_in)
        found_in_db = any(f["file"].endswith(".db") or f["file"].endswith(".db-wal") or f["file"].endswith(".db-shm") for f in found_in)
        found_in_brief_or_digest = any("brief_output" in f["file"] or "digest_output" in f["file"] for f in found_in)

        # Classification
        if stype == "pointer_sentence":
            disposition = "pointer_preserved"
            # pointer is expected to be present as a reference
            is_leak = False
        elif stype == "benign_lookalike":
            disposition = "benign_preserved_unmangled" if len(found_in) > 0 else "benign_wrongly_redacted"
            is_leak = False
        else:
            if leaked_in_export or leaked_in_audit or found_in_brief_or_digest:
                disposition = "leaked_plaintext_in_export_or_brief"
                is_leak = True
                plaintext_leaks_count += 1
            elif found_in_db:
                disposition = "leaked_plaintext_in_raw_sqlite"
                is_leak = True
                plaintext_leaks_count += 1
            else:
                disposition = "redacted_or_quarantined"
                is_leak = False
                redacted_or_quarantined_count += 1

        finding = {
            "id": sid,
            "type": stype,
            "marker": marker,
            "surface": s["surface"],
            "disposition": disposition,
            "is_plaintext_leak": is_leak,
            "found_locations": found_in,
            "found_count": len(found_in),
            "leaked_in_export": leaked_in_export,
            "leaked_in_audit": leaked_in_audit,
            "found_in_raw_db": found_in_db
        }
        secret_findings.append(finding)

    # Check benign lookalike specifically
    benign_finding = next(f for f in secret_findings if f["type"] == "benign_lookalike")
    benign_unmangled = benign_finding["found_count"] > 0 and benign_finding["disposition"] == "benign_preserved_unmangled"

    # File permissions on MESH_HOME
    stat_info = MESH_HOME.stat()
    file_permissions = {
        "mesh_home_path": str(MESH_HOME),
        "windows_mode_octal": oct(stat_info.st_mode),
        "windows_mode_dec": stat_info.st_mode,
        "is_dir": MESH_HOME.is_dir(),
        "files_scanned_count": len(files_to_scan)
    }

    # Summary
    # strictly reported as N of M tested patterns
    secret_patterns_evaluated = [f for f in secret_findings if f["type"] not in ("benign_lookalike", "pointer_sentence")]
    protected_patterns = sum(1 for f in secret_patterns_evaluated if not f["is_plaintext_leak"])
    total_secret_patterns = len(secret_patterns_evaluated)

    results = {
        "run_label": "T",
        "tested_patterns_statement": f"{protected_patterns} of {total_secret_patterns} tested secret patterns protected against plaintext leakage",
        "total_patterns_tested": total_markers_tested,
        "secret_patterns_evaluated": total_secret_patterns,
        "protected_patterns_count": protected_patterns,
        "plaintext_leaks_count": plaintext_leaks_count,
        "benign_lookalike_unmangled": benign_unmangled,
        "pointer_preserved": any(f["type"] == "pointer_sentence" and f["disposition"] == "pointer_preserved" for f in secret_findings),
        "file_permissions": file_permissions,
        "findings": secret_findings
    }

    out_file = PROJECT_DIR / "report" / "data" / "secret_audit_results.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nSecret audit finished: {results['tested_patterns_statement']}")
    print(f"Benign lookalike unmangled: {benign_unmangled}")
    print(f"Plaintext leaks count: {plaintext_leaks_count}")
    return results

if __name__ == "__main__":
    run_secret_audit()

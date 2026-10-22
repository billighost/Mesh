import os
import sys
import json
import time
import tempfile
import subprocess
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from event_logger import log_event

def test_sweep():
    results = {}
    mesh_exe = str(Path(sys.prefix) / "Scripts" / "mesh.exe")

    # 1. install --apply --json works and produces applied mode
    print("Testing 1: install --apply --json...")
    t0 = time.perf_counter()
    tmp_proj = Path(tempfile.mkdtemp(prefix="reg_sweep_1_"))
    try:
        p1 = subprocess.run(
            [mesh_exe, "install", "--scope", "project", "--dir", str(tmp_proj), "--agents", "antigravity", "--apply", "--json"],
            capture_output=True, text=True, timeout=15
        )
        data1 = json.loads(p1.stdout) if p1.stdout else {}
        status_1 = "FIXED" if p1.returncode == 0 and data1.get("mode") == "applied" and (tmp_proj / ".agents" / "mcp_config.json").exists() else "REGRESSED"
        results["A001_B002_install_apply_json"] = {
            "status": status_1,
            "returncode": p1.returncode,
            "mode": data1.get("mode"),
            "mcp_config_exists": (tmp_proj / ".agents" / "mcp_config.json").exists(),
            "notes": "Verified --apply --json applies configuration cleanly"
        }
    except Exception as e:
        results["A001_B002_install_apply_json"] = {"status": "REGRESSED", "error": str(e)}
    log_event("phase2", "tester", "A001_B002_install_apply_json", "mesh install", (time.perf_counter() - t0)*1000, results["A001_B002_install_apply_json"]["status"])

    # 2. Invalid MCP subcommand generation: resolve_mesh_mcp_command() returns ['server']
    print("Testing 2: MCP subcommand generation...")
    t0 = time.perf_counter()
    try:
        from mesh.installer import resolve_mesh_mcp_command
        cmd, args = resolve_mesh_mcp_command()
        status_2 = "FIXED" if args == ["server"] else "REGRESSED"
        results["A002_mcp_subcommand_and_alias"] = {
            "status": status_2,
            "resolved_args": args,
            "resolved_cmd": cmd,
            "notes": "resolve_mesh_mcp_command resolves args to ['server']"
        }
    except Exception as e:
        results["A002_mcp_subcommand_and_alias"] = {"status": "REGRESSED", "error": str(e)}
    log_event("phase2", "tester", "A002_mcp_subcommand_and_alias", "resolve_mesh_mcp_command", (time.perf_counter() - t0)*1000, results["A002_mcp_subcommand_and_alias"]["status"])

    # 3. Project scope binds virtualenv python/executable and MESH_HOME
    print("Testing 3: Project scope binding...")
    t0 = time.perf_counter()
    try:
        cfg = json.loads((tmp_proj / ".agents" / "mcp_config.json").read_text(encoding="utf-8"))
        mcp_entry = cfg["mcpServers"]["mesh"]
        cmd_path = Path(mcp_entry["command"]).resolve()
        expected_exe = Path(mesh_exe).resolve()
        env_home = mcp_entry.get("env", {}).get("MESH_HOME")
        status_3 = "FIXED" if cmd_path == expected_exe and env_home == str((tmp_proj / ".mesh-home").resolve()) else "REGRESSED"
        results["A003_B001_project_scope_binding"] = {
            "status": status_3,
            "command": str(cmd_path),
            "expected_command": str(expected_exe),
            "mesh_home": env_home,
            "expected_mesh_home": str((tmp_proj / ".mesh-home").resolve())
        }
    except Exception as e:
        results["A003_B001_project_scope_binding"] = {"status": "REGRESSED", "error": str(e)}
    log_event("phase2", "tester", "A003_B001_project_scope_binding", "inspect_mcp_config", (time.perf_counter() - t0)*1000, results["A003_B001_project_scope_binding"]["status"])

    # 4. Secret ingestion detection and redaction
    print("Testing 4: Secret ingestion detection and redaction...")
    t0 = time.perf_counter()
    tmp_mesh_sec = Path(tempfile.mkdtemp(prefix="reg_sweep_sec_"))
    old_mesh_home = os.environ.get("MESH_HOME")
    os.environ["MESH_HOME"] = str(tmp_mesh_sec)
    os.environ["MESH_NO_AUTO_DAEMON"] = "1"
    os.environ["MESH_NO_MODEL_DOWNLOAD"] = "1"
    os.environ["MESH_NO_UPDATE_CHECK"] = "1"
    try:
        from mesh import Mesh
        m_sec = Mesh(namespace="reg_sec_test", agent_id="sec_tester")
        res_stripe = m_sec.learn("The secret webhook key is WEBHOOK_KEY_9876543210fedcba")
        res_pass = m_sec.learn("The server password is supersecretpassword123")
        res_token = m_sec.learn("Auth header Bearer DEMO_FAKE_TOKEN_7F3A91B4E8C21094AA55")

        row1 = m_sec.store.get(res_stripe["memory_id"])
        row2 = m_sec.store.get(res_pass["memory_id"])
        row3 = m_sec.store.get(res_token["memory_id"])

        sec_ok = (row1["local_only"] is True and "[REDACTED" in row1["content"] and "WEBHOOK_KEY_9876543210fedcba" not in row1["content"]) and \
                 (row2["local_only"] is True and "[REDACTED" in row2["content"] and "supersecretpassword123" not in row2["content"]) and \
                 (row3["local_only"] is True and "[REDACTED" in row3["content"] and "DEMO_FAKE_TOKEN_7F3A91B4E8C21094AA55" not in row3["content"])

        status_4 = "FIXED" if sec_ok else "REGRESSED"
        results["A004_B005_secret_ingestion_redaction"] = {
            "status": status_4,
            "stripe_redacted": "[REDACTED" in row1["content"],
            "stripe_local_only": row1["local_only"],
            "pass_redacted": "[REDACTED" in row2["content"],
            "pass_local_only": row2["local_only"],
            "token_redacted": "[REDACTED" in row3["content"],
            "token_local_only": row3["local_only"]
        }
    except Exception as e:
        results["A004_B005_secret_ingestion_redaction"] = {"status": "REGRESSED", "error": str(e)}
    log_event("phase2", "tester", "A004_B005_secret_ingestion_redaction", "mesh.learn", (time.perf_counter() - t0)*1000, results["A004_B005_secret_ingestion_redaction"]["status"])

    # 5. update_work_item(status='reopened') clean transition and event
    print("Testing 5: Work item reopened transition...")
    t0 = time.perf_counter()
    try:
        from mesh import Mesh
        m_reopen = Mesh(namespace="reg_reopen_test", agent_id="reopen_tester")
        t = m_reopen.task_create(title="Regression Test Task", kind="task")
        m_reopen.task_complete(item_id=t["id"], summary="initial completion")
        reopened = m_reopen.task_update(item_id=t["id"], status="reopened", note="bug recurred")
        events = m_reopen.get_work_events(item_id=t["id"])
        has_reopen_event = any(e.get("event_type") == "reopened" for e in events)
        status_5 = "FIXED" if reopened["status"] == "todo" and reopened.get("completed_at") is None and has_reopen_event else "REGRESSED"
        results["A005_B006_reopened_status_transition"] = {
            "status": status_5,
            "resulting_status": reopened["status"],
            "completed_at": reopened.get("completed_at"),
            "has_reopened_event": has_reopen_event
        }
    except Exception as e:
        results["A005_B006_reopened_status_transition"] = {"status": "REGRESSED", "error": str(e)}
    log_event("phase2", "tester", "A005_B006_reopened_status_transition", "task_update(reopened)", (time.perf_counter() - t0)*1000, results["A005_B006_reopened_status_transition"]["status"])

    # 6. Python SDK method aliases (task_create, task_update, task_complete, task_list, log)
    print("Testing 6: Python SDK method aliases...")
    t0 = time.perf_counter()
    try:
        from mesh import Mesh
        m_alias = Mesh(namespace="reg_alias_test", agent_id="alias_tester")
        t_a = m_alias.task_create(title="Alias Task", kind="task")
        u_a = m_alias.task_update(item_id=t_a["id"], status="in_progress")
        c_a = m_alias.task_complete(item_id=t_a["id"], summary="Done", evidence=["test.log"])
        l_a = m_alias.task_list()
        log_a = m_alias.log(since="1d")
        status_6 = "FIXED" if callable(m_alias.task_create) and callable(m_alias.task_update) and callable(m_alias.task_complete) and callable(m_alias.task_list) and callable(m_alias.log) and len(l_a) >= 1 else "REGRESSED"
        results["A006_B004_sdk_method_aliases"] = {
            "status": status_6,
            "created_id": t_a["id"],
            "updated_status": u_a["status"],
            "completed_status": c_a["status"],
            "task_list_len": len(l_a),
            "log_completed_count": log_a.get("completed_count")
        }
    except Exception as e:
        results["A006_B004_sdk_method_aliases"] = {"status": "REGRESSED", "error": str(e)}
    log_event("phase2", "tester", "A006_B004_sdk_method_aliases", "sdk_aliases", (time.perf_counter() - t0)*1000, results["A006_B004_sdk_method_aliases"]["status"])

    # 7. Guidance on unknown task IDs
    print("Testing 7: Guidance on unknown task IDs...")
    t0 = time.perf_counter()
    try:
        from mesh import Mesh
        m_guidance = Mesh(namespace="reg_guidance_test", agent_id="guidance_tester")
        t_sample = m_guidance.task_create(title="Known Task")
        guidance_found = False
        try:
            m_guidance.task_update(item_id="nonexistent-task-999", status="in_progress")
        except ValueError as err:
            err_msg = str(err)
            if "Available work items:" in err_msg and t_sample["short_id"] in err_msg:
                guidance_found = True
        status_7 = "FIXED" if guidance_found else "REGRESSED"
        results["A007_unknown_task_guidance"] = {
            "status": status_7,
            "guidance_found": guidance_found
        }
    except Exception as e:
        results["A007_unknown_task_guidance"] = {"status": "REGRESSED", "error": str(e)}
    log_event("phase2", "tester", "A007_unknown_task_guidance", "task_update(unknown)", (time.perf_counter() - t0)*1000, results["A007_unknown_task_guidance"]["status"])

    # 8. MCP server startup stderr clean in headless mode
    print("Testing 8: Headless MCP server startup stderr...")
    t0 = time.perf_counter()
    try:
        env = os.environ.copy()
        env["MESH_NO_AUTO_DAEMON"] = "1"
        env["MESH_AUTO_APPROVE"] = "0"
        p_mcp = subprocess.Popen(
            [sys.executable, "-m", "mesh.mcp_server"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env
        )
        time.sleep(1.5)
        p_mcp.terminate()
        try:
            _, stderr = p_mcp.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            p_mcp.kill()
            _, stderr = p_mcp.communicate()
        status_8 = "FIXED" if "Needs your approval" not in stderr and "Warning" not in stderr else "REGRESSED"
        results["A008_B007_mcp_headless_stderr"] = {
            "status": status_8,
            "stderr_len": len(stderr.strip()),
            "stderr_content": stderr.strip()
        }
    except Exception as e:
        results["A008_B007_mcp_headless_stderr"] = {"status": "REGRESSED", "error": str(e)}
    log_event("phase2", "tester", "A008_B007_mcp_headless_stderr", "mcp_headless_stderr", (time.perf_counter() - t0)*1000, results["A008_B007_mcp_headless_stderr"]["status"])

    # 9. Defunct ONNX model mirror removed
    print("Testing 9: Defunct ONNX model mirror...")
    t0 = time.perf_counter()
    try:
        from mesh import memory
        mirrors = getattr(memory, "_GCS_TARBALL_MIRRORS", ())
        defunct_absent = all("storage.googleapis.com/qdrant-fastembed" not in m for m in mirrors)
        status_9 = "FIXED" if defunct_absent else "REGRESSED"
        results["B003_defunct_model_mirror_removed"] = {
            "status": status_9,
            "mirrors_checked": list(mirrors),
            "defunct_absent": defunct_absent
        }
    except Exception as e:
        results["B003_defunct_model_mirror_removed"] = {"status": "REGRESSED", "error": str(e)}
    log_event("phase2", "tester", "B003_defunct_model_mirror_removed", "check_mirrors", (time.perf_counter() - t0)*1000, results["B003_defunct_model_mirror_removed"]["status"])

    # Restore old MESH_HOME
    if old_mesh_home: os.environ["MESH_HOME"] = old_mesh_home
    else: os.environ.pop("MESH_HOME", None)

    # Save results
    out_file = PROJECT_DIR / "report" / "data" / "regression_sweep_results.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print("\n--- Regression Sweep Summary ---")
    for k, v in results.items():
        print(f"{k}: {v['status']}")
    return results

if __name__ == "__main__":
    test_sweep()

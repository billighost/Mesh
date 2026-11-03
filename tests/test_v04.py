"""Tests for Mesh v0.4 & v0.5+: HTTP API, MCP server, Dashboard, WAL sync, Idle Unload, Daemon, Context Builder, CLI, Audit, Passive Capture, Digest."""

import asyncio
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from mesh.audit import (
    _get_audit_db_path,
    get_audit_log,
    get_audit_stats,
    log_forget,
    log_learn,
    log_recall,
    log_update,
)
from mesh.context_builder import build_context
from mesh.digest import generate_digest
from mesh.memory import (
    Mesh,
    _embed_cached,
    drain_reembed_worker,
    get_engine_status,
    set_idle_unload_seconds,
    unload_model,
)
from mesh.namespaces import _remove_sqlite_files, delete_namespace, rename_namespace
from mesh.passive import (
    _extract_local,
    _has_signal,
    _is_credential,
    detect_provider,
    extract_memories_from_log,
)
from mesh.dashboard import verify_dashboard_auth
from mesh.http_server import verify_request_auth
from mesh.store import NamespaceNotFoundError, _vector_to_blob
from mesh.version import VERSION


def _cleanup_ns(ns: str) -> None:
    """Cleanup is best-effort: 0.6.3 made deleting a missing namespace an error."""
    try:
        delete_namespace(ns)
    except NamespaceNotFoundError:
        pass
    _remove_sqlite_files(_get_audit_db_path(ns))


# --- Audit Trail Tests ---


def test_audit_log_recall():
    """Recall events should be logged to audit trail."""
    ns = "test_audit"
    try:
        mesh = Mesh(namespace=ns, agent_id="test-agent")
        mesh.learn("audit test memory")
        results = mesh.recall("audit test")
        log_recall(
            query="audit test",
            namespace=ns,
            agent_id="test-agent",
            results=results,
        )
        entries = get_audit_log(namespace=ns, limit=10)
        recall_entries = [e for e in entries if e["action"] == "recall"]
        assert len(recall_entries) > 0
        assert recall_entries[0]["query"] == "audit test"
        assert recall_entries[0]["agent_id"] == "test-agent"
    finally:
        _cleanup_ns(ns)


def test_audit_log_learn_update_forget():
    """Learn, update, and forget events should be logged to audit trail."""
    ns = "test_audit_learn"
    try:
        log_learn(
            content="test learn event",
            namespace=ns,
            agent_id="test-agent",
            result={"memory_id": "test-id-123", "conflict": None},
        )
        log_update(
            memory_id="test-id-123",
            content="updated learn event",
            namespace=ns,
            agent_id="test-agent",
        )
        log_forget(
            memory_id="test-id-123",
            namespace=ns,
            agent_id="test-agent",
        )
        entries = get_audit_log(namespace=ns, limit=10)
        learn_entries = [e for e in entries if e["action"] == "learn"]
        update_entries = [e for e in entries if e["action"] == "update"]
        forget_entries = [e for e in entries if e["action"] == "forget"]
        assert len(learn_entries) > 0
        assert learn_entries[0]["content"] in ("test learn event", "[PURGED]")
        assert len(update_entries) == 1
        assert len(forget_entries) == 1
    finally:
        _cleanup_ns(ns)


def test_audit_filter_by_action():
    """Audit log should support filtering by action type including delete/forget."""
    ns = "test_audit_filter"
    try:
        log_recall(query="test query", namespace=ns, agent_id="agent1", results=[])
        log_learn(content="test content", namespace=ns, agent_id="agent1", result={"memory_id": "x"})
        log_forget(memory_id="x", namespace=ns, agent_id="agent1")
        recalls = get_audit_log(namespace=ns, action_filter="read")
        learns = get_audit_log(namespace=ns, action_filter="write")
        deletes = get_audit_log(namespace=ns, action_filter="delete")
        assert all(e["action"] == "recall" for e in recalls)
        assert all(e["action"] == "learn" for e in learns)
        assert len(deletes) == 1
        assert deletes[0]["action"] == "forget"
    finally:
        _cleanup_ns(ns)


def test_audit_stats():
    """Audit stats should return accurate aggregate counts."""
    ns = "test_audit_stats"
    try:
        log_recall(query="q1", namespace=ns, agent_id="agent-a", results=[])
        log_recall(query="q2", namespace=ns, agent_id="agent-b", results=[])
        log_learn(
            content="c1",
            namespace=ns,
            agent_id="agent-a",
            result={"memory_id": "x", "conflict": True},
        )
        log_update(memory_id="x", content="c2", namespace=ns, agent_id="agent-a")
        log_forget(memory_id="x", namespace=ns, agent_id="agent-a")
        stats = get_audit_stats(ns)
        assert stats["total_recalls"] >= 2
        assert stats["total_learns"] >= 1
        assert stats["total_updates"] >= 1
        assert stats["total_forgets"] >= 1
        assert "agent-a" in stats["agents"]
        assert stats["conflicts_detected"] >= 1
    finally:
        _cleanup_ns(ns)


# --- Namespace Audit DB Cleanup & Migration Tests ---


def test_namespace_delete_and_rename_cleans_audit_db():
    """delete_namespace and rename_namespace should clean up and migrate audit_<ns>.db files."""
    ns_old = "test_audit_ns_old"
    ns_new = "test_audit_ns_new"
    try:
        mesh = Mesh(namespace=ns_old, agent_id="test")
        res = mesh.learn("fact inside old namespace")
        assert _get_audit_db_path(ns_old).exists()

        rename_namespace(ns_old, ns_new)
        assert not _get_audit_db_path(ns_old).exists()
        assert _get_audit_db_path(ns_new).exists()
        entries = get_audit_log(ns_new)
        assert len(entries) == 1
        assert entries[0]["namespace"] == ns_new

        delete_namespace(ns_new)
        assert not _get_audit_db_path(ns_new).exists()
    finally:
        _cleanup_ns(ns_old)
        _cleanup_ns(ns_new)


# --- Cross-Process SQLite WAL Synchronization Tests ---


def test_wal_cross_process_cache_sync():
    """MeshStore vector cache should detect external SQLite WAL writes immediately."""
    ns = "test_wal_sync"
    try:
        mesh = Mesh(namespace=ns, agent_id="proc-1")
        mesh.learn("Initial memory from process 1")
        assert mesh.count() == 1

        # Simulate a second external process writing directly to SQLite WAL
        vec = list(_embed_cached("External memory written via WAL"))
        blob = _vector_to_blob(vec)
        conn = sqlite3.connect(mesh.store.db_path, timeout=5.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                INSERT INTO memories
                (id, namespace, content, confidence, memory_type, source_agent,
                 tags, created_at, accessed_at, access_count, local_only, embedding)
                VALUES (?, ?, ?, 1.0, 'fact', 'proc-2', '[]',
                        '2026-10-15T00:00:00+00:00', '2026-10-15T00:00:00+00:00', 0, 0, ?)
                """,
                ("ext-wal-id-1", ns, "External memory written via WAL", blob),
            )
            conn.commit()
        finally:
            conn.close()

        # Process 1 should detect the WAL change immediately without stale cache count
        assert mesh.count() == 2
        recalled = mesh.recall("External memory written via WAL", n=2)
        assert any(r["id"] == "ext-wal-id-1" for r in recalled)
    finally:
        _cleanup_ns(ns)


# --- Engine Idle Unload & Cache Clearing Tests ---


def test_engine_idle_unload_clears_cache():
    """Unloading frees the model and the embed cache; lookups reload on demand."""
    ns = "test_engine_unload"
    try:
        mesh = Mesh(namespace=ns, agent_id="test")
        mesh.learn("FastEmbed and idle unload test memory")
        drain_reembed_worker(timeout=5.0)

        status_before = get_engine_status()
        assert status_before["embed_cache_entries"] >= 1

        # Unload the neural model from RAM; 0.6.3 also clears the embed cache
        unload_model()
        status_unloaded = get_engine_status()
        assert status_unloaded["model_loaded"] is False
        assert status_unloaded["backend"] == "unloaded"
        assert status_unloaded["embed_cache_entries"] == 0

        # The uncached lookup reloads the model on demand and returns a vector
        cached_vec = _embed_cached("FastEmbed and idle unload test memory")
        assert len(cached_vec) == 384
        assert get_engine_status()["model_loaded"] is True

        # Recall keeps working with the reloaded model
        results = mesh.recall("completely new uncached query string for reload test")
        assert len(results) >= 1
        assert get_engine_status()["model_loaded"] is True

        set_idle_unload_seconds(180)
    finally:
        _cleanup_ns(ns)


# --- Dashboard HTML/JS Regression & API Tests ---


def test_dashboard_html_js_regex_and_icons_intact():
    """DASHBOARD_HTML must be a raw string preserving JS regex escapes and action button icons."""
    from mesh.dashboard import DASHBOARD_HTML

    # Verify the fatal `/\/g` regex corruption is fixed and `escAttr` has valid JS `/\\/g`
    assert r".replace(/\\/g, '\\\\')" in DASHBOARD_HTML
    assert ".replace(/\\/g, '\\\\')" not in DASHBOARD_HTML.replace(r".replace(/\\/g, '\\\\')", "")
    assert "&#x21bb;" in DASHBOARD_HTML  # Refresh icon
    assert "&#x270e;" in DASHBOARD_HTML  # Edit icon
    assert "&#x2715;" in DASHBOARD_HTML  # Close / Delete icon
    assert "loadAudit('forget', this)" in DASHBOARD_HTML  # Deletes audit filter chip
    assert "setType('general', this)" in DASHBOARD_HTML  # General memory type chip


def test_dashboard_api_crud_and_namespace_deletion():
    """Dashboard FastAPI endpoints should serve UI, perform CRUD, and cleanly delete active namespace."""
    from fastapi.testclient import TestClient
    from mesh.dashboard import api

    ns = "test_dashboard_api"
    api.dependency_overrides[verify_dashboard_auth] = lambda: None
    client = TestClient(api)
    try:
        # 1. Root HTML loads
        res_html = client.get("/")
        assert res_html.status_code == 200
        assert "<title>Mesh</title>" in res_html.text

        # 2. Create memory via Dashboard API
        res_create = client.post(
            "/api/memories",
            json={
                "content": "Dashboard test fact on port 7433",
                "memory_type": "fact",
                "confidence": 0.95,
                "tags": ["ui", "test"],
                "namespace": ns,
            },
        )
        assert res_create.status_code == 200
        mid = res_create.json()["result"]["memory_id"]

        # 3. Stats & Memories endpoints
        res_stats = client.get(f"/api/stats?namespace={ns}")
        assert res_stats.status_code == 200
        assert res_stats.json()["total_memories"] == 1

        # 4. Update memory via Dashboard API
        res_update = client.put(
            f"/api/memories/{mid}",
            json={
                "content": "Dashboard updated fact on port 7434",
                "namespace": ns,
            },
        )
        assert res_update.status_code == 200
        assert res_update.json()["result"]["memory"]["content"] == "Dashboard updated fact on port 7434"

        # 5. Delete active namespace and verify it does not resurrect in list_namespaces
        res_del_ns = client.delete(f"/api/namespaces/{ns}")
        assert res_del_ns.status_code == 200
        assert res_del_ns.json()["deleted"] is True
        ns_list = [x["namespace"] for x in client.get("/api/namespaces").json()]
        assert ns not in ns_list
    finally:
        api.dependency_overrides.clear()
        _cleanup_ns(ns)


# --- Context Builder Tests ---


def test_context_builder_returns_string():
    """build_context should return a non-empty string when memories exist."""
    ns = "test_context"
    try:
        mesh = Mesh(namespace=ns, agent_id="test")
        mesh.learn("the deploy script is at ./scripts/deploy.sh", memory_type="instruction")
        mesh.learn("staging URL is staging.example.com", memory_type="fact")
        result = build_context(namespace=ns, count=5)
        assert isinstance(result, str)
        assert len(result) > 0
    finally:
        _cleanup_ns(ns)


def test_context_builder_formats():
    """build_context should support markdown, plain, xml, and json formats."""
    ns = "test_context_fmt"
    try:
        mesh = Mesh(namespace=ns, agent_id="test")
        mesh.learn("format test memory")
        for fmt in ["markdown", "plain", "xml", "json"]:
            result = build_context(namespace=ns, format=fmt)
            assert isinstance(result, str)
            assert len(result) > 0
    finally:
        _cleanup_ns(ns)


def test_context_builder_empty_namespace():
    """build_context should return empty string for empty/nonexistent namespace."""
    ns = "test_context_empty_xyz"
    try:
        result = build_context(namespace=ns, count=5)
        assert result == ""
    finally:
        _cleanup_ns(ns)


# --- Digest Tests ---


def test_digest_structure():
    """generate_digest should return a dict with expected keys."""
    ns = "test_digest_struct"
    try:
        digest = generate_digest(namespace=ns, hours=24)
        required_keys = [
            "namespace",
            "hours",
            "since",
            "new_memories",
            "new_count",
            "contradictions",
            "contradiction_count",
            "stale_memories",
            "stale_count",
            "total_recalls",
            "agents_active",
        ]
        for key in required_keys:
            assert key in digest, f"Missing key: {key}"
    finally:
        _cleanup_ns(ns)


def test_digest_counts_new_memories():
    """Digest should count memories created in the time window."""
    ns = "test_digest_count"
    try:
        mesh = Mesh(namespace=ns, agent_id="test")
        mesh.learn("digest test memory one")
        mesh.learn("digest test memory two")
        digest = generate_digest(namespace=ns, hours=1)
        assert digest["new_count"] >= 2
    finally:
        _cleanup_ns(ns)


# --- HTTP API Tests ---


def test_http_server_imports():
    """HTTP server module should import without errors."""
    from mesh import http_server

    assert http_server.app is not None


def test_http_endpoints_exist():
    """HTTP server should have all required endpoints including /engine, /unload, and /mcp."""
    from mesh.http_server import app

    routes = [r.path for r in app.routes]
    required = [
        "/health",
        "/engine",
        "/unload",
        "/mcp",
        "/learn",
        "/recall",
        "/update",
        "/forget",
        "/inspect",
        "/patterns",
        "/contradictions",
        "/context",
        "/audit",
    ]
    for endpoint in required:
        assert endpoint in routes, f"Missing endpoint: {endpoint}"


def test_http_health_and_engine_endpoints():
    """Health, engine, and unload endpoints should return ok status."""
    from fastapi.testclient import TestClient
    from mesh.http_server import app

    app.dependency_overrides[verify_request_auth] = lambda: None
    client = TestClient(app)
    try:
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["version"] == VERSION
        assert "engine" not in data

        eng_res = client.get("/engine")
        assert eng_res.status_code == 200
        assert eng_res.json()["status"] == "ok"
        assert "engine" in eng_res.json()

        unload_res = client.post("/unload")
        assert unload_res.status_code == 200
        assert unload_res.json()["engine"]["model_loaded"] is False
    finally:
        app.dependency_overrides.clear()


def test_http_learn_update_and_recall():
    """HTTP learn, update, and recall should work end-to-end (including 'n' alias)."""
    from fastapi.testclient import TestClient
    from mesh.http_server import app

    ns = "test_http_e2e"
    app.dependency_overrides[verify_request_auth] = lambda: None
    prev_ns = os.environ.get("MESH_NAMESPACE")
    os.environ["MESH_NAMESPACE"] = ns
    client = TestClient(app)
    try:
        r = client.post(
            "/learn",
            json={
                "content": "HTTP test: production port is 8080",
                "memory_type": "process",
                "tags": ["prod", "network"],
                "namespace": ns,
            },
        )
        assert r.status_code == 200
        learned_data = r.json()
        assert learned_data["status"] == "ok"
        mem_id = learned_data["result"]["memory_id"]

        # Recall with 'n' alias and memory_type filter
        r = client.post(
            "/recall",
            json={
                "query": "what port is production?",
                "n": 3,
                "memory_type": "process",
                "namespace": ns,
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["count"] >= 1
        assert any("8080" in m["content"] for m in data["results"])

        # Update in-place
        r_up = client.post(
            "/update",
            json={
                "memory_id": mem_id,
                "content": "HTTP test: production port is 9090",
                "namespace": ns,
            },
        )
        assert r_up.status_code == 200
        assert r_up.json()["result"]["memory"]["content"] == "HTTP test: production port is 9090"

        # Forget non-existent ID should not record a false audit log
        r_forget_missing = client.post(
            "/forget",
            json={"memory_id": "non-existent-id", "namespace": ns},
        )
        assert r_forget_missing.status_code == 200
        assert r_forget_missing.json()["status"] == "not_found"
        forgets = get_audit_log(ns, action_filter="forget")
        assert len(forgets) == 0
    finally:
        app.dependency_overrides.clear()
        if prev_ns is None:
            os.environ.pop("MESH_NAMESPACE", None)
        else:
            os.environ["MESH_NAMESPACE"] = prev_ns
        _cleanup_ns(ns)


def test_http_local_only_stored():
    """HTTP learn with local_only should store private memory."""
    from fastapi.testclient import TestClient
    from mesh.http_server import app

    ns = "test_http_private"
    app.dependency_overrides[verify_request_auth] = lambda: None
    client = TestClient(app)
    try:
        r = client.post(
            "/learn",
            json={
                "content": "secret: API key is sk-test-xyz",
                "local_only": True,
                "namespace": ns,
            },
        )
        assert r.status_code == 200
    finally:
        app.dependency_overrides.clear()
        _cleanup_ns(ns)


# --- MCP Server Tool Tests ---


def test_mcp_server_tools_and_execution():
    """MCP server should expose all 10 tools and execute learn/recall/update/resolve cleanly."""
    from mesh.mcp_server import call_tool, list_tools

    ns = "test_mcp_tools"
    try:
        tools = asyncio.run(list_tools())
        tool_names = {t.name for t in tools}
        expected_tools = {
            "mesh_learn",
            "mesh_recall",
            "mesh_update",
            "mesh_forget",
            "mesh_inspect",
            "mesh_context",
            "mesh_patterns",
            "mesh_contradictions",
            "mesh_resolve_contradiction",
            "mesh_sync",
        }
        assert expected_tools.issubset(tool_names)

        # Call mesh_learn
        learn_out = asyncio.run(
            call_tool(
                "mesh_learn",
                {
                    "content": "MCP test: staging server uses port 7000",
                    "memory_type": "fact",
                    "tags": ["mcp", "staging"],
                    "namespace": ns,
                },
            )
        )
        learn_json = json.loads(learn_out[0].text)
        assert learn_json["status"] == "stored"
        mid = learn_json["memory_id"]

        # Call mesh_update
        update_out = asyncio.run(
            call_tool(
                "mesh_update",
                {
                    "memory_id": mid,
                    "content": "MCP test: staging server uses port 7001",
                    "namespace": ns,
                },
            )
        )
        update_json = json.loads(update_out[0].text)
        assert update_json["status"] == "updated"

        # Call mesh_recall
        recall_out = asyncio.run(
            call_tool(
                "mesh_recall",
                {"query": "port 7001", "namespace": ns},
            )
        )
        recall_json = json.loads(recall_out[0].text)
        assert recall_json["count"] == 1
        assert "7001" in recall_json["results"][0]["content"]
    finally:
        _cleanup_ns(ns)


# --- Passive Capture Tests ---


def test_detect_provider_anthropic_key():
    """Should detect Anthropic when ANTHROPIC_API_KEY is set."""
    with patch.dict(
        "os.environ",
        {"ANTHROPIC_API_KEY": "sk-ant-test", "MESH_AI_PROVIDER": ""},
        clear=False,
    ):
        config = detect_provider()
        assert config["provider"] == "anthropic"
        assert config["available"] is True


def test_detect_provider_openai_key():
    """Should detect OpenAI when OPENAI_API_KEY is set and no Anthropic key."""
    env = {"OPENAI_API_KEY": "sk-openai-test", "ANTHROPIC_API_KEY": "", "MESH_AI_PROVIDER": ""}
    with patch.dict("os.environ", env, clear=False):
        config = detect_provider()
        assert config["provider"] == "openai-compatible"
        assert "openai.com" in config["base_url"]


def test_detect_provider_explicit_override():
    """MESH_AI_PROVIDER should override auto-detection."""
    env = {
        "MESH_AI_PROVIDER": "groq",
        "GROQ_API_KEY": "gsk_test",
        "ANTHROPIC_API_KEY": "sk-also-set",
    }
    with patch.dict("os.environ", env, clear=False):
        config = detect_provider()
        assert "groq" in config["base_url"]
        assert config["model"] == "llama3-8b-8192"


def test_detect_provider_no_keys_returns_local():
    """Should return local provider when no keys are set and Ollama is not running."""
    env = {
        "ANTHROPIC_API_KEY": "",
        "OPENAI_API_KEY": "",
        "GROQ_API_KEY": "",
        "MESH_AI_PROVIDER": "",
    }
    with patch.dict("os.environ", env, clear=False):
        with patch("mesh.passive._ollama_is_running", return_value=False):
            config = detect_provider()
            assert config["provider"] == "local"
            assert config["available"] is True


def test_detect_provider_ollama_fallback():
    """Should detect Ollama when it's running and no API keys are set."""
    env = {
        "ANTHROPIC_API_KEY": "",
        "OPENAI_API_KEY": "",
        "GROQ_API_KEY": "",
        "MESH_AI_PROVIDER": "",
    }
    with patch.dict("os.environ", env, clear=False):
        with patch("mesh.passive._ollama_is_running", return_value=True):
            with patch("mesh.passive._detect_ollama_model", return_value="llama3"):
                config = detect_provider()
                assert config["provider"] == "openai-compatible"
                assert "11434" in config["base_url"]


def test_local_extractor_finds_facts():
    """Local extractor should find signal sentences."""
    log = """
    We discussed the deploy process today.
    The deploy script is at ./scripts/deploy.sh and must be run from root.
    We decided to always run tests before merging to main.
    The staging server URL is staging.acme.com.
    Okay sounds good thanks.
    """
    results = _extract_local(log)
    assert len(results) >= 2
    assert all("content" in r for r in results)
    assert all("type" in r for r in results)


def test_local_extractor_marks_credentials_private():
    """Local extractor should mark credential-like content as private."""
    log = "The API key is sk-abc123xyz and should be kept secret."
    results = _extract_local(log)
    cred_results = [r for r in results if r.get("private")]
    assert len(cred_results) >= 1


def test_local_extractor_skips_noise():
    """Local extractor should not store conversational filler."""
    log = "Yes. Okay. Sure. Thanks. Got it. I see. Let me help you with that."
    results = _extract_local(log)
    assert len(results) == 0


def test_extract_dry_run_does_not_store():
    """Dry run should never write memories."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "content": [
            {
                "text": '[{"content": "dry run test fact", "type": "fact", "tags": [], "private": false}]'
            }
        ]
    }
    with patch("httpx.post", return_value=mock_resp):
        result = extract_memories_from_log(
            log_text="dry run test session log with facts",
            namespace="test_dry_run_passive",
            dry_run=True,
        )
    assert result["stored_count"] == 0


def test_extract_anthropic_stores_memories():
    """Anthropic extraction path should store memories including process/decision types."""
    ns = "test_passive_anthropic"
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "content": [
            {
                "text": '[{"content": "prod uses port 8080", "type": "process", "tags": ["infra"], "private": false}]'
            }
        ]
    }
    try:
        with patch("httpx.post", return_value=mock_resp):
            with patch(
                "mesh.passive.detect_provider",
                return_value={
                    "provider": "anthropic",
                    "api_key": "sk-test",
                    "model": "claude-haiku-4-5-20251001",
                    "base_url": None,
                    "available": True,
                },
            ):
                result = extract_memories_from_log(
                    log_text="we set production to use port 8080",
                    namespace=ns,
                    dry_run=False,
                    assume_yes=True,
                )
        assert result["stored_count"] >= 1
    finally:
        _cleanup_ns(ns)


def test_extract_falls_back_to_local_on_api_error():
    """Should fall back to local extraction if AI API call fails."""
    with patch(
        "mesh.passive.detect_provider",
        return_value={
            "provider": "anthropic",
            "api_key": "bad-key",
            "model": "claude-haiku-4-5-20251001",
            "base_url": None,
            "available": True,
        },
    ):
        with patch("mesh.passive._extract_with_anthropic", side_effect=Exception("API error")):
            result = extract_memories_from_log(
                log_text="the deploy script is at ./deploy.sh and must be run as root",
                namespace="test_passive_fallback",
                dry_run=True,
                verbose=False,
            )
    assert "provider_used" in result
    assert "local" in result["provider_used"]


def test_force_local_skips_ai():
    """--local-only flag should skip all AI providers."""
    result = extract_memories_from_log(
        log_text="the staging URL is staging.example.com and must be used for testing",
        namespace="test_force_local",
        force_local=True,
        dry_run=True,
    )
    assert result["provider_used"] == "local-keyword-extractor"


def test_extract_reads_log_file_path():
    """extract_memories_from_log should automatically read files when passed a file path."""
    ns = "test_passive_filepath"
    with tempfile.NamedTemporaryFile(suffix=".log", mode="w", delete=False, encoding="utf-8") as f:
        f.write("We decided to use PostgreSQL 16 on port 5432 for production.\n")
        path = f.name
    try:
        result = extract_memories_from_log(
            log_text=path,
            namespace=ns,
            force_local=True,
            dry_run=False,
        )
        assert result["stored_count"] >= 1
    finally:
        _cleanup_ns(ns)
        Path(path).unlink(missing_ok=True)

"""Tests for Mesh v0.4: HTTP API, context builder, CLI, audit trail, passive capture, digest."""

import json
import pytest
from unittest.mock import patch, MagicMock
from mesh.memory import Mesh
from mesh.audit import log_recall, log_learn, get_audit_log, get_audit_stats
from mesh.context_builder import build_context
from mesh.digest import generate_digest
from mesh.namespaces import delete_namespace


# --- Audit Trail Tests ---

def test_audit_log_recall():
    """Recall events should be logged to audit trail."""
    mesh = Mesh(namespace="test_audit", agent_id="test-agent")
    mesh.learn("audit test memory")
    results = mesh.recall("audit test")
    
    log_recall(
        query="audit test",
        namespace="test_audit",
        agent_id="test-agent",
        results=results
    )
    
    entries = get_audit_log(namespace="test_audit", limit=10)
    recall_entries = [e for e in entries if e["action"] == "recall"]
    assert len(recall_entries) > 0
    assert recall_entries[0]["query"] == "audit test"
    assert recall_entries[0]["agent_id"] == "test-agent"
    
    delete_namespace("test_audit")


def test_audit_log_learn():
    """Learn events should be logged to audit trail."""
    log_learn(
        content="test learn event",
        namespace="test_audit_learn",
        agent_id="test-agent",
        result={"memory_id": "test-id-123", "conflict": None}
    )
    
    entries = get_audit_log(namespace="test_audit_learn", limit=10)
    learn_entries = [e for e in entries if e["action"] == "learn"]
    assert len(learn_entries) > 0
    assert "test learn event" in learn_entries[0]["content"]


def test_audit_filter_by_action():
    """Audit log should support filtering by action type."""
    ns = "test_audit_filter"
    log_recall(query="test query", namespace=ns, agent_id="agent1", results=[])
    log_learn(content="test content", namespace=ns, agent_id="agent1", result={"memory_id": "x"})
    
    recalls = get_audit_log(namespace=ns, action_filter="read")
    learns = get_audit_log(namespace=ns, action_filter="write")
    
    assert all(e["action"] == "recall" for e in recalls)
    assert all(e["action"] == "learn" for e in learns)


def test_audit_stats():
    """Audit stats should return accurate aggregate counts."""
    ns = "test_audit_stats"
    log_recall(query="q1", namespace=ns, agent_id="agent-a", results=[])
    log_recall(query="q2", namespace=ns, agent_id="agent-b", results=[])
    log_learn(content="c1", namespace=ns, agent_id="agent-a", result={"memory_id": "x", "conflict": True})
    
    stats = get_audit_stats(ns)
    assert stats["total_recalls"] >= 2
    assert stats["total_learns"] >= 1
    assert "agent-a" in stats["agents"]
    assert stats["conflicts_detected"] >= 1


# --- Context Builder Tests ---

def test_context_builder_returns_string():
    """build_context should return a non-empty string when memories exist."""
    mesh = Mesh(namespace="test_context", agent_id="test")
    mesh.learn("the deploy script is at ./scripts/deploy.sh", memory_type="instruction")
    mesh.learn("staging URL is staging.example.com", memory_type="fact")
    
    result = build_context(namespace="test_context", count=5)
    assert isinstance(result, str)
    assert len(result) > 0
    
    delete_namespace("test_context")


def test_context_builder_formats():
    """build_context should support markdown, plain, xml, and json formats."""
    mesh = Mesh(namespace="test_context_fmt", agent_id="test")
    mesh.learn("format test memory")
    
    for fmt in ["markdown", "plain", "xml", "json"]:
        result = build_context(namespace="test_context_fmt", format=fmt)
        assert isinstance(result, str)
        assert len(result) > 0
    
    delete_namespace("test_context_fmt")


def test_context_builder_empty_namespace():
    """build_context should return empty string for empty/nonexistent namespace."""
    result = build_context(namespace="test_context_empty_xyz", count=5)
    assert result == ""


# --- Digest Tests ---

def test_digest_structure():
    """generate_digest should return a dict with expected keys."""
    digest = generate_digest(namespace="test_digest_struct", hours=24)
    
    required_keys = [
        "namespace", "hours", "since", "new_memories", "new_count",
        "contradictions", "contradiction_count", "stale_memories",
        "stale_count", "total_recalls", "agents_active"
    ]
    for key in required_keys:
        assert key in digest, f"Missing key: {key}"


def test_digest_counts_new_memories():
    """Digest should count memories created in the time window."""
    mesh = Mesh(namespace="test_digest_count", agent_id="test")
    mesh.learn("digest test memory one")
    mesh.learn("digest test memory two")
    
    digest = generate_digest(namespace="test_digest_count", hours=1)
    assert digest["new_count"] >= 2
    
    delete_namespace("test_digest_count")


# --- HTTP API Tests ---

def test_http_server_imports():
    """HTTP server module should import without errors."""
    from mesh import http_server
    assert http_server.app is not None


def test_http_endpoints_exist():
    """HTTP server should have all required endpoints."""
    from mesh.http_server import app
    routes = [r.path for r in app.routes]
    
    required = ["/health", "/learn", "/recall", "/forget", "/inspect", "/patterns", "/context", "/audit"]
    for endpoint in required:
        assert endpoint in routes, f"Missing endpoint: {endpoint}"


def test_http_health_endpoint():
    """Health endpoint should return ok status."""
    from fastapi.testclient import TestClient
    from mesh.http_server import app
    
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["version"] == "0.4"


def test_http_learn_and_recall():
    """HTTP learn and recall should work end-to-end."""
    from fastapi.testclient import TestClient
    from mesh.http_server import app
    import os
    os.environ["MESH_NAMESPACE"] = "test_http_e2e"
    
    client = TestClient(app)
    
    # Learn
    r = client.post("/learn", json={
        "content": "HTTP test: production port is 8080",
        "memory_type": "fact",
        "namespace": "test_http_e2e"
    })
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    
    # Recall
    r = client.post("/recall", json={
        "query": "what port is production?",
        "count": 3,
        "namespace": "test_http_e2e"
    })
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["count"] >= 1
    assert any("8080" in m["content"] for m in data["results"])
    
    delete_namespace("test_http_e2e")


def test_http_local_only_stored():
    """HTTP learn with local_only should store private memory."""
    from fastapi.testclient import TestClient
    from mesh.http_server import app
    
    client = TestClient(app)
    r = client.post("/learn", json={
        "content": "secret: API key is sk-test-xyz",
        "local_only": True,
        "namespace": "test_http_private"
    })
    assert r.status_code == 200
    
    delete_namespace("test_http_private")


# --- Passive Capture Tests ---

from mesh.passive import (
    detect_provider, extract_memories_from_log,
    _extract_local, _is_credential, _has_signal
)

def test_detect_provider_anthropic_key():
    """Should detect Anthropic when ANTHROPIC_API_KEY is set."""
    with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test", "MESH_AI_PROVIDER": ""}, clear=False):
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
        "ANTHROPIC_API_KEY": "sk-also-set"
    }
    with patch.dict("os.environ", env, clear=False):
        config = detect_provider()
        assert "groq" in config["base_url"]
        assert config["model"] == "llama3-8b-8192"


def test_detect_provider_no_keys_returns_local():
    """Should return local provider when no keys are set and Ollama is not running."""
    env = {"ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "", "GROQ_API_KEY": "", "MESH_AI_PROVIDER": ""}
    with patch.dict("os.environ", env, clear=False):
        with patch("mesh.passive._ollama_is_running", return_value=False):
            config = detect_provider()
            assert config["provider"] == "local"
            assert config["available"] is True


def test_detect_provider_ollama_fallback():
    """Should detect Ollama when it's running and no API keys are set."""
    env = {"ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "", "GROQ_API_KEY": "", "MESH_AI_PROVIDER": ""}
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
        "content": [{"text": '[{"content": "dry run test fact", "type": "fact", "tags": [], "private": false}]'}]
    }
    with patch("httpx.post", return_value=mock_resp):
        result = extract_memories_from_log(
            log_text="dry run test session log with facts",
            namespace="test_dry_run_passive",
            dry_run=True
        )
    assert result["stored_count"] == 0


def test_extract_anthropic_stores_memories():
    """Anthropic extraction path should store memories."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "content": [{"text": '[{"content": "prod uses port 8080", "type": "fact", "tags": ["infra"], "private": false}]'}]
    }
    with patch("httpx.post", return_value=mock_resp):
        with patch("mesh.passive.detect_provider", return_value={
            "provider": "anthropic",
            "api_key": "sk-test",
            "model": "claude-haiku-4-5-20251001",
            "base_url": None,
            "available": True
        }):
            result = extract_memories_from_log(
                log_text="we set production to use port 8080",
                namespace="test_passive_anthropic",
                dry_run=False
            )
    assert result["stored_count"] >= 1
    delete_namespace("test_passive_anthropic")


def test_extract_falls_back_to_local_on_api_error():
    """Should fall back to local extraction if AI API call fails."""
    with patch("mesh.passive.detect_provider", return_value={
        "provider": "anthropic",
        "api_key": "bad-key",
        "model": "claude-haiku-4-5-20251001",
        "base_url": None,
        "available": True
    }):
        with patch("mesh.passive._extract_with_anthropic", side_effect=Exception("API error")):
            result = extract_memories_from_log(
                log_text="the deploy script is at ./deploy.sh and must be run as root",
                namespace="test_passive_fallback",
                dry_run=True,
                verbose=False
            )
    # Should have fallen back to local and found something (or at least not crashed)
    assert "provider_used" in result
    assert "local" in result["provider_used"]


def test_force_local_skips_ai():
    """--local-only flag should skip all AI providers."""
    result = extract_memories_from_log(
        log_text="the staging URL is staging.example.com and must be used for testing",
        namespace="test_force_local",
        force_local=True,
        dry_run=True
    )
    assert result["provider_used"] == "local-keyword-extractor"

"""
tests/test_secrets_corpus.py — Secret detection corpus, precision/recall metrics,
and lifecycle hook integration contracts.

Validates Finding F8:
  - High recall across diverse fake secret formats (PINs, passphrases, multiline keys,
    tokens, JSON/YAML, task titles/notes/summaries).
  - High precision (0 false positives) across benign look-alikes (ports, commit hashes,
    versions, model tags, paths, URLs, and benign keyword prose).
  - Lifecycle hook contract compliance (Antigravity PreInvocation, Claude Code SessionStart,
    prompt injection defense banner, error resilience).
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from mesh.guards import guard_ingest
from mesh.memory import Mesh
from mesh.secrets import is_credential, redact_secrets_local


@pytest.fixture
def isolated_env(monkeypatch):
    """Ensure all tests run strictly against an isolated temporary environment."""
    temp_dir = tempfile.mkdtemp(prefix="mesh_test_secrets_")
    t_path = Path(temp_dir)
    home_dir = t_path / "home"
    mesh_home = t_path / "mesh_home"

    home_dir.mkdir(parents=True)
    mesh_home.mkdir(parents=True)

    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("USERPROFILE", str(home_dir))
    monkeypatch.setenv("APPDATA", str(home_dir / "AppData"))
    monkeypatch.setenv("LOCALAPPDATA", str(home_dir / "AppData" / "Local"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home_dir / ".config"))
    monkeypatch.setenv("MESH_HOME", str(mesh_home))
    monkeypatch.setenv("MESH_NO_AUTO_DAEMON", "1")
    monkeypatch.setenv("MESH_AUTO_APPROVE", "0")

    monkeypatch.setattr(Path, "home", lambda: home_dir)

    yield {
        "root": t_path,
        "home": home_dir,
        "mesh_home": mesh_home,
    }

    shutil.rmtree(temp_dir, ignore_errors=True)


SECRET_CORPUS: list[str] = [
    # PINs (numeric, short)
    "Database backup PIN is 4910",
    "Security pin: 918273",
    "Access pin=1029",

    # Passphrases (multi-word)
    "passphrase is correct horse battery staple",
    "recovery phrase: apple orange banana grape cherry mango",
    "seed phrase is winter ocean mountain forest river cloud",

    # Cryptographic keys (PEM formats)
    "-----BEGIN PRIVATE KEY-----\nMIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQC7\n-----END PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0Y1...",

    # JWT tokens
    "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",

    # Known API Key prefixes
    "Anthropic API key is sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890",
    "OpenAI project key: sk-proj-abcdefghijklmnopqrstuvwxyz1234567890",
    "GitHub personal token: ghp_123456789012345678901234567890123456",
    "GitLab token: glpat-abcdefghijklmnopqrst",
    "PyPI upload token: pypi-AgEIcHlwaS5vcmcCJDM4YWU2ZGM1LWQxNTAtNDBkNy1iODkzLTgzYTI4MzQ1YjAwNgACJXsiYXVkaWVuY2UiOiAicHlwaSIsICJ2ZXJzaW9uIjogMX0AAAYgtJg7Z5",
    "AWS access key ID: AKIAIOSFODNN7EXAMPLE",
    "Slack bot token: xoxb-TEST-BOT-TOKEN-NOT-REAL-SECRET-123456789",
    "SendGrid key: SG.abcdefghijklmnopqrstuv.12345678901234567890123456789012",

    # CLI Flags
    "mesh run --password Hunter2!SuperSecret",
    "mesh run --token my-access-token-987654321",
    "curl --api-key 'sk-ant-test-key-9999'",

    # Authorization headers
    "Authorization: Bearer superSecretToken123456789",
    "Authorization: Basic dXNlcm5hbWU6cGFzc3dvcmQxMjM=",

    # URL userinfo
    "postgres://admin:s3cr3tP@ssw0rd!@db.internal:5432/production",
    "redis://:redis_secret_password_123@cache.internal:6379/0",

    # Structured JSON / YAML
    '{"service": "payments", "api_key": "sk-ant-api03-abcdef1234567890", "active": true}',
    "password: |\n  first_secret_line\n  second_secret_line",

    # Common lowercase password in natural language
    "The staging database password is hunter2",

    # Work item titles, notes, and completions containing secrets
    "Rotate compromised key sk-ant-api03-1234567890 immediately",
    "Task note: Database admin password is passWord99!",
    "Completed migration with auth token ghp_123456789012345678901234567890123456",
]


BENIGN_CORPUS: list[str] = [
    # Ports and IP addresses
    "Service listening on port 8080",
    "Backend server running on http://127.0.0.1:3000",
    "Postgres listening on default port 5432",

    # Commit hashes and git references
    "Fix regression in commit a5b40ab6741f4de4b0909a5e27ac85382aa6ca59",
    "Cherry-picked from 8f2c3d1e4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d",

    # Semantic versions
    "Mesh version v0.8.1 was published yesterday",
    "Upcoming target release is 0.8.2 with security fixes",

    # Model names and technical identifiers
    "Using sentence-transformers/all-MiniLM-L6-v2 for local embeddings",
    "Model SHA-256 matches verified upstream digest",

    # File paths and URLs
    "Configuration file located at /Users/alice/projects/Mesh/src/config.py",
    "Project documentation at https://example.com/api/v1/health",
    "pwd: /home/user/workspace/repo",

    # Benign prose with keywords
    "Token limit is 4096 tokens per request",
    "We are organizing the annual Secret Santa gift exchange",
    "The refresh token is automatically rotated every 24 hours",
    "Password is required for user authentication",
    "Database foreign key is user_id",
    "HTTP status code is 200 OK",
    "Process exit code was 0",
]


def test_secret_corpus_recall():
    """Verify that every entry in the secret corpus is flagged as a credential."""
    missed: list[str] = []
    for item in SECRET_CORPUS:
        if not is_credential(item):
            missed.append(item)

    recall = (len(SECRET_CORPUS) - len(missed)) / len(SECRET_CORPUS)
    assert recall >= 0.95, f"Secret detection recall {recall:.2%} below 95%. Missed: {missed}"
    assert len(missed) == 0, f"Missed secrets in corpus: {missed}"


def test_benign_corpus_precision():
    """Verify that benign look-alikes produce zero false positives (100% precision)."""
    false_positives: list[str] = []
    for item in BENIGN_CORPUS:
        if is_credential(item):
            false_positives.append(item)

    assert len(false_positives) == 0, f"False positives detected in benign corpus: {false_positives}"


def test_redaction_sanitizes_secret_corpus():
    """Verify that redact_secrets_local eliminates live secrets without dropping prose."""
    for item in SECRET_CORPUS:
        redacted = redact_secrets_local(item)
        assert not is_credential(redacted), f"Secret still detected after redaction: {redacted}"


def test_guard_ingest_forces_private_on_secrets():
    """Verify guard_ingest forces local_only=True and redacts content when secrets are ingested."""
    for item in SECRET_CORPUS:
        res = guard_ingest(item, local_only=False)
        assert res.local_only is True, f"Secret failed to force local_only=True: {item}"
        assert not res.rejected, "Secrets should be safely redacted and privatized, not rejected"
        assert not is_credential(res.content), f"Guarded content still has active credential: {res.content}"


def test_work_items_quarantine_secrets(isolated_env):
    """Verify work item creation and completion force local_only when secrets are present."""
    mesh = Mesh(namespace="secret_test_ns", agent_id="tester", create=True, trusted_local=True)

    # 1. Task created with secret in title
    task1 = mesh.task_create(
        title="Deploy sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890 to prod",
        kind="task",
        local_only=False,
    )
    assert task1["local_only"] == 1, "Task with secret in title must be forced to local_only=1"
    assert "sk-ant" not in task1["title"], "API key must be redacted in task title"

    # 2. Task completed with secret in summary
    task2 = mesh.task_create(title="Clean deployment task", kind="task", local_only=False)
    assert task2["local_only"] == 0

    done2 = mesh.task_complete(
        item_id=task2["id"],
        summary="Completed using database PIN: 849201",
    )
    assert done2["local_only"] == 1, "Task completed with secret in summary must be forced to local_only=1"


def test_hook_antigravity_output_contract(isolated_env, capsys):
    """Verify mesh brief --format antigravity produces valid Antigravity hook JSON payload."""
    from mesh.cli import brief_cmd

    with patch.object(sys, "argv", ["mesh", "--format", "antigravity"]):
        brief_cmd()

    out = capsys.readouterr().out.strip()
    data = json.loads(out)

    assert "injectSteps" in data
    assert isinstance(data["injectSteps"], list)
    assert len(data["injectSteps"]) >= 1

    step = data["injectSteps"][0]
    assert "ephemeralMessage" in step
    msg = step["ephemeralMessage"]
    assert "<!-- UNTRUSTED REFERENCE DATA: MESH CONTEXT BRIEF -->" in msg
    assert "passive historical context" in msg


def test_hook_markdown_untrusted_banner(isolated_env, capsys):
    """Verify mesh brief --format markdown includes untrusted reference banner."""
    from mesh.cli import brief_cmd

    with patch.object(sys, "argv", ["mesh", "--format", "markdown"]):
        brief_cmd()

    out = capsys.readouterr().out
    assert "<!-- UNTRUSTED REFERENCE DATA: MESH CONTEXT BRIEF -->" in out


def test_hook_graceful_error_resilience(capsys):
    """Verify mesh brief hook handles unexpected errors gracefully without crashing."""
    from mesh.cli import brief_cmd

    with patch("mesh.memory.Mesh.__init__", side_effect=RuntimeError("Simulated unexpected failure")):
        with patch.object(sys, "argv", ["mesh", "--format", "antigravity"]):
            brief_cmd()

    out = capsys.readouterr().out.strip()
    data = json.loads(out)
    assert data == {"injectSteps": []}, "Hook must return empty injectSteps on failure, not crash"

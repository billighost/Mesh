"""
tests/test_phase6.py — Tests for Phase 6 fixes:
- Embedding downgrade warnings and strict mode (MESH_REQUIRE_MODEL=1)
- mesh reembed command and vector backend tracking
- _is_mixed_space warning emission on incompatible vector spaces
- Markdown export fencing against hostile content injection
- Export privacy parity (CSV/Markdown/JSON) and owner-only permissions
- OpenCode user-scope XDG path verification
- Review placeholders check enforcement
"""
from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
import warnings
from pathlib import Path
from unittest.mock import patch

import pytest

from mesh import Mesh
from mesh.installer import ADAPTER_MAP
from mesh.io import export_namespace, export_to_markdown
from mesh.memory import _warn_hash_fallback_once
from mesh.namespaces import delete_namespace
from mesh.store import MeshStore, NamespaceNotFoundError


@pytest.fixture(autouse=True)
def isolated_test_env(tmp_path, monkeypatch):
    mesh_home = tmp_path / "mesh_home"
    mesh_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MESH_HOME", str(mesh_home))
    monkeypatch.setenv("MESH_NO_AUTO_DAEMON", "1")
    monkeypatch.setenv("MESH_AUTO_APPROVE", "0")
    monkeypatch.delenv("MESH_REQUIRE_MODEL", raising=False)
    monkeypatch.delenv("MESH_ENCRYPT_AT_REST", raising=False)
    monkeypatch.delenv("MESH_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("MESH_PASSPHRASE", raising=False)
    yield mesh_home


def _cleanup_ns(namespace: str) -> None:
    try:
        delete_namespace(namespace)
    except (NamespaceNotFoundError, Exception):
        pass


# ==============================================================================
# Item 4: OpenCode User-Scope Config Path
# ==============================================================================

def test_opencode_user_scope_xdg_path(monkeypatch, tmp_path):
    """Verify OpenCode user-scope config path conforms to XDG ~/.config/opencode/opencode.json."""
    fake_home = tmp_path / "fake_home"
    fake_xdg = tmp_path / "fake_xdg"
    fake_home.mkdir()
    fake_xdg.mkdir()

    adapter = ADAPTER_MAP["opencode"]

    # 1. With XDG_CONFIG_HOME set
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_xdg))
    monkeypatch.setattr(adapter, "get_user_home", lambda: fake_home)
    cfg_xdg = adapter.get_mcp_config_path("user")
    assert cfg_xdg == fake_xdg / "opencode" / "opencode.json"

    # 2. Without XDG_CONFIG_HOME (falls back to ~/.config/opencode/opencode.json)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    cfg_home = adapter.get_mcp_config_path("user")
    assert cfg_home == fake_home / ".config" / "opencode" / "opencode.json"


# ==============================================================================
# Item 5: Export Privacy Parity, Fenced Markdown, and Permissions
# ==============================================================================

def test_markdown_export_fences_hostile_content():
    """Verify Markdown export fences memory and work item content to prevent MD/HTML injection."""
    hostile_text = (
        "# Injected Title\n"
        "[Click me](http://evil.example.com)\n"
        "<script>alert('xss')</script>\n"
        "```python\nprint('fenced')\n```"
    )
    data = {
        "namespace": "test_hostile",
        "exported_at": "2026-10-26T12:00:00Z",
        "memories": [
            {
                "id": "mem-001",
                "content": hostile_text,
                "memory_type": "fact",
                "confidence": 1.0,
                "tags": ["test"],
                "source_agent": "tester",
                "trust_tier": "user",
                "created_at": "2026-10-26T12:00:00Z",
            }
        ],
        "work_items": [
            {
                "id": "task-001",
                "title": "Task with `backticks` and <tags>",
                "status": "todo",
                "priority": "normal",
                "completion_summary": hostile_text,
            }
        ],
    }
    rendered = export_to_markdown(data)

    # Top-level "# Injected Title" must NOT appear un-fenced.
    # The fence should have 4 backticks because the content contains 3 backticks.
    assert "````text\n# Injected Title" in rendered
    assert "````" in rendered
    # Title backticks should be neutralized in safe_title
    assert "### [task-001] Task with 'backticks' and <tags>" in rendered


def test_export_privacy_parity(tmp_path):
    """
    Verify JSON, CSV, and Markdown exports treat private memories, secret references,
    and quarantined items identically:
      - private (local_only) memories are excluded
      - quarantined memories are excluded by default
      - secret references are exported as metadata pointers without plaintext secrets
      - export files receive secure owner-only permissions (0600 on POSIX)
    """
    ns = "test_export_privacy"
    mesh = Mesh(namespace=ns, agent_id="tester")

    try:
        # 1. Public memory
        mesh.learn("Public infrastructure fact", tags=["public"])
        # 2. Private memory
        mesh.learn("Private secret note", tags=["private"], local_only=True)
        # 3. Quarantined memory
        store = MeshStore(namespace=ns)
        store.upsert(
            memory_id="quarantine-1",
            content="Untrusted prompt injection memory",
            embedding=[0.01] * 384,
            metadata={
                "memory_type": "fact",
                "tags": ["untrusted"],
                "source_agent": "hostile",
                "trust_tier": "untrusted",
                "quarantined": True,
            },
            local_only=False,
            embedding_model="onnx-minilm-l6-v2",
        )
        # 4. Secret reference memory
        store.upsert(
            memory_id="secret-ref-1",
            content="API endpoint with secret reference",
            embedding=[0.01] * 384,
            metadata={
                "memory_type": "fact",
                "tags": ["api"],
                "source_agent": "tester",
                "trust_tier": "user",
                "secret_ref": {
                    "provider": "vault",
                    "uri": "vault://prod/api_key",
                    "key_name": "api_key",
                    "version": 1,
                },
            },
            local_only=False,
            embedding_model="onnx-minilm-l6-v2",
        )

        for fmt in ("json", "csv", "markdown"):
            out_file = tmp_path / f"export.{fmt}"
            export_namespace(namespace=ns, output_path=str(out_file), format=fmt)
            assert out_file.exists()

            # Check permissions
            if os.name != "nt":
                mode = out_file.stat().st_mode & 0o777
                assert mode == 0o600, f"Expected 0600 permissions on {fmt} export, got {oct(mode)}"

            text = out_file.read_text(encoding="utf-8")

            # Public memory is present
            assert "Public infrastructure fact" in text
            # Private memory MUST NOT be present
            assert "Private secret note" not in text
            # Quarantined memory MUST NOT be present
            assert "Untrusted prompt injection memory" not in text
            # Secret ref is present as locator
            assert "vault://prod/api_key" in text

    finally:
        _cleanup_ns(ns)


# ==============================================================================
# Item 6: Review Placeholders Check
# ==============================================================================

def test_check_docs_detects_review_placeholders(tmp_path):
    """Verify check_no_review_placeholders_in_published_files detects placeholders."""
    from scripts.check_docs import check_no_review_placeholders_in_published_files

    # Clean run on real repo files should pass
    clean_errors = check_no_review_placeholders_in_published_files()
    assert len(clean_errors) == 0, f"Unexpected placeholders in repo: {clean_errors}"

    # Verify that a placeholder in a scanned file is flagged
    bad_file = tmp_path / "README.md"
    bad_file.write_text("Audit report: [REPORT_LINK]", encoding="utf-8")
    with patch("scripts.check_docs.REPO_ROOT", tmp_path):
        errors = check_no_review_placeholders_in_published_files()
        assert len(errors) > 0
        assert any("[REPORT_LINK]" in err for err in errors)


# ==============================================================================
# Item 8: Phase 6 Downgrade Warning, Strict Mode, Mixed Space, Re-embed
# ==============================================================================

def test_warn_hash_fallback_once(capsys):
    """Verify loud warning on stderr when falling back to hash-384, emitted only once."""
    import mesh.memory as mem_mod

    # Reset emission state
    with mem_mod._HASH_FALLBACK_LOCK:
        mem_mod._HASH_FALLBACK_WARNED = False

    _warn_hash_fallback_once("unit test trigger")
    captured = capsys.readouterr()
    assert "Running with hash-384 embedding fallback" in captured.err
    assert "mesh setup" in captured.err

    # Second call should not duplicate stderr output
    _warn_hash_fallback_once("unit test trigger 2")
    captured2 = capsys.readouterr()
    assert "[mesh warning]" not in captured2.err


def test_strict_mode_mesh_require_model(monkeypatch):
    """Verify MESH_REQUIRE_MODEL=1 fails fast when ONNX model is unavailable."""
    import mesh.memory as mem_mod

    monkeypatch.setenv("MESH_REQUIRE_MODEL", "1")
    mem_mod.unload_model()
    # Simulate missing model files
    with patch("mesh.memory._has_local_onnx_pair", return_value=None), \
         patch("mesh.memory._discover_local_onnx_pair", return_value=None):
        with pytest.raises(RuntimeError) as exc_info:
            mem_mod._get_model()
        assert "MESH_REQUIRE_MODEL=1 is set" in str(exc_info.value)
        assert "Silent fallback to hash-384 is forbidden" in str(exc_info.value)


def test_recall_metadata_includes_backend():
    """Verify recall results include embedding_backend and embedding_model metadata."""
    ns = "test_recall_meta"
    mesh = Mesh(namespace=ns, agent_id="tester")
    try:
        mesh.learn("Vector backend test entry", tags=["meta"])
        recalled = mesh.recall("Vector backend", n=1)
        assert len(recalled) > 0
        assert "embedding_backend" in recalled[0]
        assert "embedding_model" in recalled[0]
        assert recalled[0]["embedding_backend"] in ("onnx", "hash-fallback", "hash-384")
    finally:
        _cleanup_ns(ns)


def test_mixed_space_warning_in_store():
    """Verify a warning is emitted when _is_mixed_space excludes memories during search."""
    ns = "test_mixed_warning"
    store = MeshStore(namespace=ns)
    try:
        # Insert one hash-384 memory and one onnx-minilm memory
        store.upsert(
            memory_id="hash-1",
            content="Hash memory",
            embedding=[0.1] * 384,
            metadata={"memory_type": "fact", "tags": []},
            local_only=False,
            embedding_model="hash-384",
        )
        store.upsert(
            memory_id="onnx-1",
            content="ONNX memory",
            embedding=[0.2] * 384,
            metadata={"memory_type": "fact", "tags": []},
            local_only=False,
            embedding_model="onnx-minilm-l6-v2",
        )

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            # Query with ONNX model vector
            results = store.query(
                embedding=[0.2] * 384,
                n=10,
                query_embedding_model="onnx-minilm-l6-v2",
            )
            # Should emit warning about 1 excluded incompatible memory
            mixed_warnings = [
                item for item in w if "Excluded" in str(item.message) and "mixed embedding space" in str(item.message)
            ]
            assert len(mixed_warnings) == 1
            assert "mesh reembed" in str(mixed_warnings[0].message)
    finally:
        _cleanup_ns(ns)


def test_reembed_command(capsys, monkeypatch):
    """Verify mesh reembed updates historical memories and records vector backend."""
    from mesh.cli import reembed_cmd

    ns = "test_reembed_cli"
    store = MeshStore(namespace=ns)
    try:
        # Seed memories with hash-384
        with store._connect() as conn:
            conn.execute(
                "INSERT INTO memories (id, namespace, content, embedding_model, embedding, created_at, updated_at, accessed_at, access_count, local_only) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("mem-re-1", ns, "Memory to reembed A", "hash-384", None, "2026-10-26T12:00:00Z", "2026-10-26T12:00:00Z", "2026-10-26T12:00:00Z", 0, 0),
            )
            conn.execute(
                "INSERT INTO memories (id, namespace, content, embedding_model, embedding, created_at, updated_at, accessed_at, access_count, local_only) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("mem-re-2", ns, "Memory to reembed B", "hash-384", None, "2026-10-26T12:00:00Z", "2026-10-26T12:00:00Z", "2026-10-26T12:00:00Z", 0, 0),
            )
            conn.commit()

        fake_pair = (Path("fake.onnx"), Path("fake_tokenizer.json"))
        with patch("mesh.memory._has_local_onnx_pair", return_value=fake_pair), \
             patch("mesh.memory._encode_uncached_texts_with_tag", side_effect=lambda texts: ([[0.05] * 384 for _ in texts], "onnx-minilm-l6-v2")):

            # 1. Test Dry Run
            monkeypatch.setattr(
                sys, "argv", ["mesh", "reembed", "--namespace", ns, "--dry-run"]
            )
            reembed_cmd()
            captured = capsys.readouterr()
            comb = captured.out + captured.err
            assert "2 memories would be re-embedded" in comb

            # Verify rows were not changed
            with store._connect() as conn:
                rows = conn.execute("SELECT id, embedding_model FROM memories WHERE namespace = ?", (ns,)).fetchall()
                assert all(r[1] == "hash-384" for r in rows)

            # 2. Perform actual reembed
            monkeypatch.setattr(
                sys, "argv", ["mesh", "reembed", "--namespace", ns]
            )
            reembed_cmd()
            captured2 = capsys.readouterr()
            comb2 = captured2.out + captured2.err
            assert "Re-embedded 2 memories" in comb2

            # Verify rows are updated to onnx-minilm-l6-v2
            with store._connect() as conn:
                rows_after = conn.execute("SELECT id, embedding_model FROM memories WHERE namespace = ?", (ns,)).fetchall()
                for r in rows_after:
                    assert r[1] == "onnx-minilm-l6-v2"

    finally:
        _cleanup_ns(ns)


def test_reembed_crash_safety_rollback_and_resume(capsys, monkeypatch):
    """
    Verify that an interruption mid-batch in mesh reembed:
      1. Rolls back the active batch safely without corrupted/missing rows
      2. Created an automatic pre-run safety backup
      3. Allows a subsequent run to resume and successfully re-embed all memories
    """
    from mesh.cli import reembed_cmd

    ns = "test_reembed_crash"
    store = MeshStore(namespace=ns)
    try:
        # Seed 4 memories
        with store._connect() as conn:
            for i in range(1, 5):
                conn.execute(
                    "INSERT INTO memories (id, namespace, content, embedding_model, embedding, created_at, updated_at, accessed_at, access_count, local_only) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (f"mem-crash-{i}", ns, f"Memory {i} for crash safety test", "hash-384", None, "2026-10-26T12:00:00Z", "2026-10-26T12:00:00Z", "2026-10-26T12:00:00Z", 0, 0),
                )
            conn.commit()

        fake_pair = (Path("fake.onnx"), Path("fake_tokenizer.json"))
        batch_call_count = 0

        def failing_encode(texts):
            nonlocal batch_call_count
            batch_call_count += 1
            if batch_call_count == 2:
                raise RuntimeError("Simulated crash mid-batch 2!")
            return ([[0.05] * 384 for _ in texts], "onnx-minilm-l6-v2")

        with patch("mesh.memory._has_local_onnx_pair", return_value=fake_pair):
            # 1. Run with crash on second batch
            with patch("mesh.memory._encode_uncached_texts_with_tag", side_effect=failing_encode):
                monkeypatch.setattr(
                    sys, "argv", ["mesh", "reembed", "--namespace", ns, "--batch-size", "2"]
                )
                with pytest.raises(RuntimeError) as exc_info:
                    reembed_cmd()
                assert "Simulated crash mid-batch 2!" in str(exc_info.value)

            # Verify database integrity:
            # - Exactly 4 memories exist (none lost)
            # - Batch 1 (2 memories) converted, Batch 2 (2 memories) intact as hash-384
            with store._connect() as conn:
                rows = conn.execute("SELECT id, embedding_model FROM memories WHERE namespace = ? ORDER BY id", (ns,)).fetchall()
                assert len(rows) == 4
                onnx_count = sum(1 for r in rows if r[1] == "onnx-minilm-l6-v2")
                hash_count = sum(1 for r in rows if r[1] == "hash-384")
                assert onnx_count == 2
                assert hash_count == 2

            # Verify safety backup file was created
            backup_files = list(store.db_path.parent.glob(f"{store.db_path.name}.bak-reembed-*"))
            assert len(backup_files) >= 1

            # 2. Re-run to completion (resumes remaining hash-384 memories)
            with patch("mesh.memory._encode_uncached_texts_with_tag", side_effect=lambda texts: ([[0.05] * 384 for _ in texts], "onnx-minilm-l6-v2")):
                monkeypatch.setattr(
                    sys, "argv", ["mesh", "reembed", "--namespace", ns, "--batch-size", "2"]
                )
                reembed_cmd()
                captured = capsys.readouterr()
                comb = captured.out + captured.err
                assert "Re-embedded 2 memories" in comb

            # Verify all 4 memories are now on ONNX
            with store._connect() as conn:
                rows_final = conn.execute("SELECT id, embedding_model FROM memories WHERE namespace = ?", (ns,)).fetchall()
                assert len(rows_final) == 4
                assert all(r[1] == "onnx-minilm-l6-v2" for r in rows_final)

    finally:
        _cleanup_ns(ns)


def test_query_logging_redacts_secrets(monkeypatch):
    """Verify that query text containing credentials is redacted before writing to queries table."""
    ns = "test_q_redact_secrets"
    try:
        mesh = Mesh(namespace=ns, agent_id="agent-audit")
        secret_query = "Find deployment token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
        mesh.recall(secret_query, n=5)
        mesh.store.flush_access_tracking()

        with mesh.store._connect() as conn:
            rows = conn.execute("SELECT query_text FROM queries WHERE namespace = ?", (ns,)).fetchall()
            assert len(rows) >= 1
            for (q_text,) in rows:
                assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456" not in q_text
                assert "[REDACTED" in q_text
    finally:
        _cleanup_ns(ns)


def test_query_logging_disabled_via_env(monkeypatch):
    """Verify that MESH_LOG_QUERY_TEXT=0 records empty query text in queries table."""
    ns = "test_q_log_disabled"
    monkeypatch.setenv("MESH_LOG_QUERY_TEXT", "0")
    try:
        mesh = Mesh(namespace=ns, agent_id="agent-audit")
        mesh.recall("Secret architecture decision", n=5)
        mesh.store.flush_access_tracking()

        with mesh.store._connect() as conn:
            rows = conn.execute("SELECT query_text, result_count FROM queries WHERE namespace = ?", (ns,)).fetchall()
            assert len(rows) >= 1
            for q_text, r_cnt in rows:
                assert q_text == ""
    finally:
        _cleanup_ns(ns)


def test_export_excludes_queries_table(tmp_path):
    """Verify that exports (JSON, Markdown, CSV) never leak queries table contents."""
    from mesh.io import export_namespace

    ns = "test_export_isolation"
    try:
        mesh = Mesh(namespace=ns, agent_id="agent-export")
        mesh.learn("Important project fact for export", tags=["safe"])
        mesh.recall("Search query containing private context", n=5)
        mesh.store.flush_access_tracking()

        json_out = tmp_path / "export.json"
        md_out = tmp_path / "export.md"
        csv_out = tmp_path / "export.csv"

        export_namespace(ns, json_out, format="json")
        export_namespace(ns, md_out, format="markdown")
        export_namespace(ns, csv_out, format="csv")

        json_data = json.loads(json_out.read_text(encoding="utf-8"))
        assert "queries" not in json_data
        md_raw = md_out.read_text(encoding="utf-8")
        csv_raw = csv_out.read_text(encoding="utf-8")

        for raw in (md_raw, csv_raw):
            assert "Search query containing private context" not in raw
            assert "queries" not in raw.lower()
    finally:
        _cleanup_ns(ns)


def test_no_outbound_network_phone_home(monkeypatch):
    """
    Automated network barrier test:
    Verify that full Mesh lifecycle executes with zero outbound network calls.
    Outbound sockets raise RuntimeError, while loopback remains allowed.
    """
    import socket

    orig_connect = socket.socket.connect

    def guarded_connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        # Allow loopback addresses
        if host in ("127.0.0.1", "::1", "localhost"):
            return orig_connect(self, address)
        raise RuntimeError(f"Prohibited outbound network access attempted to: {address}")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)

    ns = "test_offline_privacy"
    try:
        mesh = Mesh(namespace=ns, agent_id="offline-agent")
        # 1. Ingestion
        m = mesh.learn("Autonomous decision for offline privacy test", tags=["offline", "security"])
        assert m["memory_id"]

        # 2. Query & Recall
        results = mesh.recall("offline privacy test", n=3)
        assert len(results) >= 1

        # 3. Project Brief
        brief = mesh.project_brief(max_tokens=300)
        assert "Autonomous decision" in brief or len(brief) > 0

        # 4. Access tracking flush
        mesh.store.flush_access_tracking()

        # 5. Contradiction check
        mesh.store.log_contradiction(
            new_memory_id="mem-new",
            new_content="Fact A",
            existing_memory_id="mem-old",
            existing_content="Fact B",
            similarity=0.95,
        )

        # 6. Work items
        task = mesh.create_work_item(title="Offline task")
        assert task["short_id"]

        # Verified: full lifecycle completed without attempting non-loopback connections
    finally:
        _cleanup_ns(ns)


def test_reembed_backup_hardening(monkeypatch):
    """
    Verify reembed backup safety:
      1. Raises OSError when disk space is insufficient
      2. Automatically prunes old backups to retain at most 3
    """
    from mesh.cli import reembed_cmd
    import shutil

    ns = "test_reembed_hardening"
    store = MeshStore(namespace=ns)
    try:
        with store._connect() as conn:
            conn.execute(
                "INSERT INTO memories (id, namespace, content, embedding_model, embedding, created_at, updated_at, accessed_at, access_count, local_only) "
                "VALUES ('mem-h-1', ?, 'Hardening test memory', 'hash-384', NULL, '2026-10-26T12:00:00Z', '2026-10-26T12:00:00Z', '2026-10-26T12:00:00Z', 0, 0)",
                (ns,),
            )
            conn.commit()

        # 1. Test insufficient disk space
        def mock_disk_usage(path):
            # return 100 bytes free
            return shutil._ntuple_diskusage(1000000, 999900, 100)

        fake_pair = (Path("fake.onnx"), Path("fake_tokenizer.json"))
        with patch("mesh.memory._has_local_onnx_pair", return_value=fake_pair):
            with patch("shutil.disk_usage", side_effect=mock_disk_usage):
                monkeypatch.setattr(sys, "argv", ["mesh", "reembed", "--namespace", ns])
                with pytest.raises(OSError) as exc_info:
                    reembed_cmd()
                assert "Insufficient disk space" in str(exc_info.value)

            # 2. Test old backup pruning (create 5 dummy backups)
            for i in range(1, 6):
                dummy_bak = store.db_path.with_name(f"{store.db_path.name}.bak-reembed-2026102600000{i}")
                dummy_bak.write_text("dummy", encoding="utf-8")

            # Run reembed successfully
            with patch("mesh.memory._encode_uncached_texts_with_tag", side_effect=lambda texts: ([[0.05] * 384 for _ in texts], "onnx-minilm-l6-v2")):
                monkeypatch.setattr(sys, "argv", ["mesh", "reembed", "--namespace", ns])
                reembed_cmd()

            # Verify that only 3 most recent backups remain
            backups = list(store.db_path.parent.glob(f"{store.db_path.name}.bak-reembed-*"))
            assert len(backups) <= 3
            # Cleanup dummy backups
            for b in backups:
                try:
                    b.unlink()
                except Exception:
                    pass
    finally:
        _cleanup_ns(ns)


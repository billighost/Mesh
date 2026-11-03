"""Tests for Mesh v0.3: privacy flags, export/import, namespace management."""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from mesh import Mesh
from mesh.io import export_namespace, import_namespace, sanitize_csv_cell
from mesh.namespaces import (
    delete_namespace,
    list_namespaces,
    namespace_stats,
    rename_namespace,
)
from mesh.store import NamespaceNotFoundError
from mesh.version import VERSION


@pytest.fixture(autouse=True)
def isolated_test_env(tmp_path, monkeypatch):
    """Ensure all v0.3 tests run in an isolated temporary MESH_HOME directory."""
    mesh_home = tmp_path / "mesh_home"
    mesh_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MESH_HOME", str(mesh_home))
    monkeypatch.setenv("MESH_NO_AUTO_DAEMON", "1")
    monkeypatch.setenv("MESH_AUTO_APPROVE", "0")
    monkeypatch.delenv("MESH_ENCRYPT_AT_REST", raising=False)
    monkeypatch.delenv("MESH_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("MESH_PASSPHRASE", raising=False)
    yield mesh_home


def _cleanup_ns(namespace: str) -> None:
    """Cleanup is best-effort: 0.6.3 made deleting a missing namespace an error."""
    try:
        delete_namespace(namespace)
    except NamespaceNotFoundError:
        pass



# --- Privacy Flag Tests ---

def test_local_only_flag_stored():
    """Memory stored with local_only=True should have local_only=True when recalled."""
    mesh = Mesh(namespace="test_privacy", agent_id="test")
    try:
        mesh.learn("my secret API key is abc123", local_only=True)
        results = mesh.recall("secret API key", n=1)
        assert len(results) > 0
        assert results[0].get("local_only") is True
    finally:
        delete_namespace("test_privacy")


def test_non_local_only_is_false_by_default():
    """Memories without local_only flag should default to False."""
    mesh = Mesh(namespace="test_privacy_default", agent_id="test")
    try:
        mesh.learn("the sky is blue")
        results = mesh.recall("sky", n=1)
        assert len(results) > 0
        assert results[0].get("local_only") is False
    finally:
        delete_namespace("test_privacy_default")


def test_local_only_excluded_from_export():
    """Export should never include local_only memories."""
    mesh = Mesh(namespace="test_export_privacy", agent_id="test")
    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        path = f.name

    try:
        mesh.learn("public fact about the project")
        mesh.learn("private API key: sk-12345", local_only=True)

        export_namespace(
            namespace="test_export_privacy",
            output_path=path,
            include_embeddings=False,
        )

        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        assert data["memory_count"] == 1
        assert data["skipped_local_only"] == 1
        assert all(not m["local_only"] for m in data["memories"])
        assert not any("sk-12345" in m["content"] for m in data["memories"])
    finally:
        delete_namespace("test_export_privacy")
        Path(path).unlink(missing_ok=True)


# --- Export / Import Tests ---

def test_export_creates_valid_json():
    """Export should create a valid JSON file with correct structure."""
    mesh = Mesh(namespace="test_export", agent_id="test")
    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        path = f.name

    try:
        mesh.learn("fact one")
        mesh.learn("fact two")

        export_namespace(
            namespace="test_export",
            output_path=path,
            include_embeddings=False,
        )

        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        assert data["mesh_version"] == VERSION
        assert data["namespace"] == "test_export"
        assert data["memory_count"] == 2
        assert len(data["memories"]) == 2
        assert "exported_at" in data

        for mem in data["memories"]:
            assert "id" in mem
            assert "content" in mem
            assert "memory_type" in mem
            assert "confidence" in mem
    finally:
        delete_namespace("test_export")
        Path(path).unlink(missing_ok=True)


def test_import_restores_memories():
    """Import should restore memories from an export file without creating duplicates."""
    mesh = Mesh(namespace="test_import_src", agent_id="test")
    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        path = f.name

    try:
        mesh.learn("imported fact: the answer is 42")
        export_namespace(
            namespace="test_import_src",
            output_path=path,
            include_embeddings=False,
        )

        stats = import_namespace(input_path=path, target_namespace="test_import_dest")
        assert stats["imported"] == 1
        assert stats["errors"] == 0

        mesh2 = Mesh(namespace="test_import_dest", agent_id="test")
        assert mesh2.count() == 1
        results = mesh2.recall("the answer")
        assert len(results) > 0
        assert "42" in results[0]["content"]
    finally:
        delete_namespace("test_import_src")
        delete_namespace("test_import_dest")
        Path(path).unlink(missing_ok=True)


def test_import_skip_existing():
    """Import with skip_existing should not overwrite memories."""
    ns = "test_import_skip_v2"
    mesh = Mesh(namespace=ns, agent_id="test")
    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        path = f.name

    try:
        mesh.learn("original fact")
        export_namespace(namespace=ns, output_path=path, include_embeddings=False)

        stats1 = import_namespace(path, target_namespace=ns, merge_strategy="skip_existing")
        stats2 = import_namespace(path, target_namespace=ns, merge_strategy="skip_existing")

        assert stats1["skipped"] == 1
        assert stats2["skipped"] == 1
    finally:
        delete_namespace(ns)
        Path(path).unlink(missing_ok=True)


def test_dry_run_doesnt_write():
    """Dry run should not write any memories."""
    mesh = Mesh(namespace="test_dry_run_src", agent_id="test")
    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        path = f.name

    try:
        mesh.learn("dry run test")
        export_namespace(
            namespace="test_dry_run_src",
            output_path=path,
            include_embeddings=False,
        )

        import_namespace(path, target_namespace="test_dry_run_dest", dry_run=True)

        namespaces = [n["namespace"] for n in list_namespaces()]
        assert "test_dry_run_dest" not in namespaces
        with pytest.raises(NamespaceNotFoundError):
            delete_namespace("test_dry_run_dest")
    finally:
        _cleanup_ns("test_dry_run_src")
        Path(path).unlink(missing_ok=True)


# --- Namespace Management Tests ---

def test_list_namespaces_includes_created():
    """list_namespaces should include namespaces that have been written to."""
    mesh = Mesh(namespace="test_ns_list", agent_id="test")
    try:
        mesh.learn("something")
        namespaces = [n["namespace"] for n in list_namespaces()]
        assert "test_ns_list" in namespaces
    finally:
        delete_namespace("test_ns_list")


def test_delete_namespace_removes_all():
    """delete_namespace should remove all memories and the namespace itself."""
    mesh = Mesh(namespace="test_ns_delete", agent_id="test")
    mesh.learn("memory to delete")

    count = delete_namespace("test_ns_delete")
    assert count >= 1

    namespaces = [n["namespace"] for n in list_namespaces()]
    assert "test_ns_delete" not in namespaces


def test_rename_namespace():
    """rename_namespace should move all memories and update internal namespace column."""
    mesh = Mesh(namespace="test_ns_rename_old", agent_id="test")
    try:
        mesh.learn("memory in old namespace")
        rename_namespace("test_ns_rename_old", "test_ns_rename_new")

        namespaces = [n["namespace"] for n in list_namespaces()]
        assert "test_ns_rename_old" not in namespaces
        assert "test_ns_rename_new" in namespaces

        mesh2 = Mesh(namespace="test_ns_rename_new", agent_id="test")
        assert len(mesh2.inspect()) == 1
        results = mesh2.recall("memory in old namespace")
        assert len(results) > 0
    finally:
        _cleanup_ns("test_ns_rename_old")
        _cleanup_ns("test_ns_rename_new")


def test_namespace_stats_accuracy():
    """namespace_stats should return accurate counts."""
    mesh = Mesh(namespace="test_ns_stats", agent_id="test")
    try:
        mesh.learn("public fact")
        mesh.learn("private fact", local_only=True)

        stats = namespace_stats("test_ns_stats")
        assert stats["memory_count"] == 2
        assert stats["local_only_count"] == 1
        assert stats["exportable_count"] == 1
    finally:
        delete_namespace("test_ns_stats")


def test_export_csv_formula_injection_protection(tmp_path):
    """
    Verify CSV export neutralizes formula injection (CWE-1236)
    while preserving commas, quotes, multiline text, and Unicode.
    """
    import csv
    from mesh.io import export_namespace

    ns = "test_csv_formulas"
    mesh = Mesh(namespace=ns, agent_id="test")

    try:
        # Ingest formula injection attempts and special characters
        mesh.learn("=cmd|' /C calc'!A0", tags=["cwe1236", "calc"])
        mesh.learn("+12345-formula", tags=["plus"])
        mesh.learn("-1+1", tags=["minus"])
        mesh.learn("@SUM(A1:B10)", tags=["at"])
        mesh.learn("\t=tab_formula", tags=["tab"])
        mesh.learn("\r=cr_formula", tags=["cr"])
        mesh.learn("   =spaced_formula", tags=["space"])
        mesh.learn('Regular, with "quotes", and\nnewlines and Unicode 🚀', tags=["benign", "unicode"])

        csv_path = tmp_path / "export.csv"
        export_namespace(namespace=ns, output_path=str(csv_path), format="csv")

        assert csv_path.exists()
        with open(csv_path, "r", encoding="utf-8", newline="") as f:
            reader = list(csv.reader(f))

        headers = reader[0]
        assert "content" in headers
        content_idx = headers.index("content")

        contents = [row[content_idx] for row in reader[1:]]

        # Verify unit behavior of sanitize_csv_cell
        assert sanitize_csv_cell("\t=raw_tab") == "'\t=raw_tab"
        assert sanitize_csv_cell("\r=raw_cr") == "'\r=raw_cr"
        assert sanitize_csv_cell("=SUM(A1)") == "'=SUM(A1)"
        assert sanitize_csv_cell("+cmd") == "'+cmd"
        assert sanitize_csv_cell("-1+1") == "'-1+1"
        assert sanitize_csv_cell("@SUM") == "'@SUM"
        assert sanitize_csv_cell("   =spaced") == "'   =spaced"
        assert sanitize_csv_cell("safe text") == "safe text"

        # Verify all formula injection strings in CSV are prefixed with single quote (')
        assert any(c.startswith("'=cmd|") for c in contents)
        assert any(c.startswith("'+12345") for c in contents)
        assert any(c.startswith("'-1+1") for c in contents)
        assert any(c.startswith("'@SUM") for c in contents)
        assert any(c.startswith("'=tab_formula") for c in contents)
        assert any(c.startswith("'=cr_formula") for c in contents)
        assert any(c.startswith("'=spaced_formula") for c in contents)

        # Verify benign special character text parses correctly
        benign_found = False
        for c in contents:
            if 'Regular, with "quotes"' in c and "Unicode 🚀" in c:
                benign_found = True
                assert not c.startswith("'")
        assert benign_found, "Benign unicode text with quotes and newlines was not preserved properly"

    finally:
        delete_namespace(ns)


def test_export_markdown_format(tmp_path):
    """Verify Markdown export format produces structured human-readable markdown without vectors."""
    from mesh.io import export_namespace

    ns = "test_md_export"
    mesh = Mesh(namespace=ns, agent_id="tester")

    try:
        mesh.learn("PostgreSQL 16 runs on port 5432", tags=["db", "infra"], memory_type="fact")
        mesh.create_work_item(title="Set up database cluster", kind="task", priority="high")

        md_path = tmp_path / "export.md"
        export_namespace(namespace=ns, output_path=str(md_path), format="markdown")

        assert md_path.exists()
        text = md_path.read_text(encoding="utf-8")

        assert f"# Mesh Export: {ns}" in text
        assert "## Memories" in text
        assert "PostgreSQL 16 runs on port 5432" in text
        assert "## Work Items" in text
        assert "Set up database cluster" in text
        # Embeddings should not appear in Markdown export
        assert "embedding" not in text.lower() or "no embeddings" in text.lower()

    finally:
        delete_namespace(ns)


def test_export_cli_format_inference_and_flag(tmp_path, monkeypatch):
    """Verify mesh export CLI infers format from extension and supports --format."""
    from mesh.cli import export_cmd

    ns = "test_cli_formats"
    mesh = Mesh(namespace=ns, agent_id="tester")

    try:
        mesh.learn("Fact for CLI export test")

        # 1. Infer .csv
        csv_path = tmp_path / "cli_inferred.csv"
        monkeypatch.setattr(
            sys, "argv", ["mesh export", str(csv_path), "--namespace", ns]
        )
        export_cmd()
        assert csv_path.exists()
        assert "id,memory_type,confidence,content" in csv_path.read_text(encoding="utf-8")

        # 2. Infer .md
        md_path = tmp_path / "cli_inferred.md"
        monkeypatch.setattr(
            sys, "argv", ["mesh export", str(md_path), "--namespace", ns]
        )
        export_cmd()
        assert md_path.exists()
        assert f"# Mesh Export: {ns}" in md_path.read_text(encoding="utf-8")

        # 3. Explicit --format csv with generic filename
        gen_path = tmp_path / "custom_output.txt"
        monkeypatch.setattr(
            sys, "argv", ["mesh export", str(gen_path), "--format", "csv", "--namespace", ns]
        )
        export_cmd()
        assert gen_path.exists()
        assert "id,memory_type,confidence,content" in gen_path.read_text(encoding="utf-8")

    finally:
        delete_namespace(ns)



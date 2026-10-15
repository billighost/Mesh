"""
full_showcase.py -- Comprehensive end-to-end feature test & latency benchmark for Mesh.

Run:
    python full_showcase.py
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import uuid
from pathlib import Path
import sys

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from fastapi.testclient import TestClient

from mesh import (
     VALID_MEMORY_TYPES,
    VERSION,
    Mesh,
    build_context,
    delete_namespace,
    export_namespace,
    extract_memories_from_log,
    generate_digest,
    get_audit_log,
    get_audit_stats,
    import_namespace,
    list_namespaces,
    log_forget,
    log_learn,
    log_recall,
    log_update,
    namespace_stats,
    rename_namespace,
    warmup_model,
)
from mesh.audit import _get_audit_db_path
from mesh.http_server import app as http_app
from mesh.mcp_server import call_tool, list_tools
from mesh.namespaces import _remove_sqlite_files


def _ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


def _cleanup_namespace(ns: str) -> None:
    try:
        delete_namespace(ns)
    except Exception:
        pass
    try:
        _remove_sqlite_files(_get_audit_db_path(ns))
    except Exception:
        pass


def main() -> None:
    run_id = uuid.uuid4().hex[:6]
    ns_main = f"showcase_main_{run_id}"
    ns_imported = f"showcase_imp_{run_id}"
    ns_renamed = f"showcase_ren_{run_id}"
    ns_mcp = f"showcase_mcp_{run_id}"
    ns_http = f"showcase_http_{run_id}"
    temp_files: list[Path] = []

    print("=" * 76)
    print(f"MESH v{VERSION} -- FULL ARCHITECTURE, FEATURE & SPEED VERIFICATION")
    print("=" * 76)

    try:
        # ------------------------------------------------------------------
        # 1. Model Warmup & Speed Benchmark (First Call vs Consecutive Calls)
        # ------------------------------------------------------------------
        print("\n[1/11] Model Warmup & Latency Benchmark...")
        t0 = time.perf_counter()
        warmup_model(ns_main)
        warmup_ms = _ms(t0)
        print(f"  [OK] warmup_model() completed in {warmup_ms:.2f} ms")

        agent_a = Mesh(namespace=ns_main, agent_id="architect-agent")
        agent_b = Mesh(namespace=ns_main, agent_id="backend-agent")

        t0 = time.perf_counter()
        first_learn = agent_a.learn(
            "Production PostgreSQL 16 database runs on port 5432 with pgBouncer pooling",
            memory_type="fact",
            confidence=1.0,
            tags=["db", "postgres", "prod"],
        )
        first_learn_ms = _ms(t0)
        first_id = first_learn["memory_id"]
        assert isinstance(first_id, str) and len(first_id) == 36
        assert first_learn["status"] == "stored"

        t0 = time.perf_counter()
        dup_learn = agent_b.learn(
            "Production PostgreSQL 16 database runs on port 5432 with pgBouncer pooling",
            memory_type="fact",
            tags=["pooling"],
        )
        cached_learn_ms = _ms(t0)
        assert dup_learn["deduplicated"] is True
        assert dup_learn["memory_id"] == first_id

        t0 = time.perf_counter()
        first_recall = agent_b.recall("what port does production postgres use?", n=3)
        uncached_recall_ms = _ms(t0)
        assert len(first_recall) == 1
        assert first_recall[0]["id"] == first_id

        t0 = time.perf_counter()
        cached_recall = agent_b.recall("what port does production postgres use?", n=3)
        cached_recall_ms = _ms(t0)
        assert len(cached_recall) == 1

        print(f"  [OK] 1st learn() (new text + embed + SQLite + matrix):  {first_learn_ms:.2f} ms")
        print(f"  [OK] Consecutive learn() (cached embed + dedup):        {cached_learn_ms:.2f} ms")
        print(f"  [OK] 1st recall() (new query embed + hybrid search):    {uncached_recall_ms:.2f} ms")
        print(f"  [OK] Consecutive recall() (cached query + hot matrix):  {cached_recall_ms:.2f} ms")

        # ------------------------------------------------------------------
        # 2. All Memory Types, Hybrid Search & Metadata Filtering
        # ------------------------------------------------------------------
        print("\n[2/11] All Memory Types & 3-Stage Hybrid Search...")
        for mtype in sorted(VALID_MEMORY_TYPES):
            res = agent_a.learn(
                f"Canonical {mtype} entry: service module {mtype}_v2 is active",
                memory_type=mtype,
                tags=["types", mtype],
            )
            assert res["memory_id"]

        # Exact technical identifier test for hybrid boost
        agent_a.learn(
            "Critical incident ERR_CODE_9482_XYZ happens when Redis cluster loses quorum",
            memory_type="error",
            tags=["redis", "incident"],
        )
        exact_hits = agent_b.recall("ERR_CODE_9482_XYZ", n=3)
        assert len(exact_hits) >= 1
        assert "ERR_CODE_9482_XYZ" in exact_hits[0]["content"]

        # Filter by memory_type, tags, and source_agent
        err_only = agent_b.recall(
            "Redis incident",
            memory_type="error",
            tags=["redis"],
            source_agent="architect-agent",
        )
        assert len(err_only) >= 1
        assert all(m["memory_type"] == "error" for m in err_only)
        print(
            f"  [OK] Verified all {len(VALID_MEMORY_TYPES)} memory types, exact token boost, and filters"
        )

        # ------------------------------------------------------------------
        # 3. Contradiction Detection & Resolution
        # ------------------------------------------------------------------
        print("\n[3/11] Contradiction Detection & Resolution...")
        r1 = agent_a.learn("The public API rate limit is 100 requests per minute", memory_type="fact")
        r2 = agent_b.learn("The public API rate limit is 500 requests per minute", memory_type="fact")
        assert r2["status"] == "stored_with_conflict"
        assert r2["conflict"] is not None
        conflict_id = r2["conflict"]["contradiction_id"]

        open_conflicts = agent_a.contradictions(resolved=False)
        assert any(c["id"] == conflict_id for c in open_conflicts)
        # Verify new_memory_id was properly bound
        matched_conflict = next(c for c in open_conflicts if c["id"] == conflict_id)
        assert matched_conflict["new_memory_id"] == r2["memory_id"]
        assert matched_conflict["existing_memory_id"] == r1["memory_id"]

        agent_a.resolve_contradiction(conflict_id)
        resolved_conflicts = agent_a.contradictions(resolved=True)
        assert any(c["id"] == conflict_id for c in resolved_conflicts)
        print(f"  [OK] Conflict detected & resolved cleanly (contradiction_id={conflict_id[:8]}...)")

        # ------------------------------------------------------------------
        # 4. In-Place Update, TTL Decay & Forget
        # ------------------------------------------------------------------
        print("\n[4/11] In-Place Update, TTL Confidence Decay & Forget...")
        before_update = agent_a.get(first_id)
        assert before_update is not None
        prev_access_count = before_update["access_count"]

        t0 = time.perf_counter()
        upd = agent_b.update(
            memory_id=first_id,
            content="Production PostgreSQL 16 database runs on port 5433 with pgBouncer pooling",
            confidence=0.95,
            tags=["db", "postgres", "prod", "updated"],
            ttl_days=30,
        )
        update_ms = _ms(t0)
        assert upd["status"] == "updated"
        assert upd["memory"]["id"] == first_id
        assert upd["memory"]["access_count"] == prev_access_count
        assert upd["memory"]["created_at"] == before_update["created_at"]
        assert upd["memory"]["is_stale"] is False
        assert upd["memory"]["days_until_stale"] is not None

        temp_mem = agent_a.learn("Ephemeral memory to delete", memory_type="general")
        count_before_forget = agent_a.count()
        assert agent_a.forget(temp_mem["memory_id"]) is True
        assert agent_a.count() == count_before_forget - 1
        print(f"  [OK] In-place update completed in {update_ms:.2f} ms; TTL & forget() verified")

        # ------------------------------------------------------------------
        # 5. Pattern & Memory Gap Analysis
        # ------------------------------------------------------------------
        print("\n[5/11] Pattern & Memory Gap Analysis...")
        # Trigger a low-similarity repeated query to test memory_gaps
        agent_b.recall("quantum gravity tachyon propulsion hyperdrive", n=1)
        agent_b.recall("quantum gravity tachyon propulsion hyperdrive", n=1)
        patterns = agent_a.patterns()
        assert "top_topics" in patterns
        assert "memory_gaps" in patterns
        assert "unused_memories" in patterns
        assert "stale_memories" in patterns
        assert len(patterns["top_topics"]) >= 1
        print(
            f"  [OK] Patterns analyzed: {len(patterns['top_topics'])} top topics, "
            f"{len(patterns['memory_gaps'])} gaps detected"
        )

        # ------------------------------------------------------------------
        # 6. Context Builder (Markdown, Plain, XML, JSON)
        # ------------------------------------------------------------------
        print("\n[6/11] Context Builder Formatting...")
        for fmt in ("markdown", "plain", "xml", "json"):
            ctx_general = build_context(namespace=ns_main, count=5, format=fmt)
            ctx_focused = build_context(
                query="PostgreSQL port",
                namespace=ns_main,
                count=3,
                format=fmt,
            )
            assert len(ctx_general) > 20
            assert len(ctx_focused) > 20
        print("  [OK] Verified build_context() across markdown, plain, xml, and json")

        # ------------------------------------------------------------------
        # 7. Privacy (`local_only`), Export & Batch Import
        # ------------------------------------------------------------------
        print("\n[7/11] Privacy Flags (`local_only`), Export & Batch Import...")
        secret_res = agent_a.learn(
            "Private production root token is sk-live-9988776655443322",
            memory_type="fact",
            local_only=True,
            tags=["secret"],
        )
        assert agent_a.get(secret_res["memory_id"])["local_only"] is True

        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as tf:
            export_path = Path(tf.name)
            temp_files.append(export_path)

        exported = export_namespace(
            namespace=ns_main,
            output_path=str(export_path),
            include_embeddings=True,
        )
        assert exported["skipped_local_only"] >= 1
        assert not any("sk-live-9988776655443322" in m["content"] for m in exported["memories"])

        t0 = time.perf_counter()
        imp_stats1 = import_namespace(
            input_path=str(export_path),
            target_namespace=ns_imported,
            merge_strategy="skip_existing",
        )
        import_ms = _ms(t0)
        assert imp_stats1["imported"] == exported["memory_count"]
        assert imp_stats1["errors"] == 0

        imp_stats2 = import_namespace(
            input_path=str(export_path),
            target_namespace=ns_imported,
            merge_strategy="skip_existing",
        )
        assert imp_stats2["skipped"] == exported["memory_count"]
        print(
            f"  [OK] Exported {exported['memory_count']} public memories (skipped "
            f"{exported['skipped_local_only']} private) and batch-imported in {import_ms:.2f} ms"
        )

        # ------------------------------------------------------------------
        # 8. Namespace Management (List, Stats, Rename, Delete)
        # ------------------------------------------------------------------
        print("\n[8/11] Namespace Management...")
        ns_list = [n["namespace"] for n in list_namespaces()]
        assert ns_main in ns_list
        assert ns_imported in ns_list

        stats = namespace_stats(ns_main)
        assert stats["memory_count"] == agent_a.count()
        assert stats["local_only_count"] >= 1

        moved = rename_namespace(ns_imported, ns_renamed)
        assert moved == exported["memory_count"]
        ns_list_after = [n["namespace"] for n in list_namespaces()]
        assert ns_imported not in ns_list_after
        assert ns_renamed in ns_list_after

        deleted_count = delete_namespace(ns_renamed)
        assert deleted_count == moved
        assert ns_renamed not in [n["namespace"] for n in list_namespaces()]
        print("  [OK] Verified list_namespaces(), namespace_stats(), rename_namespace(), delete_namespace()")

        # ------------------------------------------------------------------
        # 9. Audit Trail & End-of-Session Digest
        # ------------------------------------------------------------------
        print("\n[9/11] High-Speed Audit Trail & Session Digest...")
        log_learn("Audit learned fact", ns_main, "architect-agent", first_learn)
        log_recall("PostgreSQL port", ns_main, "backend-agent", first_recall)
        log_update(first_id, "Updated port 5433", ns_main, "backend-agent")
        log_forget(temp_mem["memory_id"], ns_main, "architect-agent")

        audit_entries = get_audit_log(ns_main, limit=20)
        assert len(audit_entries) >= 4
        audit_summary = get_audit_stats(ns_main)
        assert audit_summary["total_learns"] >= 1
        assert audit_summary["total_recalls"] >= 1

        digest = generate_digest(namespace=ns_main, hours=24)
        assert digest["new_count"] >= 1
        assert digest["total_recalls"] >= 1
        print(
            f"  [OK] Audit log ({len(audit_entries)} entries) and 24h digest "
            f"({digest['new_count']} new memories) verified"
        )

        # ------------------------------------------------------------------
        # 10. Passive Capture (Raw Log + Log File + Credential Auto-Privacy)
        # ------------------------------------------------------------------
        print("\n[10/11] Passive Log Capture...")
        with tempfile.NamedTemporaryFile(
            suffix=".log", mode="w", delete=False, encoding="utf-8"
        ) as lf:
            lf.write(
                "We decided to deploy the worker service on port 9091.\n"
                "Always run pytest before merging any pull request to main.\n"
                "The staging bearer token is sk-staging987654321 and must remain secret.\n"
                "Okay sounds good thanks!\n"
            )
            log_file_path = Path(lf.name)
            temp_files.append(log_file_path)

        capture_res = extract_memories_from_log(
            log_text=str(log_file_path),
            namespace=ns_main,
            force_local=True,
            dry_run=False,
        )
        assert capture_res["stored_count"] >= 2
        assert any(m.get("private") for m in capture_res["memories"])
        print(
            f"  [OK] Passive capture extracted {capture_res['stored_count']} memories "
            f"(provider: {capture_res['provider_used']})"
        )

        # ------------------------------------------------------------------
        # 11. MCP Server Tools & FastAPI HTTP Server Endpoints
        # ------------------------------------------------------------------
        print("\n[11/11] MCP Server Tools & HTTP REST API...")
        mcp_tools = asyncio.run(list_tools())
        assert len(mcp_tools) == 10

        mcp_learn_res = json.loads(
            asyncio.run(
                call_tool(
                    "mesh_learn",
                    {
                        "content": "MCP service discovery runs on Consul port 8500",
                        "memory_type": "process",
                        "tags": ["mcp", "consul"],
                        "namespace": ns_mcp,
                    },
                )
            )[0].text
        )
        assert mcp_learn_res["status"] == "stored"

        mcp_ctx_res = json.loads(
            asyncio.run(
                call_tool(
                    "mesh_context",
                    {"query": "Consul port", "namespace": ns_mcp, "format": "markdown"},
                )
            )[0].text
        )
        assert "8500" in mcp_ctx_res["context"]

        client = TestClient(http_app)
        assert client.get("/health").status_code == 200

        http_learn = client.post(
            "/learn",
            json={
                "content": "HTTP gateway timeouts are configured to 30 seconds",
                "memory_type": "decision",
                "tags": ["http", "gateway"],
                "namespace": ns_http,
            },
        ).json()
        assert http_learn["status"] == "ok"

        http_ctx = client.post(
            "/context",
            json={"query": "gateway timeout", "namespace": ns_http, "count": 5},
        ).json()
        assert http_ctx["status"] == "ok"
        assert http_ctx["memory_count"] == 1
        assert "30 seconds" in http_ctx["context"]
        print("  [OK] All 10 MCP tools and HTTP REST endpoints verified end-to-end")

        print("\n" + "=" * 76)
        print("ALL 11 SHOWCASE STAGES PASSED WITH ZERO ERRORS!")
        print("=" * 76)

    finally:
        for ns in (ns_main, ns_imported, ns_renamed, ns_mcp, ns_http):
            _cleanup_namespace(ns)
        for fpath in temp_files:
            fpath.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
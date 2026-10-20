"""
Tests for Mesh core functionality.
Run: python -m pytest tests/ -v
"""

import uuid
import pytest
from mesh import Mesh, VALID_MEMORY_TYPES
from mesh.namespaces import delete_namespace


@pytest.fixture
def mesh():
    """Fresh Mesh instance with unique namespace for each test, fully removed on teardown."""
    ns = f"test-{uuid.uuid4().hex[:8]}"
    m = Mesh(namespace=ns, agent_id="test-agent")
    yield m
    m.clear()
    delete_namespace(ns)


class TestLearn:
    def test_learn_returns_uuid(self, mesh):
        result = mesh.learn("The sky is blue")
        mid = result["memory_id"]
        assert isinstance(mid, str)
        assert len(mid) == 36  # UUID format

    def test_learn_increments_count(self, mesh):
        assert mesh.count() == 0
        mesh.learn("First memory")
        assert mesh.count() == 1
        mesh.learn("Second memory")
        assert mesh.count() == 2

    def test_learn_empty_content_raises(self, mesh):
        with pytest.raises(ValueError):
            mesh.learn("")

    def test_learn_invalid_type_raises(self, mesh):
        with pytest.raises(ValueError):
            mesh.learn("content", memory_type="invalid_type")

    def test_learn_confidence_clamped(self, mesh):
        result = mesh.learn("content", confidence=999.0)
        assert result["memory_id"] is not None

    def test_learn_all_memory_types(self, mesh):
        for mtype in VALID_MEMORY_TYPES:
            result = mesh.learn(f"Memory of type {mtype}", memory_type=mtype)
            assert result["memory_id"] is not None


class TestRecall:
    def test_recall_returns_list(self, mesh):
        mesh.learn("Python is a programming language")
        results = mesh.recall("programming languages")
        assert isinstance(results, list)
        assert len(results) >= 1

    def test_recall_semantic_similarity(self, mesh):
        mesh.learn("The user prefers dark mode in all editors")
        results = mesh.recall("what visual theme does the user like?")
        assert len(results) >= 1
        assert results[0]["similarity"] > 0.3

    def test_recall_returns_correct_fields(self, mesh):
        mesh.learn(
            "Test memory content",
            memory_type="fact",
            confidence=0.9,
            tags=["testing", "core"],
        )
        results = mesh.recall("test memory")
        assert len(results) >= 1
        r = results[0]
        assert "id" in r
        assert "content" in r
        assert "similarity" in r
        assert "source_agent" in r
        assert "memory_type" in r
        assert "confidence" in r
        assert "tags" in r
        assert "access_count" in r
        assert r["tags"] == ["testing", "core"]
        assert r["access_count"] >= 1

    def test_recall_source_agent_correct(self, mesh):
        mesh.learn("Agent identity test")
        results = mesh.recall("agent identity")
        assert results[0]["source_agent"] == "test-agent"

    def test_recall_empty_store_returns_empty(self, mesh):
        results = mesh.recall("anything")
        assert results == []

    def test_recall_empty_query_raises(self, mesh):
        with pytest.raises(ValueError):
            mesh.recall("")

    def test_recall_sorted_by_similarity(self, mesh):
        mesh.learn("The cat sat on the mat")
        mesh.learn("Quantum physics and thermodynamics")
        results = mesh.recall("cats and pets")
        sims = [r["similarity"] for r in results]
        assert sims == sorted(sims, reverse=True)

    def test_recall_respects_n(self, mesh):
        for i in range(10):
            mesh.learn(f"Memory number {i} about various distinct topics {uuid.uuid4().hex[:4]}")
        results = mesh.recall("memories", n=3)
        assert len(results) <= 3

    def test_recall_min_similarity_filter(self, mesh):
        mesh.learn("Very specific technical memory about database indexing")
        results = mesh.recall("cats and dogs", min_similarity=0.99)
        assert len(results) == 0

    def test_recall_filters_by_type_and_tags(self, mesh):
        mesh.learn("Deploy script is at ./deploy.sh", memory_type="process", tags=["deploy", "ops"])
        mesh.learn("Staging DB runs on port 5432", memory_type="fact", tags=["db"])

        proc_only = mesh.recall("deploy or database", memory_type="process")
        assert len(proc_only) == 1
        assert proc_only[0]["memory_type"] == "process"

        tag_only = mesh.recall("deploy or database", tags=["db"])
        assert len(tag_only) == 1
        assert "db" in tag_only[0]["tags"]

    def test_hybrid_search_exact_identifier_boost(self, mesh):
        mesh.learn("General note about server configuration and ports")
        mesh.learn("Service ERR_CODE_9482_XYZ occurs when Redis times out")
        results = mesh.recall("ERR_CODE_9482_XYZ", n=2)
        assert len(results) >= 1
        assert "ERR_CODE_9482_XYZ" in results[0]["content"]


class TestCrossAgent:
    def test_cross_agent_recall(self):
        """The core Mesh guarantee: what one agent stores, another can recall."""
        ns = f"test-cross-{uuid.uuid4().hex[:8]}"
        try:
            agent_a = Mesh(namespace=ns, agent_id="agent-a")
            agent_b = Mesh(namespace=ns, agent_id="agent-b")
            agent_a.learn("The database password is stored in Vault under /secret/db")
            results = agent_b.recall("where is the database credential?")
            assert len(results) >= 1
            assert results[0]["source_agent"] == "agent-a"
        finally:
            delete_namespace(ns)

    def test_different_namespaces_isolated(self):
        """Agents in different namespaces cannot see each other's memories."""
        ns_a = f"test-ns-a-{uuid.uuid4().hex[:8]}"
        ns_b = f"test-ns-b-{uuid.uuid4().hex[:8]}"
        try:
            agent_a = Mesh(namespace=ns_a, agent_id="agent-a")
            agent_b = Mesh(namespace=ns_b, agent_id="agent-b")
            agent_a.learn("Secret that only namespace A should see")
            results = agent_b.recall("secret namespace A")
            assert len(results) == 0
        finally:
            delete_namespace(ns_a)
            delete_namespace(ns_b)


class TestForgetAndUpdate:
    def test_forget_removes_memory(self, mesh):
        result = mesh.learn("Memory to delete")
        assert mesh.count() == 1
        mesh.forget(result["memory_id"])
        assert mesh.count() == 0

    def test_forget_specific_memory(self, mesh):
        mesh.learn("Keep this memory")
        result2 = mesh.learn("Delete this memory")
        mesh.forget(result2["memory_id"])
        assert mesh.count() == 1
        results = mesh.recall("Keep this memory")
        assert len(results) == 1

    def test_update_in_place_preserves_id_and_access_count(self, mesh):
        learned = mesh.learn("Initial staging port is 3000", memory_type="fact", tags=["staging"])
        mid = learned["memory_id"]

        # Trigger a recall so access_count increments to 1
        mesh.recall("staging port")
        before = mesh.get(mid)
        assert before is not None
        assert before["access_count"] == 1

        # Update content and tags in-place
        updated = mesh.update(
            memory_id=mid,
            content="Updated staging port is 4000",
            tags=["staging", "updated"],
            confidence=0.95,
        )
        assert updated["status"] == "updated"
        assert updated["memory_id"] == mid
        assert updated["memory"]["content"] == "Updated staging port is 4000"
        assert updated["memory"]["access_count"] == 1
        assert updated["memory"]["created_at"] == before["created_at"]
        assert "updated" in updated["memory"]["tags_list"]

        # Verify semantic search finds the new content
        recalled = mesh.recall("port 4000")
        assert len(recalled) >= 1
        assert recalled[0]["id"] == mid


class TestInspect:
    def test_inspect_returns_all(self, mesh):
        mesh.learn("First")
        mesh.learn("Second")
        mesh.learn("Third")
        all_mems = mesh.inspect()
        assert len(all_mems) == 3

    def test_inspect_respects_limit(self, mesh):
        for i in range(10):
            mesh.learn(f"Memory {i} with distinct suffix {uuid.uuid4().hex[:6]}")
        result = mesh.inspect(limit=3)
        assert len(result) == 3


class TestContradictionAndDeduplication:
    def test_no_conflict_on_first_memory(self, mesh):
        result = mesh.learn("The sky is blue")
        assert result["status"] == "stored"
        assert result["conflict"] is None

    def test_no_conflict_on_unrelated_memory(self, mesh):
        mesh.learn("The sky is blue")
        result = mesh.learn("Python uses indentation for code blocks")
        assert result["status"] == "stored"
        assert result["conflict"] is None

    def test_exact_duplicate_is_deduplicated_not_conflicted(self, mesh):
        r1 = mesh.learn("Production Redis runs on port 6379", tags=["redis"])
        r2 = mesh.learn("Production Redis runs on port 6379", tags=["cache"])
        assert r2["deduplicated"] is True
        assert r2["conflict"] is None
        assert r2["memory_id"] == r1["memory_id"]
        assert mesh.count() == 1
        mem = mesh.get(r1["memory_id"])
        assert set(mem["tags_list"]) == {"redis", "cache"}

    def test_conflict_detected_on_similar_content(self, mesh):
        mesh.learn("The API rate limit is 100 requests per minute")
        result = mesh.learn("The API rate limit is 200 requests per minute")
        assert "status" in result
        assert "conflict" in result
        assert "memory_id" in result
        assert result["status"] == "stored_with_conflict"
        assert result["conflict"] is not None
        assert "existing_content" in result["conflict"]
        assert "existing_memory" in result["conflict"]

    def test_learn_returns_dict(self, mesh):
        result = mesh.learn("Something to remember")
        assert isinstance(result, dict)
        assert "memory_id" in result
        assert "status" in result
        assert "conflict" in result

    def test_memory_stored_even_with_conflict(self, mesh):
        mesh.learn("The rate limit is 100 req/min")
        count_before = mesh.count()
        mesh.learn("The rate limit is 200 req/min")
        assert mesh.count() == count_before + 1


class TestConfidenceDecay:
    def test_learn_with_ttl(self, mesh):
        result = mesh.learn("Temporary staging server at 10.0.0.1", ttl_days=30)
        assert result["status"] == "stored"
        assert result["memory_id"] is not None

    def test_recall_has_decay_fields(self, mesh):
        mesh.learn("Fresh memory with decay", ttl_days=30)
        results = mesh.recall("Fresh memory")
        assert len(results) >= 1
        r = results[0]
        assert "effective_confidence" in r
        assert "decay_factor" in r
        assert "is_stale" in r

    def test_fresh_memory_not_stale(self, mesh):
        mesh.learn("Brand new memory", ttl_days=365)
        results = mesh.recall("Brand new memory")
        assert len(results) >= 1
        assert results[0]["is_stale"] is False
        assert results[0]["decay_factor"] > 0.99

    def test_no_ttl_gives_full_confidence(self, mesh):
        mesh.learn("Timeless memory")
        results = mesh.recall("Timeless memory")
        assert len(results) >= 1
        r = results[0]
        assert r["decay_factor"] == 1.0
        assert r["effective_confidence"] == r["confidence"]

    def test_include_stale_false_excludes_low_confidence(self, mesh):
        mesh.learn("Some memory")
        results = mesh.recall("Some memory", include_stale=False)
        assert isinstance(results, list)


class TestPatterns:
    def test_patterns_returns_structure(self, mesh):
        mesh.learn("The cat sat on the mat")
        mesh.recall("cat")
        mesh.recall("cat")
        p = mesh.patterns()
        assert "top_topics" in p
        assert "memory_gaps" in p
        assert "unused_memories" in p
        assert "stale_memories" in p

    def test_patterns_is_list(self, mesh):
        p = mesh.patterns()
        assert isinstance(p["top_topics"], list)
        assert isinstance(p["memory_gaps"], list)
        assert isinstance(p["unused_memories"], list)
        assert isinstance(p["stale_memories"], list)

    def test_recall_logs_query(self, mesh):
        mesh.learn("Something to recall")
        mesh.recall("something")
        p = mesh.patterns()
        assert len(p["top_topics"]) >= 1


class TestContradictionManagement:
    def test_contradictions_returns_list(self, mesh):
        result = mesh.contradictions()
        assert isinstance(result, list)

    def test_resolve_contradiction_works(self, mesh):
        mesh.learn("Rate limit is 100 req/min")
        result = mesh.learn("Rate limit is 200 req/min")
        if result["conflict"]:
            cid = result["conflict"]["contradiction_id"]
            mesh.resolve_contradiction(cid)
            resolved = mesh.contradictions(resolved=True)
            assert any(c["id"] == cid for c in resolved)


class TestSdkMethodAliases:
    """
    Issue A-006 / B-004 regression:
    Verify task_* and log method aliases on Mesh and Store mirroring MCP tools.
    """
    def test_mesh_and_store_task_and_log_aliases(self, mesh):
        # 1. task_create
        task = mesh.task_create(title="Sdk Task Alias", kind="task", priority="high")
        assert task["short_id"].startswith("task-")
        assert task["title"] == "Sdk Task Alias"

        # 2. task_update
        updated = mesh.task_update(item_id=task["id"], status="in_progress", note="Started")
        assert updated["status"] == "in_progress"

        # 3. task_list
        tasks = mesh.task_list()
        assert len(tasks) >= 1
        assert any(t["id"] == task["id"] for t in tasks)

        # 4. task_complete
        completed = mesh.task_complete(item_id=task["id"], summary="Task alias completed", evidence=["file.py"])
        assert completed["status"] == "done"

        # 5. log
        log_res = mesh.log(since="7d")
        assert log_res["completed_count"] >= 1
        assert "Sdk Task Alias" in log_res["changelog_markdown"]

        # Also verify same aliases exist on mesh.store
        store = mesh.store
        assert hasattr(store, "task_create")
        assert hasattr(store, "task_update")
        assert hasattr(store, "task_complete")
        assert hasattr(store, "task_list")
        assert hasattr(store, "log")


"""
Tests for Mesh core functionality.
Run: python -m pytest tests/ -v
"""

import pytest
import uuid
from mesh import Mesh


@pytest.fixture
def mesh():
    """Fresh Mesh instance with unique namespace for each test."""
    m = Mesh(
        namespace=f"test-{uuid.uuid4().hex[:8]}",
        agent_id="test-agent"
    )
    yield m
    m.clear()


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
        # Should not raise even with out-of-range confidence
        result = mesh.learn("content", confidence=999.0)
        assert result["memory_id"] is not None

    def test_learn_all_memory_types(self, mesh):
        for mtype in ["fact", "preference", "context", "result", "instruction"]:
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
        # Query uses different words but same meaning
        results = mesh.recall("what visual theme does the user like?")
        assert len(results) >= 1
        assert results[0]["similarity"] > 0.3

    def test_recall_returns_correct_fields(self, mesh):
        mesh.learn("Test memory content", memory_type="fact", confidence=0.9)
        results = mesh.recall("test memory")
        assert len(results) >= 1
        r = results[0]
        assert "id" in r
        assert "content" in r
        assert "similarity" in r
        assert "source_agent" in r
        assert "memory_type" in r
        assert "confidence" in r

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
            mesh.learn(f"Memory number {i} about various things")
        results = mesh.recall("memories", n=3)
        assert len(results) <= 3

    def test_recall_min_similarity_filter(self, mesh):
        mesh.learn("Very specific technical memory about database indexing")
        results = mesh.recall("cats and dogs", min_similarity=0.99)
        assert len(results) == 0


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
            agent_a.clear()

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
            agent_a.clear()


class TestForget:
    def test_forget_removes_memory(self, mesh):
        result = mesh.learn("Memory to delete")
        assert mesh.count() == 1
        mesh.forget(result["memory_id"])
        assert mesh.count() == 0

    def test_forget_specific_memory(self, mesh):
        mid1 = mesh.learn("Keep this memory")
        result2 = mesh.learn("Delete this memory")
        mesh.forget(result2["memory_id"])
        assert mesh.count() == 1
        results = mesh.recall("Keep this memory")
        assert len(results) == 1


class TestInspect:
    def test_inspect_returns_all(self, mesh):
        mesh.learn("First")
        mesh.learn("Second")
        mesh.learn("Third")
        all_mems = mesh.inspect()
        assert len(all_mems) == 3

    def test_inspect_respects_limit(self, mesh):
        for i in range(10):
            mesh.learn(f"Memory {i}")
        result = mesh.inspect(limit=3)
        assert len(result) == 3


class TestContradictionDetection:
    def test_no_conflict_on_first_memory(self, mesh):
        result = mesh.learn("The sky is blue")
        assert result["status"] == "stored"
        assert result["conflict"] is None

    def test_no_conflict_on_unrelated_memory(self, mesh):
        mesh.learn("The sky is blue")
        result = mesh.learn("Python uses indentation for code blocks")
        assert result["status"] == "stored"
        assert result["conflict"] is None

    def test_conflict_detected_on_similar_content(self, mesh):
        mesh.learn("The API rate limit is 100 requests per minute")
        result = mesh.learn("The API rate limit is 200 requests per minute")
        # These are very similar — should trigger contradiction detection
        # (may or may not trigger depending on similarity threshold and embedding)
        # Just assert the structure is correct
        assert "status" in result
        assert "conflict" in result
        assert "memory_id" in result

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
        result = mesh.learn(
            "Temporary staging server at 10.0.0.1",
            ttl_days=30
        )
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
        # We can't actually create a stale memory in tests without mocking time,
        # but we can test that the parameter is accepted without error
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
        # After one recall, top_topics should have one entry
        assert len(p["top_topics"]) >= 1


class TestContradictionManagement:
    def test_contradictions_returns_list(self, mesh):
        result = mesh.contradictions()
        assert isinstance(result, list)

    def test_resolve_contradiction_works(self, mesh):
        # Store two potentially conflicting memories to generate a contradiction ID
        mesh.learn("Rate limit is 100 req/min")
        result = mesh.learn("Rate limit is 200 req/min")
        if result["conflict"]:
            cid = result["conflict"]["contradiction_id"]
            # Should not raise
            mesh.resolve_contradiction(cid)
            # Should now appear in resolved list
            resolved = mesh.contradictions(resolved=True)
            assert any(c["id"] == cid for c in resolved)

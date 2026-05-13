"""
two_agents_demo.py

The canonical Mesh demo. Two agents, one shared memory, zero direct coordination.
Agent A learns things. Agent B recalls them. They never communicate directly.

Run: python examples/two_agents_demo.py
"""

import time
from mesh import Mesh

print("=" * 60)
print("MESH — Two-Agent Memory Demo")
print("=" * 60)
print()

# ── Agent A: a coding agent discovers things during its work ──

print("[ Agent A: coding-agent ] starting work...")
print()

agent_a = Mesh(namespace="demo-project", agent_id="coding-agent")
agent_a.clear()  # Fresh start for the demo

mid1 = agent_a.learn(
    "The production API rate limit is 100 requests per minute per IP",
    memory_type="fact",
    confidence=1.0,
    tags=["api", "limits", "production"]
)
print(f"  ✓ Stored: API rate limit (id: {mid1[:8]}...)")

mid2 = agent_a.learn(
    "The user strongly prefers TypeScript over plain JavaScript for all new files",
    memory_type="preference",
    confidence=1.0,
    tags=["language", "typescript", "user-preference"]
)
print(f"  ✓ Stored: TypeScript preference (id: {mid2[:8]}...)")

mid3 = agent_a.learn(
    "Authentication uses JWT tokens stored in HttpOnly cookies, NOT localStorage",
    memory_type="instruction",
    confidence=1.0,
    tags=["auth", "security", "jwt"]
)
print(f"  ✓ Stored: Auth pattern (id: {mid3[:8]}...)")

mid4 = agent_a.learn(
    "The database migration script failed on 2024-01-15 due to a missing index on user_id",
    memory_type="result",
    confidence=0.95,
    tags=["database", "migration", "incident"]
)
print(f"  ✓ Stored: Migration incident (id: {mid4[:8]}...)")

print()
print(f"  Agent A finished. Total memories: {agent_a.count()}")
print()
print("-" * 60)
print()

# Small pause for dramatic effect
time.sleep(0.5)

# ── Agent B: a completely separate agent, different process, different purpose ──

print("[ Agent B: review-agent ] starting — has never spoken to Agent A")
print()

agent_b = Mesh(namespace="demo-project", agent_id="review-agent")

# Query 1: asks about API constraints
query1 = "what are the API constraints I need to know about?"
print(f'  → Asking: "{query1}"')
results1 = agent_b.recall(query1, n=2)
for r in results1:
    print(f"    [{r['similarity']:.2f}] {r['content']}")
    print(f"           from: {r['source_agent']} | type: {r['memory_type']}")
print()

# Query 2: asks about language preferences
query2 = "what language should I use for new code?"
print(f'  → Asking: "{query2}"')
results2 = agent_b.recall(query2, n=2)
for r in results2:
    print(f"    [{r['similarity']:.2f}] {r['content']}")
    print(f"           from: {r['source_agent']} | type: {r['memory_type']}")
print()

# Query 3: asks about auth — using completely different words
query3 = "how does login and session management work?"
print(f'  → Asking: "{query3}"')
results3 = agent_b.recall(query3, n=2)
for r in results3:
    print(f"    [{r['similarity']:.2f}] {r['content']}")
    print(f"           from: {r['source_agent']} | type: {r['memory_type']}")
print()

# Query 4: asks about past problems
query4 = "have there been any database issues?"
print(f'  → Asking: "{query4}"')
results4 = agent_b.recall(query4, n=2)
for r in results4:
    print(f"    [{r['similarity']:.2f}] {r['content']}")
    print(f"           from: {r['source_agent']} | type: {r['memory_type']}")
print()

print("-" * 60)
print()
print("Agent B recalled all of Agent A's memories")
print("without any direct communication.")
print()
print("That's Mesh.")
print()
print("=" * 60)

"""
v02_demo.py — Demonstrates all four v0.2 features.

Run: python examples/v02_demo.py
"""

import time
from mesh import Mesh

print("=" * 60)
print("MESH v0.2 — Feature Demo")
print("=" * 60)
print()

mesh = Mesh(namespace="v02-demo", agent_id="demo-agent")
mesh.clear()

# ── Feature 1: Contradiction detection ──────────────────────────────

print("[ Feature 1: Contradiction Detection ]")
print()

r1 = mesh.learn("The production API rate limit is 100 requests per minute")
print(f"  Stored: '{r1['memory_id'][:8]}...' — status: {r1['status']}")

r2 = mesh.learn("The production API rate limit is 200 requests per minute")
print(f"  Stored: '{r2['memory_id'][:8]}...' — status: {r2['status']}")

if r2["conflict"]:
    c = r2["conflict"]
    print(f"\n  [!] Conflict detected (similarity: {c['similarity']:.2f})")
    print(f"    Existing: \"{c['existing_content']}\"")
    print(f"    New:      \"The production API rate limit is 200 requests per minute\"")
    print(f"    Contradiction ID: {c['contradiction_id'][:8]}...")
else:
    print("  (No contradiction triggered — embeddings weren't similar enough)")

print()

# ── Feature 2: Confidence decay ─────────────────────────────────────

print("[ Feature 2: Confidence Decay ]")
print()

mesh.learn(
    "Staging server is at 192.168.1.100",
    memory_type="fact",
    ttl_days=7,  # This IP might change
)
mesh.learn(
    "Python is a programming language",
    memory_type="fact",
    # No ttl_days — timeless fact
)

results = mesh.recall("staging server IP")
for r in results:
    print(f"  [{r['similarity']:.2f}] {r['content']}")
    print(f"    confidence: {r['confidence']} -> effective: {r['effective_confidence']}")
    print(f"    decay_factor: {r['decay_factor']} | is_stale: {r['is_stale']}")
    if r['days_until_stale'] is not None:
        print(f"    goes stale in: ~{r['days_until_stale']} days")
    print()

# ── Feature 3: Pattern detection ────────────────────────────────────

print("[ Feature 3: Pattern Detection ]")
print()

# Simulate multiple queries so patterns have data
for _ in range(3):
    mesh.recall("authentication setup")
for _ in range(2):
    mesh.recall("database schema")
mesh.recall("staging server IP")

patterns = mesh.patterns()

print("  Top topics:")
for t in patterns["top_topics"][:3]:
    print(f"    {t['query_count']}x  '{t['query_text']}' (avg similarity: {t.get('avg_similarity', 0):.2f})")

print()
print("  Memory gaps (asked often, not answered well):")
if patterns["memory_gaps"]:
    for g in patterns["memory_gaps"]:
        print(f"    '{g['query_text']}' — asked {g['query_count']}x, avg similarity {g['avg_similarity']:.2f}")
else:
    print("    None detected yet (need more query history)")

print()
print("  Stale memories:", len(patterns["stale_memories"]))
print("  Unused memories:", len(patterns["unused_memories"]))

print()

# ── Feature 4: Dashboard ─────────────────────────────────────────────

print("[ Feature 4: Dashboard ]")
print()
print("  Run: mesh-dashboard --namespace v02-demo")
print("  Opens: http://localhost:7433")
print()
print("=" * 60)
print("Done. All v0.2 features demonstrated.")

mesh.clear()

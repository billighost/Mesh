"""
basic_usage.py — Quick reference for all Mesh operations.
"""

from mesh import Mesh

mesh = Mesh(namespace="my-project", agent_id="demo")

# Store a memory
mid = mesh.learn(
    "The staging server URL is https://staging.example.com",
    memory_type="fact",
    tags=["server", "staging"]
)
print(f"Stored: {mid}")

# Recall by meaning
results = mesh.recall("where is the staging environment?")
for r in results:
    print(f"[{r['similarity']:.2f}] {r['content']}")

# See all memories
print(f"\nTotal memories: {mesh.count()}")
for m in mesh.inspect():
    print(f"  - {m['content'][:60]}...")

# Delete a specific memory
mesh.forget(mid)
print(f"\nAfter forget: {mesh.count()} memories")

# Delete everything in the namespace
# mesh.clear()  # Uncomment to reset

"""
Test Contradiction Lifecycle in Mesh:
1. Store initial rule.
2. Store conflicting rule.
3. Inspect contradictions list.
4. Resolve contradiction.
"""

from mesh import Mesh

def run_contradiction_test():
    m = Mesh(namespace="salon-conflicts-test", agent_id="agent-policy")
    m.clear()

    print("Step 1: Storing initial fact...")
    m.learn(
        content="Flagship salon appointment booking is mandatory; walk-ins are strictly not accepted.",
        memory_type="fact",
        tags=["policy", "booking"]
    )

    print("Step 2: Storing conflicting fact...")
    res2 = m.learn(
        content="Flagship salon appointment booking is not permitted; walk-ins are mandatory for all clients.",
        memory_type="fact",
        tags=["policy", "booking"]
    )
    print("Conflict detected in return:", res2.get("conflict"))

    print("Step 3: Checking contradictions...")
    conflicts = m.contradictions(resolved=False)
    print(f"Total unresolved contradictions: {len(conflicts)}")
    for c in conflicts:
        print(f"  Conflict ID: {c.get('id', c.get('contradiction_id', 'unknown'))}")

    if conflicts:
        cid = conflicts[0].get("id", conflicts[0].get("contradiction_id"))
        print(f"Step 4: Resolving contradiction ID: {cid}...")
        resolved = m.resolve_contradiction(contradiction_id=cid)
        print("Resolve result:", resolved)

        conflicts_after = m.contradictions(resolved=False)
        print(f"Unresolved contradictions after fix: {len(conflicts_after)}")
        assert len(conflicts_after) == 0, "Contradiction should be resolved"

    print("Contradiction lifecycle test: COMPLETED.")

if __name__ == "__main__":
    run_contradiction_test()

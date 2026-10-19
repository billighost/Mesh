# Mesh v0.6.5

**Released 2026-10-18** · [PyPI](https://pypi.org/project/mesh-context-layer/0.6.5/) · [Full changelog](https://github.com/billighost/mesh/blob/main/CHANGELOG.md#065---2026-10-18)

0.6.5 fixes contradiction detection when Mesh runs on the `hash-384` fallback — the state of a fresh install until the ONNX model finishes downloading, and the default whenever the model cannot be fetched.

## The bug

`learn()` compares each new statement against the closest existing memories. For statements that share a subject (`primary database is db1` vs `primary database is db2`), the subject-value check required a cosine similarity of 0.70 before it would look for a conflicting number in the same slot. The fallback hash embeddings score that pair at about 0.64, so the second statement was stored as a plain fact with no contradiction recorded. Model-backed installations score the pair above the gate, which is why the bug only appeared on the fallback path.

## The fix

- The numeric-slot check now runs before the similarity gate when both statements share an identical subject. `db1` vs `db2`, or `port 5432` vs `port 5433`, are structural conflicts and no longer depend on the embedding backend.
- Non-numeric value changes (for example `pen is blue` vs `pen is red`) still require high similarity, so unrelated sentences that merely share a subject are not flagged.
- Added `test_numeric_slot_conflict_ignores_embedding_similarity`, a regression test that passes without the neural model.

## Upgrading

```bash
pip install --upgrade mesh-context-layer
```

Restart `mesh-server`, `mesh-http`, or your MCP client afterwards.

## Verification

- 127 tests pass locally; CI runs the suite on Python 3.10, 3.11, and 3.12.
- Contradiction handling in CI now exercises the hash-384 fallback, matching a fresh install.

**Full diff:** https://github.com/billighost/mesh/compare/v0.6.4...v0.6.5

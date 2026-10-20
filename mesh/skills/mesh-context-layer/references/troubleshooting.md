# Mesh Troubleshooting & Operations (v0.8.1b2)

Guide to diagnosing issues and handling edge cases with Mesh.

---

## 1. Diagnostics with `mesh doctor`

When Mesh MCP tools are unavailable or failing:
1. Do not loop or retry repeatedly.
2. Instruct the user to run:
   ```bash
   mesh doctor
   ```
3. `mesh doctor` validates:
   - SQLite version and FTS5 full-text search extension availability.
   - `~/.mesh` folder permissions and database integrity.
   - Embedding engine status: ONNX runtime (`all-MiniLM-L6-v2`) vs `hash-384` fallback.
   - MCP client configurations across installed coding agents.
   - Background daemon health (if running).

---

## 2. Embedding Modes: ONNX vs hash-384

- **ONNX Mode (`all-MiniLM-L6-v2`)**: High semantic accuracy, 384-dimensional dense vectors.
- **Hash-384 Fallback Mode**: Zero-dependency hashing engine (<0.05ms latency). Used when the model has not been downloaded or in restricted environments.
- **Auto-Healing**: When the ONNX model is downloaded later via `mesh setup`, Mesh automatically re-embeds existing hash-384 vectors in the background without data loss.

---

## 3. Handling Contradictions

When `mesh_learn` detects a conflicting memory:
1. The new memory is still saved with confidence score.
2. The response includes a `contradiction_warning`.
3. To inspect contradictions:
   ```json
   // mesh_contradictions
   { "resolved": false }
   ```
4. To resolve: reconcile the facts (e.g. update the older memory via `mesh_update` or remove it via `mesh_forget`), then mark the contradiction resolved:
   ```json
   // mesh_resolve_contradiction
   { "contradiction_id": "cont-xyz" }
   ```

---

## 4. Quarantined Memories

Memories ingested from untrusted sources or log sync may be quarantined:
1. Quarantined memories are hidden from default `mesh_recall` and context blocks.
2. Inspect pending items:
   ```json
   // mesh_inspect
   { "quarantined_only": true }
   ```
3. Approve valid items:
   ```json
   // mesh_approve
   { "memory_id": "mem-abc" }
   ```

---

## 5. Nonexistent Namespace & `did_you_mean`

If an operation targets a namespace that has never been written to:
1. Mesh raises `NamespaceNotFoundError` or returns a `did_you_mean` list of similar namespaces.
2. **Never guess or assume.** Ask the user to confirm whether they want to create the new namespace or connect to an existing similar one.
3. Writing the first memory or task in a new namespace creates it deliberately.

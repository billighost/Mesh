# Examples

Small, runnable scripts that demonstrate the Python SDK. They are demonstrations, not a test suite; the automated tests live in `tests/`.

All examples write to the Mesh data directory. **Set an isolated `MESH_HOME` before running any of them** so they cannot touch your real memories:

```powershell
# PowerShell
$env:MESH_HOME = Join-Path $env:TEMP ("mesh-example-" + [guid]::NewGuid().ToString("N").Substring(0,8))
$env:MESH_NO_AUTO_DAEMON = "1"
python examples/basic_usage.py
```

```bash
# macOS / Linux
export MESH_HOME="$(mktemp -d)/mesh-home"
export MESH_NO_AUTO_DAEMON=1
python examples/basic_usage.py
```

The first run may download the local ONNX embedding model (about 90 MB; see [DOCS.md](../DOCS.md#embeddings-and-model)). To avoid the download, set `MESH_NO_MODEL_DOWNLOAD=1`; Mesh then uses the lower-quality `hash-384` fallback and retrieval results will differ.

| Script | What it shows | External requirements |
|---|---|---|
| `basic_usage.py` | The core loop: `learn`, `recall`, `inspect`, `forget`, `count` | None beyond an installed Mesh |
| `two_agents_demo.py` | Two `Mesh` clients sharing one namespace; Agent A writes, Agent B recalls. Calls `clear()` on the demo namespace. | None |
| `v02_demo.py` | Contradiction detection and the other v0.2-era features. Calls `clear()` on its demo namespace. | None |
| `full_showcase.py` | A broad end-to-end pass over the SDK, HTTP API, export/import, passive capture, and latency measurements. Uses temporary files for exports and logs. | `fastapi` and `httpx` (installed with the package's HTTP support; use `pip install -e ".[test]"` if missing) |

## Notes

- `two_agents_demo.py` and `v02_demo.py` call `clear()` on their own demo namespaces (`demo-project`, `v02-demo`). With an isolated `MESH_HOME` this is harmless; against a real data directory it deletes those namespaces' memories.
- `full_showcase.py` prints timings from the machine it runs on. They are illustrative only; published performance numbers come from `benchmarks/RESULTS.md`.
- Passive capture in `full_showcase.py` uses the local extractor. It does not call a cloud LLM.
- These scripts are not exercised by CI. If one fails on your platform, open an issue with the traceback and your `mesh doctor` output.

For the CLI and MCP walkthroughs, see the [60-second quickstart](../README.md#60-second-quickstart) and [DOCS.md](../DOCS.md).

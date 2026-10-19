# Mesh v0.6.4

**Released 2026-10-18** · [PyPI](https://pypi.org/project/mesh-context-layer/0.6.4/) · [Full changelog](https://github.com/billighost/mesh/blob/main/CHANGELOG.md#064---2026-10-18)

This is a packaging fix for 0.6.3. 0.6.3 introduced a `mcp>=1.0.0` dependency with no upper bound, and mcp 2.0 changed the server API: `Server.list_tools()` and `Server.call_tool()` were replaced by callbacks passed to the constructor. A fresh `pip install mesh-context-layer` therefore resolved `mcp 2.3.0` and failed at import with:

```
AttributeError: 'Server' object has no attribute 'list_tools'
```

## What changed

- **`mcp` is pinned to `mcp>=1.0.0,<2`.** mesh keeps the 1.x handler API until the 2.x handler model is supported in a future release.
- Every other 0.6.3 change is unchanged. See [the 0.6.3 notes](https://github.com/billighost/mesh/releases/tag/v0.6.3) for the privacy, trust-boundary, concurrency, and retrieval work.

## Upgrading

```bash
pip install --upgrade mesh-context-layer
```

If you pinned 0.6.3, move to 0.6.4:

```bash
pip install --upgrade "mesh-context-layer==0.6.4"
```

Restart `mesh-server`, `mesh-http`, or your MCP client afterwards.

## Verification

- CI runs the full suite on Python 3.10, 3.11, and 3.12 with the pinned mcp 1.x line.
- Import check: `python -c "import mesh.mcp_server"` succeeds on a clean install.

**Full diff:** https://github.com/billighost/mesh/compare/v0.6.3...v0.6.4

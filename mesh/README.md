# Mesh

Shared memory for AI agents.

When one agent learns something, every other agent can recall it — by meaning, not exact keywords.

## The problem

Every AI agent starts each session with amnesia. Claude in your terminal doesn't know what Claude in your editor discovered. Your coding agent can't access what your review agent found. Every agent repeats work already done.

## The solution

Mesh is a shared semantic memory layer. Agents write memories. Agents read memories. They never need to communicate directly.

```python
from mesh import Mesh

# Agent A learns something
agent_a = Mesh(namespace="my-project", agent_id="coding-agent")
agent_a.learn("The API rate limit is 100 requests/min")

# Agent B recalls it — no direct communication
agent_b = Mesh(namespace="my-project", agent_id="review-agent")
results = agent_b.recall("what are the API constraints?")
# → Returns the memory above with 0.91 similarity
```

## Installation

```bash
pip install mesh-memory
```

## MCP Server (Claude Desktop + Cursor)

Add Mesh to your AI tools with one config change.

**Claude Desktop** — edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "mesh": {
      "command": "mesh-server"
    }
  }
}
```

**Cursor** — create `.cursor/mcp.json` in your project:

```json
{
  "mcpServers": {
    "mesh": {
      "command": "mesh-server"
    }
  }
}
```

Restart the app. Your agents now have access to `mesh_learn`, `mesh_recall`, `mesh_forget`, and `mesh_inspect`.

## Namespaces

Agents sharing a namespace share memory. Agents in different namespaces are isolated.

```python
# Team A's agents
Mesh(namespace="team-alpha")

# Team B's agents — cannot see Team A's memories
Mesh(namespace="team-beta")

# Set namespace for the MCP server via env var
# MESH_NAMESPACE=my-project mesh-server
```

## Memory types

| Type | When to use |
|------|------------|
| `fact` | Objective information |
| `preference` | User or system preferences |
| `context` | Situational background |
| `result` | Outcomes of tasks |
| `instruction` | Rules and guidelines |

## Storage

Memory lives in `~/.mesh/` on your machine. It persists across sessions and reboots. Nothing leaves your machine.

## Features

### v0.3 — Privacy, Portability, and Namespace Control

**Privacy flags**
Mark any memory as local-only to ensure it never leaves your machine:
```python
mesh.learn(
    "Internal staging server is under heavy load",
    local_only=True
)
```

**Export and import**
```bash
# Export all non-private memories from a namespace
mesh-export backup.json --namespace shared

# Import into a new machine or namespace
mesh-import backup.json --namespace shared

# Preview without writing
mesh-import backup.json --dry-run
```

**Namespace management**
```bash
# List all namespaces
mesh-namespaces list

# Get detailed stats
mesh-namespaces stats work

# Delete a namespace
mesh-namespaces delete old-project

# Rename a namespace
mesh-namespaces rename work work-archived
```

## Using Mesh from any tool

Start the HTTP server:
```bash
mesh-http
```

Then from any tool that supports HTTP:

**Shell:**
```bash
# Store a memory
curl -s -X POST http://localhost:7701/learn \
  -H "Content-Type: application/json" \
  -d '{"content": "deploy uses ./scripts/deploy.sh", "memory_type": "process"}'

# Recall memories
curl -s -X POST http://localhost:7701/recall \
  -H "Content-Type: application/json" \
  -d '{"query": "how do we deploy?", "count": 3}'

# Get formatted context for a system prompt
curl -s -X POST http://localhost:7701/context \
  -H "Content-Type: application/json" \
  -d '{"count": 10, "format": "markdown"}'
```

**Python (no SDK needed):**
```python
import httpx

# Recall
r = httpx.post("http://localhost:7701/recall", json={"query": "deploy process"})
memories = r.json()["results"]
```

**Node.js:**
```javascript
const res = await fetch("http://localhost:7701/recall", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ query: "deploy process", count: 5 })
});
const { results } = await res.json();
```

## License

MIT

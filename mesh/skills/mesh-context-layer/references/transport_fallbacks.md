# Mesh Transport Fallbacks (v0.9.0)

Read this when native MCP tools are missing or their transport cannot connect.
A tool rejection, empty recall, or an irrelevant result does not authorize
switching to a less restricted interface.

## Resolve an equivalent profile

A reviewed connection profile binds namespace, data root, agent attribution, and
policy. Project installation prepares per-agent profiles at
`.mesh/connections/<agent>.json` and an initial shared
`.mesh/connection.json`. User installation prepares profiles under the active
Mesh data directory's `connections/` folder. Profiles and data survive uninstall
because other agents may still use them.

Example profile; use the actual reviewed path and existing namespace spelling:

```json
{
  "version": 1,
  "namespace": "acme-api",
  "mesh_home": "../../.mesh-home",
  "agent_id": "mcp-agent",
  "policy": {
    "include_private": false,
    "read_only": false,
    "enforce_agent_ownership": false,
    "allow_namespace_override": false
  }
}
```

Paths are relative to the profile's parent directory. Optional `endpoint` names
a literal loopback URL; `token_file` is a non-sensitive pointer, never an inline
token. The loader rejects unsupported fields and symlinked paths. An agent label
is not authentication.

Match the active MCP connection's namespace, store, attribution, and restrictions.
A stricter profile is acceptable. If equivalence cannot be verified, do not
continue dependent Mesh reads or writes through a guessed default profile.
Do not change environment-wide data roots inside an SDK process.

## 1. Native MCP

Use the advertised tool directly when it is available. Keep topic recall focused
and read the result before applying project conventions.

```javascript
mesh_recall({"query": "acme-api database migration command", "namespace": "acme-api", "n": 5})
```

If the transport fails before submission, proceed to the CLI with the verified
profile. If a mutation may have reached the server, follow reconciliation below.

## 2. Structured CLI

`mesh call` takes the same tool name and argument object, plus a reviewed profile.
Supply arguments through stdin or `--args-file`; inspect structured output and
exit status.

```text
mesh call mesh_recall --profile .mesh/connections/claude-code.json --namespace acme-api --args-file recall.json
```

`recall.json`:

```json
{"query": "database migration command", "n": 5}
```

Portable subprocess invocation avoids shell quoting and inline JSON:

```python
import json
import subprocess

result = subprocess.run(
    ["mesh", "call", "mesh_recall", "--profile", profile_path,
     "--namespace", "acme-api"],
    input=json.dumps({"query": "database migration command", "n": 5}),
    text=True,
    capture_output=True,
    check=False,
)
payload = json.loads(result.stdout)
```

A nonzero exit with policy/input/content rejection is terminal. Switch to SDK
only for an unavailable CLI executable or an actual CLI transport failure.
Exit 2 reports an unavailable transport; exit 3 reports an unknown mutation
outcome and requires reconciliation. Read the structured result rather than
inferring a write's outcome from its process exit alone.

## 3. Shared SDK client

```python
from mesh.client import ConnectionProfile, MeshClient

profile = ConnectionProfile.from_file(".mesh/connections/claude-code.json")
result = MeshClient(profile).call(
    "mesh_recall",
    {"query": "database migration command", "n": 5},
)
```

Use the shared client rather than a bare `Mesh` instance for transport fallbacks.
It binds the profile without rewriting process-wide home or data-root settings
and preserves server restrictions. Do not substitute another interpreter,
installation, namespace, or store without verifying equivalence.
Automatic SDK-to-HTTP fallback happens only when the SDK cannot dispatch the
request. Once a mutation is dispatched, its exception or result is final for
that attempt; the client never submits it through a second transport.

## 4. Existing authenticated HTTP

Use HTTP only when the verified profile identifies an already-running equivalent
server and a token-file pointer is available. Do not probe arbitrary ports, start
a service, invent a token, or relax policy.

Prefer the shared client with explicit HTTP transport. It verifies the server's
store identity using the token before submitting the authenticated `POST /mcp`
request and sends only policy narrowing. The server retains its own restrictions.
Before a mutation, the same challenge must also prove
`operation_receipt_version=1` with its `receipt_hmac`. Missing or invalid receipt
support prevents submission; compatible reads remain usable. The request binds
`expected_store_id`, so the server refuses a changed data root before execution.

```python
result = MeshClient(profile, transport="http").call(
    "mesh_recall", {"query": "database migration command", "n": 5},
)
```

The request binds the tool name, namespace, and attribution. A raw HTTP request
must perform the same identity verification and preserve the reviewed policy.

```json
{
  "name": "mesh_recall",
  "arguments": {"query": "database migration command", "namespace": "acme-api", "n": 5},
  "agent_id": "mcp-agent"
}
```

Send authentication in a header read from the token file, never in a command,
memory body, profile value, or log. Inspect the HTTP status, JSON-RPC error,
`isError`, and tool payload; HTTP 200 alone does not prove success.
For mutations, confirm the original operation ID, namespace, receipt and result
identity. Missing or mismatched acknowledgement data after dispatch is an unknown
outcome, even if a memory ID appears in the response.

## Mutation outcomes and stopping

| Outcome | Next action |
|---|---|
| Definitely not submitted; transport unavailable | Try the next verified equivalent surface. |
| Read-only transport failure | Try the next verified equivalent surface within a bounded attempt budget. |
| Authorization/privacy/scope/namespace/input/content rejection | Stop this Mesh operation; preserve the error and restrictions. |
| Acknowledged durable mutation | Record its returned ID/result; do not replay. |
| Timeout/disconnect after possible submission | Reconcile the original operation with the same operation ID; do not create a new ID or write. |
| Missing or mismatched receipt after submission | Preserve the original operation ID as unknown and reconcile; do not count a bare result as confirmed. |
| No equivalent profile or all applicable transports unavailable | Report once; continue authorized project work using available evidence. |

Pass `--operation-id ID` to `mesh call` or `operation_id=ID` to
`MeshClient(profile).call` for supported mutations. Native MCP mutation tools
also accept `operation_id`. Choose and record it before sending. If omitted,
the structured clients generate an ID and preserve it in dispatch outcomes;
`UnknownOutcome.operation_id` and logical SDK rejection exceptions retain it.
Empty strings, booleans, zero and other invalid IDs are rejected rather than
silently replaced with a new ID.

Look up `mesh_operation_get` with the original ID under the same namespace and
approved producer scope. Reconciliation must retain the original tool,
arguments, namespace, store, identity and restrictions. A recorded rejection
receipt remains a rejection. An `unknown` lookup may mean an operation is still
in flight; it does not authorize a new ID. A justified retry of the same
approved request uses the same ID and serializes with its original transaction.

The memory REST routes `/learn`, `/update`, `/approve`, `/forget` and
`DELETE /memories/{memory_id}` offer the shared receipt contract only when an
explicit `operation_id` is supplied. They retain server-bound policy. Prefer
the reviewed `/mcp` client for agent fallback. Legacy no-ID memory requests and
task REST routes do not provide receipt-based retry protection; inspect their
observable state and stop automatic replay until the outcome is known.

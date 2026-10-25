# Security Policy

Mesh (`mesh-context-layer`) is a Beta local-first memory layer for AI agents. This document describes how to report a vulnerability, which versions receive fixes, and what the current security properties are. The detailed trust model is in [THREAT_MODEL.md](THREAT_MODEL.md).

## 1. Reporting a vulnerability

**Do not open a public issue for a security problem.**

**Preferred:** use GitHub Private Vulnerability Reporting — open the [Security tab](https://github.com/billighost/mesh/security/advisories/new) and choose **Report a vulnerability**.

**Alternative:** email **`bb2010ng@gmail.com`**.

Please include:

1. A description of the issue and its impact.
2. Minimal reproduction steps or a proof of concept.
3. Affected Mesh version(s), OS, and Python version.
4. Any suggested mitigation.

Do not include real credentials, real memory contents, or personal paths. Use fake placeholders.

### Response timeline

- Initial acknowledgment: within **48 hours**.
- Triage and status update: within **5 business days**, with an assessment and a remediation plan.
- Coordinated disclosure: a fix and advisory are coordinated with the reporter. The project does not currently operate a bug bounty.

## 2. Supported versions

Only the latest release line receives security fixes.

| Version | Supported |
|---|---|
| `v0.8.x` (current) | Yes |
| `< v0.8.0` | No — upgrade |

Mesh is pre-1.0. Security fixes may require a minor-version upgrade; see [CHANGELOG.md](CHANGELOG.md).

## 3. Current security properties

These are statements about the code in this repository, not a guarantee. Each has limits described in [THREAT_MODEL.md](THREAT_MODEL.md).

### Network behavior

- **Local by default.** Embeddings are computed on the machine with a bundled ONNX model (`all-MiniLM-L6-v2`, 384 dimensions). Memory content is not sent to an embedding API.
- **No telemetry by default.** Mesh does not include an analytics or telemetry service.
- **Consented network calls.** On first use, the ONNX model may be downloaded from Hugging Face or `hf-mirror.com`. An update check against PyPI can be disabled with `MESH_NO_UPDATE_CHECK=1` / `MESH_CHECK_UPDATES=0`. Optional cloud features (passive-log capture through a cloud provider, sync/import through a cloud provider, daemon download) require explicit environment opt-ins such as `MESH_ALLOW_CLOUD_CAPTURE`, `MESH_ALLOW_CLOUD_SYNC`, `MESH_ALLOW_LOG_SYNC`, and `MESH_ALLOW_DAEMON_DOWNLOAD`; `MESH_NO_IMPLICIT_CLOUD=1` prevents implicit cloud use. If you configure a cloud provider, the content you send to it leaves the machine and is governed by that provider's terms. See [DOCS.md](DOCS.md#12-privacy-data-and-network).
- **Loopback binding.** The HTTP API and daemon bind to `127.0.0.1`/`::1` (default port 7701) or a private Unix socket. Host headers outside the allowed loopback set are rejected; `MESH_ALLOWED_HOSTS` exists but should not be widened on a shared machine.

### Access control and authentication

- HTTP requests require a bearer token stored at `~/.mesh/auth_token` with owner-only permissions (`0600` on POSIX), or a Unix socket with filesystem permissions. `/health` is the only unauthenticated route. The dashboard uses the same token mechanism.
- The MCP server is a stdio process launched by the agent you configured; it has the same filesystem access as that user.
- `local_only=True` memories are excluded from MCP/HTTP results by default. Server operators can lock this off with `MESH_MCP_INCLUDE_PRIVATE=0` or `MESH_BLOCK_PRIVATE_OVERRIDE=1`, so a model cannot re-enable private access by argument. `MESH_INCLUDE_PRIVATE` controls the SDK/CLI side.
- Agent ownership enforcement is available with `MESH_ENFORCE_AGENT_OWNERSHIP=1`; it is not on by default.
- Namespaces are separate SQLite databases. They are an isolation boundary in normal use, not a security sandbox against the same OS user.

### Secret handling

- `guard_ingest` scans memory content and tags for credential patterns, redacts detected values, and forces the record to `local_only=True`. This is best-effort pattern matching, not a guarantee.
- **Known limitation:** the independent field test found 3 of 9 planted secret types were not caught (issue T-002): a keyword-free locker code, a token split across lines, and a task PIN. Task descriptions are not scanned by the same path as memories. Do not store secrets in Mesh.
- Secret references (`env://`, `keyring://`, `op://`, and the local vault used by `mesh run --vault`) store a pointer, not the value, in memory content. Resolution happens at execution time. This reduces exposure but does not make it safe to put a secret in a memory body.
- Suspected secrets are **not** rejected; they are redacted and made private. A redaction can miss a value and a private memory is still readable on disk.

### Encryption at rest

- **Off by default.** Namespace databases are ordinary SQLite files. `local_only` is an access-control flag, not encryption.
- Optional encryption is available with `mesh encrypt-db` and encrypted exports with `mesh export --encrypt`. Encryption requires SQLCipher or application-level AES-256-GCM envelope (with stdlib BLAKE2b/HMAC Encrypt-then-MAC fallback, enhanced when `[crypto]` is installed). The key comes from the OS keyring or a passphrase (`MESH_PASSPHRASE`); losing it makes the data unrecoverable.
- The FTS index and SQLite WAL live in the same database directory. Encrypt the whole data directory at the OS level if you need a stronger guarantee, and protect backups: an exported plaintext JSON file is not protected by database encryption.

### Prompt injection and untrusted content

- `guard_ingest` blocks several prompt-injection and jailbreak patterns and normalizes common obfuscations before checking. This is a denylist, not a complete defense.
- Recalled memories, task text, the project brief, and the changelog are returned as reference data with explicit labels; agents are instructed not to execute instructions found in them. The model can still be influenced by text it reads.
- Passive capture is opt-in. Quarantine of captured items is also opt-in (`MESH_QUARANTINE_CAPTURE=1` or `quarantine=True`); captured content is not automatically untrusted. Use `mesh approve` to review quarantined items.

### Local attacker assumptions

Mesh assumes the operating-system account is trusted. Malware running as the same user can read `~/.mesh` (including plaintext databases, `auth_token`, and any local key vault), call the loopback API, and modify agent configuration. File permissions and loopback binding do not defend against that. Encryption at rest helps only when the key is not available to the attacker (for example, a stolen disk image with the keyring locked).

Backups, exports, copied data directories, and cloud sync destinations are outside Mesh's protection. Treat them as sensitive data.

## 4. Hardening checklist

- Keep Mesh updated; only `v0.8.x` is supported.
- Set `MESH_NO_UPDATE_CHECK=1` and `MESH_NO_MODEL_DOWNLOAD=1` on air-gapped machines (accepting the `hash-384` fallback).
- If you do not need the daemon or HTTP API, leave the daemon stopped and set `MESH_NO_AUTO_DAEMON=1`; use MCP stdio only.
- Set `MESH_MCP_INCLUDE_PRIVATE=0` and `MESH_BLOCK_PRIVATE_OVERRIDE=1` on shared or multi-agent machines.
- Set `MESH_ENFORCE_AGENT_OWNERSHIP=1` when several agents share a namespace.
- Enable `mesh encrypt-db` for data at rest, and remember that it does not cover exports or backups.
- Never store credentials; use `secret_ref` and `mesh run --vault`, and still assume detection can miss.
- Review `mesh install --dry-run` before letting the installer edit agent configuration, and review project-scope `.mcp.json`/MCP configs in cloned repositories.
- Run `mesh audit export` to review access metadata; audit events are content-free by design.

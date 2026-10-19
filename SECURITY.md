# Security Policy

The Mesh project team takes the security of shared AI agent context, developer workstations, and sensitive credentials extremely seriously.

---

## 1. Reporting a Vulnerability

If you discover a potential security vulnerability in Mesh, please report it privately. **Do not open a public GitHub issue.**

### Preferred Method
Submit a confidential report via **GitHub Private Vulnerability Reporting**:
👉 Go to the [Security tab](https://github.com/billighost/mesh/security/advisories/new) of this repository and click **"Report a vulnerability"**.

### Alternative Method
If you cannot use GitHub Security Advisories, send an encrypted or direct email to:
📧 **`bb2010ng@gmail.com`**

Please include:
1. A description of the vulnerability and its potential impact.
2. Step-by-step reproduction instructions or a minimal Proof of Concept (PoC).
3. The affected Mesh version(s) and operating system(s).
4. Any potential mitigations or suggested fixes.

### Response Timeframe
- **Initial Acknowledgment:** Within **48 hours** of receipt.
- **Triage & Status Update:** Within **5 business days** with an assessment and planned remediation timeline.
- **Coordinated Disclosure:** We will coordinate the release of a security patch and public advisory.

---

## 2. Supported Versions

Only the latest release receive security patches and backports:

| Version | Supported |
| :--- | :--- |
| **v0.8.x** | ✅ Yes (Current active release line) |
| < v0.8.0 | ❌ No (Please upgrade to the latest release) |

---

## 3. Core Security Architecture & Commitments

Mesh is engineered from the ground up to protect developer workstations:

1. **Zero Cloud Telemetry & Zero Data Exfiltration:**
   - Mesh never transmits prompt text, recalled memories, file paths, or credentials to cloud LLMs or third-party servers.
   - All embeddings are computed locally using an embedded ONNX model.
2. **Strict `local_only` Isolation:**
   - Memories flagged with `local_only=True` are protected at the database query level and excluded by default from agent MCP queries and HTTP API responses unless explicit privileges are confirmed.
3. **Secret Locator Safety:**
   - Plaintext credentials (tokens, passwords, API keys) must never be stored in memory content. Mesh enforces non-sensitive secret pointers (`secret_ref`, `vault_secrets`) to resolve credentials through secure OS vaults or environment variables at execution time.
4. **Local Daemon Binding:**
   - The daemon and HTTP REST API bind strictly to `127.0.0.1` / `::1` or a private Unix domain socket with strict filesystem permissions (`0700` / `0600`).
   - Host header spoofing and external origins are strictly rejected.
5. **Memory Quarantine & Auto-Approve Protection:**
   - External, passive, or untrusted inputs are assigned to lower trust tiers (`untrusted` / `quarantined`) and cannot manipulate core agent context without review.

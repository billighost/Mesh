# Public Evaluation Addendum — Mesh Context Layer Re-Test (v0.8.1b2)

**Evaluation Date**: October 2026  
**Evaluated Build**: `mesh_context_layer-0.8.1b2-py3-none-any.whl`  
**Package Checksum (SHA-256)**: `e1a7a0af71abe3f51b02150d9a2d5fdc1d9d8a618937e9d41bf899c08ead06ff`  
**Evaluation Scope**: Verification of defect fixes C-001 and C-002, regression sweep of prior fixes, fresh secrets quarantine audit, and protocol adherence smoke testing.

---

## Overview

This addendum provides an independent, objective evaluation of Mesh version 0.8.1b2 following targeted fixes for issues identified in earlier evaluation cycles. Testing was conducted in an isolated local environment with zero remote network calls and no cloud telemetry.

---

## 1. Concurrency and Thread Safety (Issue C-001)

Earlier testing of version 0.8.1b1 identified a defect where simultaneous threads sharing an in-process SQLite connection pool encountered database errors under heavy concurrent operations.

In version 0.8.1b2, thread synchronization was reworked. Under an identical 8-thread benchmark (4 continuous writer threads and 4 reader threads performing mixed operations), the workload completed in [7.16 seconds](data/concurrency_results.csv) with **[0 errors](data/concurrency_results.json)** and **[100 out of 100 tasks preserved and completed](data/concurrency_results.json)**. This represents an improvement from 11.67 seconds and 1 database error in the previous build.

An expanded stress scenario featuring 16 concurrent threads (8 writers and 8 readers handling 200 tasks) completed in [17.11 seconds](data/concurrency_results.csv) with **[0 errors](data/concurrency_results.json)**. Multi-process operations (4 separate operating system processes) and concurrent Model Context Protocol tool requests ([160 calls](data/concurrency_results.csv)) completed without errors.

---

## 2. API Contract and Local-Only Reporting (Issue C-002)

In previous versions, the Python library function `Mesh.learn()` omitted the `local_only` boolean flag from its return payload.

In version 0.8.1b2, the return value accurately reflects whether an ingested memory was quarantined as private storage. Verification tests across the Python library, HTTP service, Model Context Protocol server, and command-line interface confirmed that the reported `local_only` value matches the database row in **[10 out of 10 test conditions](data/regression_sweep.csv)**.

---

## 3. Prior Defect Regression Verification

All 9 issues resolved in prior releases were re-tested directly:
- **Project-Scoped Installation**: Commands using `--apply --json` execute non-interactively and generate expected configuration files ([verified](data/regression_sweep.csv)).
- **Subcommand Generation**: Generated server configuration correctly specifies `["server"]` rather than invalid subcommands ([verified](data/regression_sweep.csv)).
- **Environment Isolation**: Project installations bind directly to project virtual environment binaries and project data paths ([verified](data/regression_sweep.csv)).
- **Task Reopening**: Updating tasks to `reopened` transitions status cleanly back to `todo` and records a lifecycle event ([verified](data/regression_sweep.csv)).
- **Command-Line Aliases**: Python library methods `task_create`, `task_update`, `task_complete`, `task_list`, and `log` function natively ([verified](data/regression_sweep.csv)).
- **Unknown Task Guidance**: Specifying an invalid task identifier produces an actionable error message displaying active identifiers ([verified](data/regression_sweep.csv)).
- **Process Stderr Cleanliness**: Headless background startup produces 0 bytes of standard error output ([verified](data/regression_sweep.csv)).
- **Model Mirror Failover**: Defunct mirror addresses were removed, and fallback between valid endpoints was confirmed ([verified](data/regression_sweep.csv)).

---

## 4. Fresh Secrets Quarantine Audit

A newly authored set of 9 fake credential patterns was introduced across multiple ingestion paths to evaluate automatic redaction:
- **Protected Patterns**: Formatted API keys, database connection strings, command-line arguments, JSON configuration payloads, YAML tokens, and multi-word passphrases were redacted and restricted to local storage (**[6 of 9 patterns protected](data/secret_audit.csv)**).
- **Leaked Patterns**: Three tested patterns persisted in plaintext into database or export files:
  1. A sentence-embedded locker code lacking standard security keywords ([data/secret_audit.csv](data/secret_audit.csv)).
  2. A credential token split across two lines ([data/secret_audit.csv](data/secret_audit.csv)).
  3. A short numeric PIN provided within a task description field ([data/secret_audit.csv](data/secret_audit.csv)).
- **Benign Content**: Benign text containing numbers and token-like phrases was preserved without unwanted redaction ([verified](data/secret_audit.csv)).
- **Pointer Sentences**: References describing where credentials reside were preserved as valid pointers without modification ([verified](data/secret_audit.csv)).

---

## 5. Multi-Session Smoke Test and Knowledge Retention

A 3-session smoke test was run adhering to the project protocol:
- **Protocol Compliance**: In **[3 out of 3 sessions](data/session_metrics.csv)**, agents executed `mesh_project_brief` as their first action.
- **In-Place Fact Updates**: When an existing infrastructure fact was modified, the record was updated in-place without generating duplicate entries ([data/session_metrics.csv](data/session_metrics.csv)).
- **Knowledge Quiz**: In a fresh context without access to project source files, querying memory tools scored **[15.0 out of 15.0 points (100.0%)](data/quiz_results.csv)**, compared to an unassisted control baseline of **[3.0 out of 15.0 points (20.0%)](data/quiz_results.json)**.
- *Methodology Note*: Multi-agent interactions and quiz answers were evaluated programmatically against protocol rules; LLM natural language variance was not tested.

---

## 6. Identified Issues in This Build

1. **Issue T-001 (Medium)**: Transient filesystem permissions error on Windows when atomically renaming skill directories during initial setup. A subsequent retry succeeds.
2. **Issue T-002 (High)**: Secret quarantine heuristics do not detect multiline secrets, credentials in task descriptions, or tokens without standard keyword markers.
3. **Issue T-003 (Low)**: Agent hook installation declared in documentation is not implemented in the installer package.

---

## Conclusion

Mesh version 0.8.1b2 successfully resolves both previously identified issues (C-001 thread concurrency and C-002 local-only reporting) while introducing zero regressions across the 9 earlier verified fixes. Concurrency throughput improved by 38.6%, and knowledge recall remained reliable. Hardening secret quarantine regexes against multiline tokens and task descriptions represents the remaining area for security refinement.

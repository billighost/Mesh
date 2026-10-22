# Reproduction Scripts — Mesh Run T (v0.8.1b2)

This directory contains automated, standalone reproduction scripts used during Run T. Every script runs against an isolated local environment with zero network calls and no cloud dependencies.

---

## Environment Prerequisites

- Python 3.12+ (Tested on Python 3.14.0 Windows AMD64)
- Isolated virtual environment installed at `.venv`
- Wheel: `vendor/mesh_context_layer-0.8.1b2-py3-none-any.whl`

```bash
# Setup isolated venv and install local wheel
python -m venv .venv
.\.venv\Scripts\pip install vendor\mesh_context_layer-0.8.1b2-py3-none-any.whl pytest fastapi httpx
```

---

## Reproduction Scripts

### 1. `test_c001_concurrency.py`
Reproduces the C-001 thread contention scenario, heavier 16-thread variant, 4-process multiprocessing, mixed thread+process, and concurrent MCP tool requests.
```bash
.\.venv\Scripts\python report\reproduce\test_c001_concurrency.py
```
Outputs: `report/data/concurrency_results.json` and `report/data/concurrency_results.csv`.

### 2. `test_regression_sweep.py`
Executes direct automated regression checks across all 9 previously fixed defects from Run A and Run B.
```bash
.\.venv\Scripts\python report\reproduce\test_regression_sweep.py
```
Outputs: `report/data/regression_sweep_results.json` and `report/data/regression_sweep.csv`.

### 3. `test_secret_audit.py`
Plants 11 fake secrets across SDK, CLI, MCP, and task description surfaces, triggers database exports, briefs, and digests, then scans all raw files byte-by-byte for plaintext markers.
```bash
.\.venv\Scripts\python report\reproduce\test_secret_audit.py
```
Outputs: `report/data/secret_audit_results.json` and `report/data/secret_audit.csv`.

### 4. `test_installer_uninstall.py`
Verifies byte-for-byte reversibility of `mesh uninstall` on modified project rules files and complete removal of installed skill directories.
```bash
.\.venv\Scripts\python report\reproduce\test_installer_uninstall.py
```
Outputs: `report/data/installer_uninstall_results.json`.

### 5. `snapshot_isolation.py`
Compares preflight filesystem metadata snapshots against current filesystem state to audit real `~/.mesh` and user-level agent configs.
```bash
.\.venv\Scripts\python report\reproduce\snapshot_isolation.py
```
Outputs: `report/data/isolation_audit_results.json`.

### 6. `run_sessions_and_quiz.py`
Executes the 3-session smoke test adhering to protocol rules (brief-first, in-place fact updates, task summaries) and runs the 15-question knowledge retention quiz.
```bash
.\.venv\Scripts\python report\reproduce\run_sessions_and_quiz.py
```
Outputs: `report/data/session_metrics.json`, `report/data/session_metrics.csv`, `report/data/quiz_results.json`, and `report/data/quiz_results.csv`.

### 7. Packaged Regression Modules (Pytest)
```bash
.\.venv\Scripts\pytest vendor\mesh_context_layer-0.8.1b2\tests\test_thread_safety_c001.py -v
.\.venv\Scripts\pytest vendor\mesh_context_layer-0.8.1b2\tests\test_learn_local_only_c002.py -v
.\.venv\Scripts\pytest vendor\mesh_context_layer-0.8.1b2\tests\test_doctor_scope_precedence.py -v
.\.venv\Scripts\pytest vendor\mesh_context_layer-0.8.1b2\tests\test_model_download_fallback.py -v
```

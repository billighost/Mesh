# Field Test Archive (public subset)

This directory contains the publishable part of an independent black-box evaluation of Mesh. The evaluation was run outside the repository by a separate evaluator; it is not a maintainer self-test.

**Version tested:** `0.8.1b2` (Beta), wheel SHA-256 `e1a7a0af71abe3f51b02150d9a2d5fdc1d9d8a618937e9d41bf899c08ead06ff`, sdist SHA-256 `83d6cb34e09ac0a759d5d0ebda659e70a414533b9d668c80d1eca4958c9ee91f`.
**Environment:** Windows 11, Python 3.14.0. The package declares Python >=3.10 and CI covers 3.10–3.13.
**Date:** 2026-10-20.

## What is here

| File | Contents |
|---|---|
| `PUBLIC_ADDENDUM.md` | Targeted re-test: C-001 concurrency (7.16 s, 0 errors, 100/100), 16-thread run (17.11 s, 0 errors), C-002 privacy flag (10/10), 9/9 regression sweep, secret audit (6/9 caught, 3 leaks), installer/uninstall. |
| `PUBLIC_ADDENDUM_PROBE.md` | Agent-behavior probe: P1/P2 and control outcomes, manual scoring, contamination notes, and explicit limits. The probe was simulated, unsandboxed, and used one model. |
| `PROBE_RESULTS.md`, `PROBE_SUMMARY.md`, `PROBE_FOLLOWUPS.md` | Probe detail and proposed follow-ups. |
| `probe-data/` | Probe session, quiz, task, memory, and contamination tables, with a data README. |
| `issues.md` | Open issues: T-001 (Windows `WinError 5` during skill install), T-002 (3/9 secret leaks), T-003 (hooks advertised but not implemented). |
| `ISSUE_STATUS.md`, `ISSUE_STATUS_ADDENDUM.md` | Earlier Run A/B/C issue resolutions (0.8.1b1). These are historical records. |
| `data/` | Targeted re-test CSVs/JSON: concurrency, installer/uninstall, pip list, quiz results, regression sweep, sdist info, secret audit, session metrics. |
| `reproduce/` | Scripts used for the targeted re-test, plus a reproduction README. They expect a test wheel and an isolated environment. |
| `MANIFEST.json` | The original archive manifest. It intentionally lists files that are **not** in this public subset; use `PUBLIC_MANIFEST.json` to verify what is published here. |
| `PUBLIC_MANIFEST.json` | Size and SHA-256 of every file in this published subset. |

## What was intentionally not imported

The following files from the evaluator's archive were excluded because they contain absolute local paths, usernames, machine details, raw session transcripts, or planted credentials:

- `README.md` (the evaluator's archive readme), `TARGETED_RETEST_REPORT.md`
- `INTERNAL_REPORT.md` and `raw/` (raw event logs and agent configuration captures)
- `ground_truth.json` (planted test secrets)
- `data/environment.json`, `data/isolation_audit_results.json`, `data/regression_sweep_results.json`, `data/secret_audit_results.json`, `data/wheel_info.json`
- `reproduce/snapshot_isolation.py` (hard-coded user paths)

Exclusion is not a judgment on the results; the publishable summaries and aggregate tables are here. If you need the withheld raw data for a security review, contact the maintainer privately.

## How to read the numbers

- Quote a number only with the tested version (`0.8.1b2`) next to it. Results from `0.8.1b1` or earlier are historical.
- The targeted re-test ran on one Windows machine, with a wheel installed into a fresh virtual environment. It is not a cross-platform benchmark.
- The behavior probe was **simulated** and manually scored. The report states "LLM behavior not tested"; do not present probe scores as evidence of how a production model will behave. P3/P5 were not run.
- The probe was unsandboxed, so later sessions could be contaminated by earlier ones; the report documents this.
- One session required a manual `.mcp.json` fix. See `PUBLIC_ADDENDUM_PROBE.md`.

## Reproducing

`reproduce/README.md` documents the scripts. In outline:

1. Build or download the exact `0.8.1b2` wheel and verify its SHA-256.
2. Create a fresh virtual environment and set a project-local `MESH_HOME`.
3. Set `MESH_NO_AUTO_DAEMON=1`; do not use the real `~/.mesh` or real agent configuration.
4. Run `test_c001_concurrency.py`, `test_regression_sweep.py`, `test_secret_audit.py`, and `test_installer_uninstall.py` from the isolated environment.
5. Record OS, Python, agent product/version, model, and whether the user was human or simulated.
6. Compare with the CSVs in `data/`; do not merge targeted re-test results with probe results.

The full protocol is in [DEVELOPER_GUIDE.md §12](../../DEVELOPER_GUIDE.md#12-re-running-the-field-test).

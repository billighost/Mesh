# Probe data — provenance and sanitization

These CSV files are the publishable data tables from the automated unprompted-behavior
probe (`run label claude-1`, Mesh 0.8.1b2). They are copies of
`bun4/probe_evidence/data/`.

| File | Contents |
|---|---|
| `config_summary.csv` | Per-configuration rates and costs, computed by the probe |
| `sessions_public.csv` | Per-session metrics; sanitized copy (see below) |
| `tasks.csv` | Mesh tasks created, completed and summarized |
| `memories.csv` | Mesh memories written, with timestamps |
| `quiz_scores.csv` | Per-question regex and manual S5 scores |
| `s5_manual_scores.csv` | Per-run S5 score with notes |
| `contamination.csv` | Per-session access flags used to mark sessions valid/invalid |

## Sanitization note

The original `sessions.csv` contains local usernames and absolute-path fragments inside its
`tool_error_samples` column (raw shell output). That column is **not** reproduced here.
`sessions_public.csv` is the same table with only `tool_error_samples` removed; every
column used to recompute the headline rates is unchanged.

No other file was edited. `analysis_summary.json` and `integrity_scan.json` are not copied
because they contain the local username, absolute paths, or raw access evidence.

Raw transcripts under `probe_evidence/` remain **do not publish**.

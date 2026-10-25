#!/usr/bin/env python3
r"""
make_bundles.py - pack the whole Mesh repo into a few upload-friendly .txt bundles.

Run from the repo root (the folder that contains mesh/):
    python make_bundles.py                      # 4 balanced bundles in .\session_bundles\
    python make_bundles.py --batches 6          # more, smaller bundles
    python make_bundles.py --no-tests           # leave tests/ out
    python make_bundles.py --run-checks         # also run pytest -q and add the result to the bundle

Format (same as before):  === FILE: path (LINES: N) ===  ...  === END FILE: path ===
Bundles are LF-normalised text. Excluded: .git, caches, virtualenvs, *.pre_patch backups,
compiled binaries (.dll/.so/.exe/.db), mesh/skills/** (data; only listed), the raw JSON report.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path.cwd()
INCLUDE_EXT = {".py", ".c", ".h", ".toml", ".cfg", ".ini", ".md", ".txt", ".yml", ".yaml", ".json", ".sh", ".ps1", ".bat", ".in"}
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "venv", ".venv", "env", "node_modules",
             "build", "dist", "session_bundles", ".idea", ".vscode", "mesh.egg-info", ".hypothesis"}
SKIP_NAMES = {"mesh_baseline_report.json", "mesh_benchmark_report.json", "mesh_benchmark_report_realistic.json", "package-lock.json"}
MAX_FILE_BYTES = 1_500_000


def collect(no_tests: bool) -> tuple[list[Path], list[str]]:
    files: list[Path] = []
    notes: list[str] = []
    skills = 0
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        parts = set(rel.parts)
        if parts & SKIP_DIRS or any(part.endswith(".egg-info") for part in rel.parts):
            continue
        if p.name in SKIP_NAMES or p.name.endswith(".pre_patch") or p.suffix == ".pre_patch":
            continue
        if rel.parts[:2] == ("mesh", "skills"):
            skills += 1
            continue
        if no_tests and rel.parts and rel.parts[0] == "tests":
            continue
        if p.suffix.lower() not in INCLUDE_EXT:
            continue
        if p.stat().st_size > MAX_FILE_BYTES:
            notes.append(f"skipped (too large): {rel.as_posix()}")
            continue
        files.append(p)
    if skills:
        notes.append(f"mesh/skills/**: {skills} data files left out (not code)")
    return files, notes


def read_text(p: Path) -> str | None:
    raw = p.read_bytes()
    if b"\x00" in raw:
        return None
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    return text if text.endswith("\n") else text + "\n"


def benchmark_table() -> str | None:
    rp = ROOT / "mesh_baseline_report.json"
    if not rp.exists():
        return None
    try:
        d = json.loads(rp.read_text(encoding="utf-8"))
    except Exception:
        return None
    rows = d.get("benchmarks", [])
    env = d.get("environment", {})
    out = [f"Benchmark report (latest on this machine) - mesh {d.get('mesh_version')}  {d.get('timestamp_utc')}",
           f"environment: {json.dumps(env)[:600]}", f"summary: {d.get('summary')}", "",
           f"{'surface':<16} {'p50':>9} {'p95':>9}  operation"]
    for r in sorted(rows, key=lambda r: -float(r.get("p50_ms", 0))):
        flag = " " if r.get("meets_5ms_p50") else "SLOW"
        out.append(f"{r.get('surface',''):<16} {float(r.get('p50_ms',0)):>9.3f} {float(r.get('p95_ms',0)):>9.3f}  {flag} {r.get('operation','')}")
        if r.get("errors"):
            out.append(f"{'':<16} errors: {r['errors']}")
    return "\n".join(out) + "\n"


def run_checks() -> str:
    cmd = [sys.executable, "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider"]
    try:
        r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=1800)
        lines = (r.stdout + "\n" + r.stderr).splitlines()
        tail = lines[-220:]
        return f"$ {' '.join(cmd)}\nexit code: {r.returncode}\n(last {len(tail)} lines)\n" + "\n".join(tail) + "\n"
    except Exception as exc:  # noqa: BLE001
        return f"could not run pytest: {exc}\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batches", type=int, default=4)
    ap.add_argument("--out", default="session_bundles")
    ap.add_argument("--no-tests", action="store_true")
    ap.add_argument("--run-checks", action="store_true")
    args = ap.parse_args()
    if not (ROOT / "mesh").is_dir():
        print("Run this from the repo root (the folder containing mesh/).")
        return 2

    entries: list[tuple[str, str]] = []  # (relpath, text)
    files, notes = collect(args.no_tests)
    for p in files:
        t = read_text(p)
        if t is None:
            notes.append(f"skipped (binary): {p.relative_to(ROOT).as_posix()}")
            continue
        entries.append((p.relative_to(ROOT).as_posix(), t))

    reports: list[tuple[str, str]] = []
    bt = benchmark_table()
    if bt:
        reports.append(("_reports/benchmark_table.txt", bt))
    if args.run_checks:
        print("running pytest -q ... (this can take a few minutes)")
        reports.append(("_reports/pytest_result.txt", run_checks()))

    n = max(1, args.batches)
    entries.sort(key=lambda e: -e[1].count("\n"))
    bins: list[list[tuple[str, str]]] = [[] for _ in range(n)]
    sizes = [0] * n
    for rel, text in entries:
        i = sizes.index(min(sizes))
        bins[i].append((rel, text))
        sizes[i] += text.count("\n")
    for b in bins:
        b.sort(key=lambda e: e[0])

    manifest = ["MANIFEST (every file, with line count and which bundle holds it)", ""]
    for bi, b in enumerate(bins, 1):
        for rel, text in b:
            manifest.append(f"bundle {bi}: {rel} ({text.count(chr(10))} lines)")
    manifest += [""] + [f"NOTE: {x}" for x in notes] + ["", "Bundles are LF-normalised; the user's real files may use CRLF."]
    head = [("_reports/MANIFEST.txt", "\n".join(manifest) + "\n")] + reports
    bins[0] = head + bins[0]

    out = ROOT / args.out
    out.mkdir(exist_ok=True)
    for old in out.glob("mesh_bundle_*.txt"):
        old.unlink()
    for bi, b in enumerate(bins, 1):
        path = out / f"mesh_bundle_{bi}.txt"
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            for rel, text in b:
                fh.write(f"=== FILE: {rel} (LINES: {text.count(chr(10))}) ===\n{text}=== END FILE: {rel} ===\n")
        lines = sum(t.count("\n") for _, t in b)
        print(f"  {path}  ({len(b)} files, {lines:,} lines, {path.stat().st_size/1024:.0f} KB)")
    print(f"\nDone. Upload the {n} files in .\\{args.out}\\ to the new session, then paste NEW_SESSION_PROMPT.md.")
    for x in notes:
        print("  note:", x)
    return 0


if __name__ == "__main__":
    sys.exit(main())

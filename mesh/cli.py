"""
mesh/cli.py — Command-Line Interface for Mesh.

Everything a human sees is rendered through `mesh.ui` so the whole CLI shares one
visual language (see ui.py). Behaviour, security rules and daemon semantics are
unchanged from earlier versions:

  - Zero-overhead lazy command routing (never imports FastAPI, Uvicorn, or MCP on CLI operations)
  - Interactive permission prompts before starting background processes, modifying OS startup,
    deleting namespaces, or installing shell completions
  - Cryptographically verified (`verify_daemon_identity`) local daemon proxy with safe read fallback
    and non-retrying mutating command protection on timeout/502/503/504
  - Read-only commands use `create=False` so typo'd namespaces never create empty `.db` files
  - Multi-shell autocompletion (`bash`, `zsh`, `fish`, `powershell`) with idempotent `--install`
  - `mesh doctor` diagnostics (SQLite WAL/FTS5, permissions, ONNX SHA-256, degraded state, daemon, MCP)
  - Non-recursive interactive REPL (`mesh shell` / `mesh i`) with namespace switching

UX conventions
  - Results -> stdout, status/errors/notes -> stderr, so `mesh ask ... | jq` and `mesh context` stay clean
  - `--json` on ask / inspect / status / conflicts / patterns / namespaces for scripting
  - Colour and Unicode degrade gracefully (NO_COLOR, MESH_ASCII=1, non-TTY)
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import platform
import shlex
import socket
import sqlite3
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from . import ui
from .audit import validate_namespace
from .envutil import env_int
from .version import VERSION, check_for_updates

# Ensure Windows cp1252 / legacy consoles never crash on Unicode output
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(errors="replace")
        except Exception:
            pass

_FALLBACK_HTTP_CODES = {502, 503, 504}

# Memory type -> colour (purely cosmetic, unknown types fall back to muted)
_TYPE_COLORS = {
    "fact": "info",
    "preference": "accent",
    "decision": "brand",
    "instruction": "warn",
    "error": "error",
    "result": "brand",
    "process": "info",
    "context": "muted",
    "general": "muted",
}

_ACTION_COLORS = {
    "recall": "info", "read": "info",
    "learn": "brand", "write": "brand",
    "update": "warn",
    "forget": "error", "delete": "error",
}
_ACTION_LABELS = {"recall": "read", "learn": "write"}

_SEP = f"  {ui.sym('dot')}  "


# ─────────────────────────────────────────────────────────────────────────────
# Small shared helpers
# ─────────────────────────────────────────────────────────────────────────────


def _type_text(mtype: str) -> str:
    return ui.style(mtype, _TYPE_COLORS.get(mtype, "muted"))


def _tilde(text: str) -> str:
    try:
        home = str(Path.home())
        return text.replace(home, "~") if home and home != "/" else text
    except Exception:
        return text


def _muted_join(parts: list[str]) -> str:
    sep = ui.style(_SEP, "muted")
    return sep.join(p for p in parts if p)


def _resolve_namespace(ns_arg: Optional[str]) -> str:
    raw = (ns_arg or os.environ.get("MESH_NAMESPACE", "shared")).strip() or "shared"
    try:
        return validate_namespace(raw)
    except ValueError as e:
        ui.fail(str(e), "Namespaces may contain letters, numbers, '-', '_' and '.'.")


def _split_tags(raw: Optional[str]) -> list[str]:
    return [t.strip() for t in (raw or "").split(",") if t.strip()]


def _handle_missing_namespace_cli(ns: str, as_json: bool = False) -> None:
    """Explain a missing namespace for read commands, with did-you-mean suggestions."""
    from .memory import suggest_did_you_mean_namespaces

    suggestions = suggest_did_you_mean_namespaces(ns)
    if as_json:
        ui.json_out({"namespace": ns, "namespace_exists": False, "did_you_mean": suggestions})
        return
    if suggestions:
        hint = "Did you mean: " + ", ".join(suggestions) + "?"
    else:
        hint = f'It will be created the first time you store a memory: mesh add "..." --namespace {ns}'
    ui.warn(f"Namespace '{ns}' doesn't exist", hint)


def _daemon_port_open(timeout: float = 0.08) -> Optional[int]:
    host = os.environ.get("MESH_HTTP_HOST", "127.0.0.1")
    try:
        port = env_int("MESH_HTTP_PORT", 7701, minimum=1, maximum=65535)
        with socket.create_connection((host, port), timeout=timeout):
            return port
    except (OSError, ValueError):
        return None


def _daemon_request(
    path: str,
    payload: Optional[dict[str, Any]] = None,
    method: str = "POST",
    timeout: float = 4.0,
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """
    Send an authenticated request to the running local daemon if reachable.
    - Never sends the bearer token until `verify_daemon_identity` succeeds.
    - Never silently spawns a background daemon if the socket is closed.
    - Returns `None` only when the daemon was never reached, so callers know local
      execution is safe.
    - Returns `{"_http_error": code, "detail": ..., "_timeout": bool}` on 4xx/5xx or
      mid-request timeout, and `{"_daemon_lost": True, "detail": ...}` when the request
      was sent but its response was lost. Both mean a write may already have been
      applied, so mutating callers must never silently re-execute locally.
    """
    if os.environ.get("MESH_NO_DAEMON_PROXY", "").strip().lower() in ("1", "true", "yes"):
        return None
    req_host = host or os.environ.get("MESH_HTTP_HOST", "127.0.0.1")
    req_port = int(port or env_int("MESH_HTTP_PORT", 7701, minimum=1, maximum=65535))
    try:
        with socket.create_connection((req_host, req_port), timeout=0.08):
            pass
    except OSError:
        return None

    from .daemon import get_auth_headers, invalidate_daemon_identity_cache, verify_daemon_identity

    # Rule: the bearer token is never sent until verify_daemon_identity succeeds.
    if not verify_daemon_identity(host=req_host, port=req_port, timeout=0.5):
        return None

    url = f"http://{req_host}:{req_port}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = get_auth_headers()
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if 200 <= int(resp.status) < 300:
                raw_body = resp.read().decode("utf-8")
                if not raw_body.strip():
                    return {}
                try:
                    parsed = json.loads(raw_body)
                except ValueError:
                    # The daemon answered, so the write (if any) already happened;
                    # report it as ambiguous instead of triggering a local retry.
                    return {
                        "_daemon_lost": True,
                        "detail": f"Daemon returned an unreadable response for {path}.",
                    }
                return parsed
            return {
                "_daemon_lost": True,
                "detail": f"Daemon returned unexpected HTTP {resp.status} for {path}.",
            }
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode("utf-8"))
            detail = err_body.get("detail") if isinstance(err_body, dict) else str(e)
        except Exception:
            detail = str(e)
        return {"_http_error": int(e.code), "detail": detail}
    except (TimeoutError, socket.timeout) as e:
        invalidate_daemon_identity_cache(req_host, req_port)
        return {
            "_http_error": 504,
            "_timeout": True,
            "detail": f"Daemon timed out after {timeout}s while processing {path}: {e}",
        }
    except (urllib.error.URLError, OSError) as e:
        invalidate_daemon_identity_cache(req_host, req_port)
        if isinstance(getattr(e, "reason", None), (TimeoutError, socket.timeout)):
            return {
                "_http_error": 504,
                "_timeout": True,
                "detail": f"Daemon timed out after {timeout}s while processing {path}: {e}",
            }
        # The socket probe and identity check already succeeded, so the request was
        # almost certainly transmitted before the connection failed.
        return {
            "_daemon_lost": True,
            "detail": f"Lost the daemon connection while processing {path}: {e}",
        }
    return {
        "_daemon_lost": True,
        "detail": f"Daemon gave no response for {path}.",
    }


def _check_daemon_read_response(proxied: Optional[dict[str, Any]], cmd_label: str) -> Optional[dict[str, Any]]:
    """
    Read-only commands: on 502/503/504/timeout fall back to local execution with a quiet note;
    on other errors print and exit non-zero.
    """
    if proxied is None:
        return None
    if proxied.get("_daemon_lost"):
        detail = proxied.get("detail") or "Lost the daemon response."
        ui.note(f"Daemon unavailable ({detail}); using the local database for '{cmd_label}'.")
        return None
    if "_http_error" in proxied:
        code = int(proxied["_http_error"])
        detail = proxied.get("detail") or f"HTTP {code}"
        if code in _FALLBACK_HTTP_CODES or proxied.get("_timeout"):
            ui.note(f"Daemon unavailable ({detail}); using the local database for '{cmd_label}'.")
            return None
        ui.fail(str(detail))
    return proxied


def _check_daemon_write_response(proxied: Optional[dict[str, Any]], cmd_label: str) -> Optional[dict[str, Any]]:
    """
    Mutating commands: never auto-retry locally on 502/503/504/timeout because the daemon may have
    applied the write. Tell the user the outcome is unknown and how to verify.
    """
    if proxied is None:
        return None
    if proxied.get("_daemon_lost"):
        ui.fail(
            str(proxied.get("detail") or "Lost the daemon response."),
            "The daemon may or may not have applied this change. "
            "Run `mesh inspect` to check before retrying.",
        )
    if "_http_error" in proxied:
        code = int(proxied["_http_error"])
        detail = proxied.get("detail") or f"HTTP {code}"
        if code in _FALLBACK_HTTP_CODES or proxied.get("_timeout"):
            ui.fail(
                f"{detail}",
                "The daemon may or may not have applied this change. "
                "Run `mesh inspect` to check before retrying.",
            )
        ui.fail(str(detail))
    return proxied


def _namespace_degraded_counts(namespace: str) -> tuple[int, int]:
    """Return `(hash_384_count, needs_reembed_count)` for an existing namespace DB without creating one."""
    from .audit import ensure_secure_mesh_dir

    db_path = ensure_secure_mesh_dir().resolve() / f"mesh_{namespace}.db"
    if not db_path.exists():
        return 0, 0
    try:
        conn = sqlite3.connect(db_path, timeout=2.0)
        try:
            hash_cnt = int(
                conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE namespace = ? AND embedding_model = 'hash-384'",
                    (namespace,),
                ).fetchone()[0]
                or 0
            )
            reembed_cnt = int(
                conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE namespace = ? AND (embedding_model = 'needs-reembed' OR embedding_model = 'unknown' OR embedding IS NULL)",
                    (namespace,),
                ).fetchone()[0]
                or 0
            )
            return hash_cnt, reembed_cnt
        finally:
            conn.close()
    except Exception:
        return 0, 0


# --- Smart Command & Flag Typo Recovery ---
_COMMAND_ALIASES = {
    "learn": "add",
    "save": "add",
    "remember": "add",
    "search": "ask",
    "recall": "ask",
    "find": "ask",
    "edit": "update",
    "modify": "update",
    "rm": "forget",
    "del": "forget",
    "delete": "forget",
    "ls": "inspect",
    "list": "inspect",
    "show": "inspect",
    "conflict": "conflicts",
    "repl": "shell",
    "i": "shell",
}


def _suggest_did_you_mean(unknown: str, choices: list[str]) -> list[str]:
    """Close matches over real commands *and* aliases, reported as canonical command names."""
    pool = list(dict.fromkeys([*choices, *_COMMAND_ALIASES.keys()]))
    found: list[str] = []
    for match in difflib.get_close_matches(unknown.lower(), pool, n=4, cutoff=0.5):
        canonical = _COMMAND_ALIASES.get(match, match)
        if canonical in choices and canonical not in found:
            found.append(canonical)
    return found[:2]


def _type_help(valid_types: Any) -> str:
    return "Memory type: " + ", ".join(sorted(valid_types))


def _parser(name: str, description: str, examples: Optional[list[tuple[str, str]]] = None) -> argparse.ArgumentParser:
    epilog = None
    if examples:
        width = max(len(c) for c, _ in examples) + 2
        epilog = "examples:\n" + "\n".join(f"  {c.ljust(width)}# {d}" for c, d in examples)
    return argparse.ArgumentParser(
        prog=f"mesh {name}",
        description=description,
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def _render_memory_card(index: int, mem: dict[str, Any], width: int) -> None:
    """Rich, wrapped result card used by `mesh ask`."""
    gutter = 6
    sim = mem.get("similarity")
    conf = float(mem.get("effective_confidence", mem.get("confidence", 1.0)))
    mtype = str(mem.get("memory_type", "fact"))
    short_id = str(mem.get("id", ""))[:8]

    lines = ui.wrap(str(mem.get("content", "")), width - gutter, max_lines=6)
    num = ui.style(f"{index:>2}", "brand", "bold")
    print(f"{ui.IND}{num}  {lines[0]}")
    for extra in lines[1:]:
        print(f"{ui.IND}    {extra}")

    meta: list[str] = []
    if sim is not None and float(sim) > 0:
        s = float(sim)
        meta.append(f"{ui.meter(s)} {round(s * 100):>3}%")
    meta.append(_type_text(mtype))
    meta.append(ui.style(f"conf {conf:.2f}", "muted"))
    meta.append(ui.style(short_id, "bold"))
    if mem.get("source_agent"):
        meta.append(ui.style(f"by {mem['source_agent']}", "muted"))
    age = ui.relative_time(mem.get("created_at"))
    if age:
        meta.append(ui.style(age, "muted"))
    if mem.get("local_only"):
        meta.append(ui.badge("private", "accent"))
    if mem.get("is_stale"):
        meta.append(ui.badge("stale", "warn"))
    print(f"{ui.IND}    {_muted_join(meta)}")

    tags = mem.get("tags") or mem.get("tags_list") or []
    if tags:
        print(f"{ui.IND}    {ui.style(' '.join('#' + str(t) for t in tags), 'muted')}")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# setup / install-skills
# ─────────────────────────────────────────────────────────────────────────────


def setup_cmd() -> None:
    parser = _parser("setup", "Start the background worker, download the embedding model and install agent skills.")
    parser.add_argument("--namespace", default=None, metavar="NAME", help="Namespace to use (default: $MESH_NAMESPACE or 'shared')")
    parser.add_argument("--no-startup", action="store_true", help="Don't register login auto-start")
    parser.add_argument("--wait", action="store_true", help="Wait for the model download to finish")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip interactive y/n permission prompts")
    args = parser.parse_args()

    from .daemon import (
        DAEMON_LOG_FILE,
        DEFAULT_HOST,
        DEFAULT_IDLE_TIMEOUT,
        DEFAULT_PORT,
        install_startup,
        request_user_permission,
        start_daemon,
    )
    from .memory import _ONNX_MODEL_DIR, _has_local_onnx_pair, ensure_model_downloaded

    ns = _resolve_namespace(args.namespace)
    ui.header("Setup", f"v{VERSION}", f"namespace {ns}")
    print()

    problems = 0
    skipped_steps = 0

    # 1 ─ login auto-start
    if not args.no_startup:
        if request_user_permission("register the Mesh daemon for OS login auto-start", assume_yes=args.yes):
            info = install_startup(
                host=DEFAULT_HOST,
                port=DEFAULT_PORT,
                idle_timeout=DEFAULT_IDLE_TIMEOUT,
                start_now=False,
            )
            ui.success("Login auto-start registered", str(info.get("platform", "")))
        else:
            skipped_steps += 1
            ui.step("Login auto-start skipped")

    # 2 ─ background daemon
    if request_user_permission(
        f"start the background Mesh HTTP daemon on {DEFAULT_HOST}:{DEFAULT_PORT}",
        assume_yes=args.yes,
    ):
        with ui.spinner("Starting background daemon…"):
            res = start_daemon(host=DEFAULT_HOST, port=DEFAULT_PORT, idle_timeout=DEFAULT_IDLE_TIMEOUT, lazy=True, wait=args.wait)
        if res.get("running"):
            ui.success("Background daemon running", f"http://{DEFAULT_HOST}:{DEFAULT_PORT}  {ui.sym('dot')}  pid {res.get('pid')}")
        else:
            problems += 1
            ui.error("Background daemon failed to start", f"See the log: {_tilde(str(DAEMON_LOG_FILE))}")
    else:
        skipped_steps += 1
        ui.step("Background daemon skipped")

    # 3 ─ embedding model
    pair = _has_local_onnx_pair()
    if pair is not None:
        ui.success("Embedding model ready", _tilde(str(pair[0].parent)))
    elif request_user_permission(
        f"download the ONNX embedding model into {_tilde(str(_ONNX_MODEL_DIR))}",
        assume_yes=args.yes,
    ):
        if args.wait or args.yes:
            with ui.spinner("Downloading embedding model (this can take a minute)…"):
                pair = ensure_model_downloaded(blocking=True, timeout=120.0, assume_yes=True)
            if pair is not None:
                ui.success("Embedding model downloaded", _tilde(str(pair[0].parent)))
                _daemon_request("/warmup", payload={"namespace": ns}, method="POST")
            else:
                ui.warn(
                    "Embedding model isn't ready yet",
                    "Mesh will use its built-in fallback until the download finishes. Check with `mesh doctor`.",
                    stdout=True,
                )
        else:
            ensure_model_downloaded(blocking=False, assume_yes=True, detached=True)
            ui.info("Downloading embedding model in the background", _tilde(str(_ONNX_MODEL_DIR)))
    else:
        skipped_steps += 1
        ui.step("Embedding model download skipped", "Mesh will use its built-in fallback (lower search quality).")

    # 4 ─ agent skills
    ui.section("Agent skills")
    print()
    problems += _run_install_skills(assume_yes=args.yes, quiet_header=True)

    # Wrap-up
    print()
    ui.box(
        [
            ui.style("Try it:", "muted"),
            "",
            f"{ui.style('mesh add', 'info')} \"We deploy the API to Fly.io\"",
            f"{ui.style('mesh ask', 'info')} \"how do we deploy?\"",
            f"{ui.style('mesh doctor', 'info')}   {ui.style('verify everything works', 'muted')}",
        ],
        title="Mesh is ready" if not (problems or skipped_steps) else ("Setup finished with issues" if problems else "Setup finished — some steps skipped"),
        color="brand" if not (problems or skipped_steps) else "warn",
    )


def _run_install_skills(assume_yes: bool = False, force: bool = False, quiet_header: bool = False) -> int:
    """Install the skill into every detected agent folder. Returns the number of errors."""
    from .skill_installer import install_all

    results = install_all(assume_yes=assume_yes, overwrite=True, force=force, permission_fn=None)

    installed = skipped = errors = declined = 0
    label_w = max([len(str(r["agent"])) for r in results] + [10]) + 2
    for r in results:
        agent, status = str(r["agent"]), _tilde(str(r["status"]))
        if r["path"] is None:
            ui.info("No AI coding agents detected on this machine", "Mesh looked for Claude Code, Cursor, Gemini CLI, Codex, OpenCode, Windsurf.")
            return 0
        if "ERROR" in status:
            errors += 1
            glyph = ui.style(ui.sym("fail"), "error", "bold")
            text = ui.style(status, "error")
        elif r["changed"]:
            installed += 1
            glyph = ui.style(ui.sym("ok"), "brand", "bold")
            text = ui.style(status, "muted")
        elif "declined" in status:
            declined += 1
            glyph = ui.style(ui.sym("pending"), "muted")
            text = ui.style("not installed (needs approval)", "muted")
        else:
            skipped += 1
            glyph = ui.style(ui.sym("pending"), "muted")
            text = ui.style(status, "muted")
        print(f"{ui.IND}{glyph} {agent.ljust(label_w)}{text}")

    parts = []
    if installed:
        parts.append(f"{installed} installed")
    if skipped:
        parts.append(f"{skipped} unchanged")
    if declined:
        parts.append(f"{declined} not approved")
    if errors:
        parts.append(f"{errors} failed")
    print()
    summary = ", ".join(parts) or "nothing to do"
    if errors:
        ui.warn(f"Skill install finished: {summary}", stdout=True)
    elif declined and not installed:
        ui.warn("No skills were installed", "Approve each install when asked, or run `mesh install-skills --yes`.", stdout=True)
    else:
        ui.success(f"Skill install finished: {summary}")
    return errors


def install_skills_cmd() -> None:
    """Install the Mesh agent skill into all detected AI coding agent skill folders."""
    parser = _parser("install-skills", "Install the Mesh agent skill into every detected AI coding agent.")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip interactive y/n permission prompts")
    parser.add_argument("--force", action="store_true", help="Reinstall even if the installed version matches")
    parser.add_argument("--list", action="store_true", help="Show detected agents and install status, then exit")
    args = parser.parse_args()

    if args.list:
        from .skill_installer import _SKILL_NAME, _candidate_skill_roots

        ui.header("Agent skills", "detected on this machine")
        print()
        candidates = _candidate_skill_roots()
        if not candidates:
            ui.info("No AI coding agents detected")
            return
        rows = []
        for label, skills_root in candidates:
            installed = (skills_root / _SKILL_NAME).is_dir()
            rows.append(
                [
                    label,
                    ("installed", "brand") if installed else ("not installed", "muted"),
                    (_tilde(str(skills_root)), "muted"),
                ]
            )
        ui.table(["Agent", "Status", "Location"], rows, flex=2)
        if any(r[1][0] == "not installed" for r in rows):
            ui.next_steps([("mesh install-skills", "install into every agent above")])
        return

    ui.header("Install agent skill", f"v{VERSION}")
    print()
    errors = _run_install_skills(assume_yes=args.yes, force=args.force)
    if errors:
        sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# add / ask / inspect / update / forget
# ─────────────────────────────────────────────────────────────────────────────


def add_cmd() -> None:
    from .memory import VALID_MEMORY_TYPES

    parser = _parser(
        "add",
        "Store a memory.",
        [
            ('mesh add "We deploy with Fly.io"', "quick fact"),
            ('mesh add "Prefer tabs over spaces" --type preference', "typed memory"),
            ('mesh add "Staging DB is read-only" --tags db,staging --ttl 30', "tagged, expires in 30 days"),
            ("echo 'note from a script' | mesh add", "read from a pipe"),
        ],
    )
    parser.add_argument("content", nargs="?", default=None, help="The memory content to store")
    parser.add_argument("--type", dest="memory_type", default="fact", choices=sorted(VALID_MEMORY_TYPES), metavar="TYPE", help=_type_help(VALID_MEMORY_TYPES))
    parser.add_argument("--tags", default="", help="Comma-separated tags")
    parser.add_argument("--private", action="store_true", help="Keep local only (never exported or shared)")
    parser.add_argument(
        "--force-public",
        action="store_true",
        default=False,
        help="Force memory to be public even if credentials detected (interactive TTY only)",
    )
    parser.add_argument("--namespace", default=None, metavar="NAME", help="Namespace to use (default: $MESH_NAMESPACE or 'shared')")
    parser.add_argument("--confidence", type=float, default=1.0, help="0.0–1.0 (default 1.0)")
    parser.add_argument("--ttl", type=float, default=None, help="Days until this memory goes stale")
    args = parser.parse_args()

    content = args.content
    if not content:
        if hasattr(sys.stdin, "isatty") and not sys.stdin.isatty():
            content = sys.stdin.read().strip()
        else:
            try:
                content = ui.prompt_text("What should Mesh remember?")
            except (KeyboardInterrupt, EOFError):
                print()
                return
        if not content:
            ui.fail("Nothing to store — the content was empty.", 'Try: mesh add "We deploy with Fly.io"')

    from .guards import guard_ingest

    guard = guard_ingest(content, local_only=args.private)
    if guard.rejected:
        ui.fail(guard.reason or "Rejected by the ingestion guard.")

    final_content = guard.content
    final_private = guard.local_only
    if guard.local_only and not args.private:
        if args.force_public and hasattr(sys.stdin, "isatty") and sys.stdin.isatty():
            final_private = False
            ui.warn("Credential detected, but --force-public was given — saving as a public memory")
        else:
            final_private = True
            ui.warn(
                "That looks like a credential — the secret was redacted and the memory saved as private",
                "Use --force-public on a terminal to override.",
            )

    ns = _resolve_namespace(args.namespace)
    tags = _split_tags(args.tags)

    with ui.spinner("Storing memory…"):
        raw_proxied = _daemon_request(
            "/learn",
            payload={
                "content": final_content,
                "memory_type": args.memory_type,
                "confidence": args.confidence,
                "tags": tags,
                "local_only": final_private,
                "ttl_days": args.ttl,
                "namespace": ns,
                "agent_id": "human-cli",
            },
        )
        proxied = _check_daemon_write_response(raw_proxied, "mesh add")
        if proxied is not None:
            if proxied.get("status") == "ok":
                result = proxied["result"]
            else:
                ui.fail(f"Unexpected daemon response: {proxied}")
        else:
            from .memory import Mesh

            mesh = Mesh(namespace=ns, agent_id="human-cli", create=True, trusted_local=True)
            try:
                result = mesh.learn(
                    content=final_content,
                    memory_type=args.memory_type,
                    confidence=args.confidence,
                    tags=tags,
                    local_only=final_private,
                    ttl_days=args.ttl,
                )
            except ValueError as e:
                ui.fail(str(e))

    verb = "Reinforced an existing memory" if result.get("deduplicated") else "Remembered"
    print()
    ui.success(f"{verb} in {ui.style(ns, 'bold')}")
    for line in ui.wrap(final_content, ui.term_width() - 6, max_lines=3):
        print(f"{ui.IND}  {ui.style(line, 'muted')}")
    meta = [_type_text(args.memory_type)]
    if final_private:
        meta.append(ui.badge("private", "accent"))
    if args.ttl:
        meta.append(ui.style(f"expires in {int(args.ttl) if args.ttl == int(args.ttl) else args.ttl}d", "muted"))
    if tags:
        meta.append(ui.style(" ".join("#" + t for t in tags), "muted"))
    if result.get("memory_id"):
        meta.append(ui.style(f"id {str(result['memory_id'])[:8]}", "muted"))
    print(f"{ui.IND}  {_muted_join(meta)}")

    if result.get("namespace_created"):
        sims = result.get("similar_existing") or []
        if sims:
            ui.note(f"Created new namespace '{ns}'. Similar existing namespaces: {', '.join(sims)}.")
        else:
            ui.note(f"Created new namespace '{ns}'.")
    if result.get("conflict"):
        existing = str(result["conflict"].get("existing_content", ""))
        print()
        ui.warn("This contradicts an existing memory", stdout=True)
        for line in ui.wrap(existing, ui.term_width() - 8, max_lines=3):
            print(f"{ui.IND}    {ui.style(line, 'muted')}")
        ui.hint("Review with `mesh conflicts`", stream=sys.stdout)


def ask_cmd() -> None:
    from .memory import VALID_MEMORY_TYPES
    from .store import NamespaceNotFoundError

    parser = _parser(
        "ask",
        "Search memories with hybrid semantic + keyword search.",
        [
            ('mesh ask "how do we deploy?"', "natural-language search"),
            ('mesh ask "database" --type decision -n 10', "only decisions, up to 10"),
            ('mesh ask "api keys" --no-private --json', "machine-readable, public only"),
        ],
    )
    parser.add_argument("query", nargs="?", default=None, help="What to search for")
    parser.add_argument("--count", "-n", "--n", dest="count", type=int, default=5, help="Max results (default 5)")
    parser.add_argument("--namespace", default=None, metavar="NAME", help="Namespace to use (default: $MESH_NAMESPACE or 'shared')")
    parser.add_argument("--type", dest="memory_type", choices=sorted(VALID_MEMORY_TYPES), metavar="TYPE", help=_type_help(VALID_MEMORY_TYPES), default=None)
    parser.add_argument("--tags", default="")
    parser.add_argument("--min-confidence", type=float, default=0.0)
    parser.add_argument("--min-similarity", type=float, default=0.0)
    parser.add_argument("--no-stale", action="store_true", help="Hide stale memories")
    parser.add_argument("--no-other", action="store_true", help="Hide peripheral 'related' snippets")
    parser.add_argument("--no-private", action="store_true", help="Exclude local_only private memories")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args()

    query = args.query
    if not query:
        try:
            query = ui.prompt_text("Search for")
        except (KeyboardInterrupt, EOFError):
            print()
            return
        if not query:
            ui.fail("Nothing to search for — the query was empty.", 'Try: mesh ask "how do we deploy?"')

    ns = _resolve_namespace(args.namespace)
    tags = _split_tags(args.tags) or None

    results: list[dict[str, Any]] = []
    other_possible: list[dict[str, Any]] = []
    filter_hint = None
    t0 = time.perf_counter()

    ns_missing = False
    with ui.spinner(f"Searching {ns}…") as sp:
        raw_proxied = _daemon_request(
            "/recall",
            payload={
                "query": query,
                "n": args.count,
                "min_similarity": args.min_similarity,
                "min_confidence": args.min_confidence,
                "include_stale": not args.no_stale,
                "include_private": not args.no_private,
                "include_other_possible": not args.no_other,
                "memory_type": args.memory_type,
                "tags": tags,
                "namespace": ns,
                "agent_id": "human-cli",
            },
        )
        proxied = _check_daemon_read_response(raw_proxied, "mesh ask")
        if proxied and proxied.get("status") == "ok":
            if proxied.get("namespace_exists") is False:
                ns_missing = True
            else:
                results = proxied.get("results", [])
                other_possible = proxied.get("other_possible_memories", []) or []
                filter_hint = proxied.get("filter_hint")
        else:
            from .memory import Mesh

            sp.update(f"Searching {ns} (loading the embedding model on first use)…")
            try:
                mesh = Mesh(namespace=ns, agent_id="human-cli", create=False, trusted_local=True)
            except NamespaceNotFoundError:
                mesh = None
                ns_missing = True

            if mesh is not None and not args.no_other and hasattr(mesh, "recall_with_context"):
                bundle = mesh.recall_with_context(
                    query=query,
                    n=args.count,
                    min_similarity=args.min_similarity,
                    min_confidence=args.min_confidence,
                    include_stale=not args.no_stale,
                    include_private=not args.no_private,
                    memory_type=args.memory_type,
                    tags=tags,
                )
                results = bundle["results"]
                other_possible = bundle.get("other_possible_memories", [])
                filter_hint = bundle.get("filter_hint")
            elif mesh is not None:
                results = mesh.recall(
                    query=query,
                    n=args.count,
                    min_similarity=args.min_similarity,
                    min_confidence=args.min_confidence,
                    include_stale=not args.no_stale,
                    include_private=not args.no_private,
                    memory_type=args.memory_type,
                    tags=tags,
                )
    if ns_missing:
        _handle_missing_namespace_cli(ns, args.json)
        return
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    if args.json:
        ui.json_out(
            {
                "namespace": ns,
                "query": query,
                "count": len(results),
                "results": results,
                "other_possible_memories": [] if args.no_other else other_possible,
                "filter_hint": filter_hint,
            }
        )
        return

    width = ui.term_width()
    if not results:
        ui.header("Search", ns, f"“{ui.truncate(query, 40)}”")
        print()
        ui.info("No matching memories")
        ui.hint(filter_hint or "Try broader or different words, or browse everything with `mesh inspect`.", stream=sys.stdout)
        print()
    else:
        ui.header("Search", ns, f"{ui.plural(len(results), 'result')}", f"{elapsed_ms} ms")
        print()
        for i, mem in enumerate(results, 1):
            _render_memory_card(i, mem, width)

    if not args.no_other and other_possible:
        print(f"{ui.IND}{ui.style('RELATED', 'muted', 'bold')}")
        for item in other_possible[:4]:
            snip = " ".join(str(item.get("snippet") or item.get("content", "")).split())
            mtype = str(item.get("memory_type", "fact"))
            short_id = str(item.get("id", ""))[:8]
            line = ui.truncate(snip, width - 30)
            print(f"{ui.IND}  {ui.style(ui.sym('bullet') + ' ' + line + '  ' + mtype + '  ' + short_id, 'muted')}")
        print()

    if results and ui.interactive():
        ui.hint("Refine with `mesh update <id>` · remove with `mesh forget <id>`")


def inspect_cmd() -> None:
    from .memory import VALID_MEMORY_TYPES
    from .store import NamespaceNotFoundError

    parser = _parser(
        "inspect",
        "Browse memories in a namespace.",
        [
            ("mesh inspect", "latest 30 memories"),
            ("mesh inspect --type decision --limit 100", "only decisions"),
            ("mesh inspect --tag billing --no-private", "filter by tag"),
        ],
    )
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--type", dest="memory_type", choices=sorted(VALID_MEMORY_TYPES), metavar="TYPE", help=_type_help(VALID_MEMORY_TYPES), default=None)
    parser.add_argument("--tag", default=None)
    parser.add_argument("--no-private", action="store_true", help="Exclude local_only private memories")
    parser.add_argument("--namespace", default=None, metavar="NAME", help="Namespace to use (default: $MESH_NAMESPACE or 'shared')")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args()

    ns = _resolve_namespace(args.namespace)
    query_params: dict[str, Any] = {
        "namespace": ns,
        "limit": args.limit,
        "include_private": "false" if args.no_private else "true",
    }
    if args.memory_type:
        query_params["memory_type"] = args.memory_type
    if args.tag:
        query_params["tag"] = args.tag

    raw_proxied = _daemon_request(f"/inspect?{urllib.parse.urlencode(query_params)}", payload=None, method="GET")
    proxied = _check_daemon_read_response(raw_proxied, "mesh inspect")
    if proxied is not None and proxied.get("status") == "ok":
        if proxied.get("namespace_exists") is False:
            _handle_missing_namespace_cli(ns, args.json)
            return
        mems = proxied.get("memories", [])
    else:
        from .memory import Mesh

        try:
            mesh = Mesh(namespace=ns, agent_id="cli", create=False, trusted_local=True)
        except NamespaceNotFoundError:
            _handle_missing_namespace_cli(ns, args.json)
            return
        mems = mesh.inspect(
            limit=args.limit,
            memory_type=args.memory_type,
            tag=args.tag,
            include_private=not args.no_private,
        )

    if args.json:
        ui.json_out({"namespace": ns, "count": len(mems), "memories": mems})
        return

    filters = [f"type={args.memory_type}" if args.memory_type else "", f"tag={args.tag}" if args.tag else ""]
    filter_s = ", ".join(f for f in filters if f)
    if not mems:
        ui.header("Memories", ns)
        print()
        ui.info("Nothing here yet" if not filter_s else f"No memories match {filter_s}")
        ui.hint('Store one with: mesh add "..."', stream=sys.stdout)
        return

    ui.header("Memories", ns, f"{len(mems)} shown", filter_s)
    print()

    any_flags = any(m.get("local_only") or m.get("is_stale") for m in mems)
    headers = ["ID", "Type", "Conf", "Reads", "Age"] + (["Flags"] if any_flags else []) + ["Memory"]
    aligns = ["l", "l", "r", "r", "l"] + (["l"] if any_flags else []) + ["l"]
    rows: list[list[Any]] = []
    for m in mems:
        mtype = str(m.get("memory_type", "fact"))
        conf = float(m.get("effective_confidence", m.get("confidence", 1.0)))
        flags = [f for f, on in (("private", m.get("local_only")), ("stale", m.get("is_stale"))) if on]
        row: list[Any] = [
            str(m.get("id", ""))[:8],
            (mtype, _TYPE_COLORS.get(mtype, "muted")),
            (f"{conf:.2f}", "warn") if conf < 0.4 else f"{conf:.2f}",
            (str(m.get("access_count", 0)), "muted"),
            (ui.relative_time(m.get("created_at")), "muted"),
        ]
        if any_flags:
            row.append((" ".join(flags), "warn" if "stale" in flags else "accent") if flags else "")
        row.append(" ".join(str(m.get("content", "")).split()))
        rows.append(row)
    ui.table(headers, rows, aligns=aligns, flex=len(headers) - 1)
    print()
    if len(mems) >= args.limit:
        ui.hint(f"Showing the first {args.limit}. Use --limit to see more or `mesh ask` to search.", stream=sys.stdout)


def update_cmd() -> None:
    parser = _parser(
        "update",
        "Edit a memory in place (by full ID or 4+ character short ID).",
        [
            ('mesh update a1b2c3d4 --content "We deploy with Railway"', "change the text"),
            ("mesh update a1b2c3d4 --confidence 0.6 --tags infra", "adjust metadata"),
            ("mesh update a1b2c3d4 --clear-ttl", "make it permanent"),
        ],
    )
    parser.add_argument("memory_id", help="Full UUID or 4+ char short ID")
    parser.add_argument("--content", default=None)
    parser.add_argument("--type", dest="memory_type", default=None)
    parser.add_argument("--confidence", type=float, default=None)
    parser.add_argument("--tags", default=None)
    parser.add_argument("--ttl", type=float, default=None)
    parser.add_argument("--clear-ttl", action="store_true", help="Remove TTL and make memory permanent")
    parser.add_argument("--private", dest="local_only", action="store_true", default=None)
    parser.add_argument("--public", dest="local_only", action="store_false", default=None)
    parser.add_argument("--namespace", default=None, metavar="NAME", help="Namespace to use (default: $MESH_NAMESPACE or 'shared')")
    args = parser.parse_args()

    ns = _resolve_namespace(args.namespace)
    tags = _split_tags(args.tags) if args.tags is not None else None
    should_clear_ttl = bool(args.clear_ttl) or (args.ttl is not None and args.ttl <= 0)

    # Every ingestion surface must run the same guard; otherwise `mesh update`
    # would be a way to bypass prompt-injection/credential handling.
    content = args.content
    local_only = args.local_only
    if content is not None:
        from .guards import guard_ingest

        guard = guard_ingest(content, local_only=bool(args.local_only))
        if guard.rejected:
            ui.fail(guard.reason or "Rejected by the ingestion guard.")
        content = guard.content
        if guard.local_only and not args.local_only:
            local_only = True
            ui.warn(
                "That looks like a credential — the secret was redacted and the memory saved as private"
            )

    changed = [
        name
        for name, on in (
            ("content", args.content is not None),
            ("type", args.memory_type is not None),
            ("confidence", args.confidence is not None),
            ("tags", args.tags is not None),
            ("ttl", args.ttl is not None or should_clear_ttl),
            ("visibility", args.local_only is not None),
        )
        if on
    ]
    if not changed:
        ui.fail(
            "Nothing to update — no changes were specified.",
            "Pass at least one of --content, --type, --confidence, --tags, --ttl, --clear-ttl, --private/--public.",
        )

    def _done(mid: str) -> None:
        print()
        ui.success(f"Updated memory {ui.style(mid[:8], 'bold')} in {ui.style(ns, 'bold')}", "changed: " + ", ".join(changed))

    raw_proxied = _daemon_request(
        "/update",
        payload={
            "memory_id": args.memory_id,
            "content": content,
            "memory_type": args.memory_type,
            "confidence": args.confidence,
            "tags": tags,
            "ttl_days": None if should_clear_ttl else args.ttl,
            "clear_ttl": should_clear_ttl,
            "local_only": local_only,
            "namespace": ns,
            "agent_id": "human-cli",
        },
    )
    proxied = _check_daemon_write_response(raw_proxied, "mesh update")
    if proxied is not None and proxied.get("status") == "ok":
        _done(args.memory_id)
        return

    from .memory import Mesh
    from .store import NamespaceNotFoundError

    try:
        mesh = Mesh(namespace=ns, agent_id="human-cli", create=False, trusted_local=True)
        res = mesh.update(
            memory_id=args.memory_id,
            content=content,
            memory_type=args.memory_type,
            confidence=args.confidence,
            tags=tags,
            ttl_days=None if should_clear_ttl else args.ttl,
            clear_ttl=should_clear_ttl,
            local_only=local_only,
        )
        _done(str(res["memory_id"]))
    except NamespaceNotFoundError:
        _handle_missing_namespace_cli(ns)
        sys.exit(1)
    except ValueError as e:
        ui.fail(str(e), "Find the right ID with `mesh inspect` or `mesh ask`.")


def forget_cmd() -> None:
    parser = _parser(
        "forget",
        "Permanently delete a memory (by full ID or 4+ character short ID).",
        [("mesh forget a1b2c3d4", "asks for confirmation in a terminal"), ("mesh forget a1b2c3d4 -y", "no prompt")],
    )
    parser.add_argument("memory_id", help="Full UUID or 4+ char short ID")
    parser.add_argument("--namespace", default=None, metavar="NAME", help="Namespace to use (default: $MESH_NAMESPACE or 'shared')")
    parser.add_argument("--yes", "-y", action="store_true", help="Don't ask for confirmation")
    args = parser.parse_args()

    ns = _resolve_namespace(args.namespace)

    # Only prompt when a human is at the keyboard; scripts and agents are never blocked.
    if not args.yes and ui.interactive():
        if not ui.confirm(f"Permanently forget memory {args.memory_id[:8]} from '{ns}'?"):
            ui.info("Cancelled — nothing was deleted")
            return

    def _deleted() -> None:
        print()
        ui.success(f"Forgot memory {ui.style(args.memory_id[:8], 'bold')} from {ui.style(ns, 'bold')}")

    def _not_found() -> None:
        ui.fail(f"No memory '{args.memory_id}' in namespace '{ns}'", "Find the right ID with `mesh inspect` or `mesh ask`.")

    raw_proxied = _daemon_request("/forget", payload={"memory_id": args.memory_id, "namespace": ns, "agent_id": "human-cli"})
    proxied = _check_daemon_write_response(raw_proxied, "mesh forget")
    if proxied is not None:
        if proxied.get("status") == "ok":
            _deleted()
            return
        if proxied.get("status") == "not_found":
            _not_found()

    from .memory import Mesh
    from .store import NamespaceNotFoundError

    try:
        mesh = Mesh(namespace=ns, agent_id="human-cli", create=False, trusted_local=True)
        deleted = mesh.forget(args.memory_id)
    except NamespaceNotFoundError:
        _handle_missing_namespace_cli(ns)
        sys.exit(1)
    except ValueError as e:
        ui.fail(str(e))
    if deleted:
        _deleted()
    else:
        _not_found()


# ─────────────────────────────────────────────────────────────────────────────
# conflicts / patterns
# ─────────────────────────────────────────────────────────────────────────────


def conflicts_cmd() -> None:
    from .store import NamespaceNotFoundError

    parser = _parser("conflicts", "Review and resolve contradictions between memories.")
    parser.add_argument("--resolve", default=None, help="Mark a contradiction resolved (ID or short ID)")
    parser.add_argument("--namespace", default=None, metavar="NAME", help="Namespace to use (default: $MESH_NAMESPACE or 'shared')")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args()

    ns = _resolve_namespace(args.namespace)
    if args.resolve:
        raw_proxied = _daemon_request(
            "/contradictions/resolve",
            payload={"contradiction_id": args.resolve, "namespace": ns},
        )
        proxied = _check_daemon_write_response(raw_proxied, "mesh conflicts --resolve")
        if proxied is not None and proxied.get("status") == "ok":
            print()
            ui.success(f"Resolved conflict {ui.style(str(args.resolve)[:8], 'bold')}")
            return
        from .memory import Mesh

        try:
            mesh = Mesh(namespace=ns, agent_id="cli", create=False, trusted_local=True)
            mesh.resolve_contradiction(args.resolve)
            print()
            ui.success(f"Resolved conflict {ui.style(str(args.resolve)[:8], 'bold')}")
        except NamespaceNotFoundError:
            _handle_missing_namespace_cli(ns)
            sys.exit(1)
        except ValueError as e:
            ui.fail(str(e), "List open conflicts with `mesh conflicts`.")
        return

    raw_list = _daemon_request(
        f"/contradictions?{urllib.parse.urlencode({'namespace': ns, 'resolved': 'false'})}",
        payload=None,
        method="GET",
    )
    proxied_list = _check_daemon_read_response(raw_list, "mesh conflicts")
    if proxied_list is not None and proxied_list.get("status") == "ok":
        if proxied_list.get("namespace_exists") is False:
            _handle_missing_namespace_cli(ns, args.json)
            return
        conflicts = proxied_list.get("contradictions", [])
    else:
        from .memory import Mesh

        try:
            mesh = Mesh(namespace=ns, agent_id="cli", create=False, trusted_local=True)
        except NamespaceNotFoundError:
            _handle_missing_namespace_cli(ns, args.json)
            return
        conflicts = mesh.contradictions(resolved=False)

    if args.json:
        ui.json_out({"namespace": ns, "count": len(conflicts), "conflicts": conflicts})
        return

    if not conflicts:
        ui.header("Conflicts", ns)
        print()
        ui.success("No unresolved conflicts")
        return

    ui.header("Conflicts", ns, f"{len(conflicts)} unresolved")
    width = ui.term_width()
    for c in conflicts:
        cid = str(c.get("id", ""))[:8]
        sim = float(c.get("similarity", 0) or 0)
        print()
        print(f"{ui.IND}{ui.style(cid, 'bold')}   {ui.style('overlap', 'muted')} {ui.meter(sim)} {round(sim * 100)}%")
        for label, key, color in (("existing", "existing_content", "muted"), ("new", "new_content", "warn")):
            lines = ui.wrap(str(c.get(key, "")), width - 14, max_lines=4)
            print(f"{ui.IND}  {ui.style(label.ljust(9), color)}{lines[0]}")
            for extra in lines[1:]:
                print(f"{ui.IND}  {' ' * 9}{extra}")
    print()
    ui.next_steps(
        [
            ("mesh forget <id>", "delete the memory that's wrong"),
            ("mesh conflicts --resolve <id>", "keep both and dismiss the warning"),
        ]
    )


def patterns_cmd() -> None:
    from .store import NamespaceNotFoundError

    parser = _parser("patterns", "Surface what you search for most and where memory is thin.")
    parser.add_argument("--namespace", default=None, metavar="NAME", help="Namespace to use (default: $MESH_NAMESPACE or 'shared')")
    parser.add_argument(
        "--include-private",
        action="store_true",
        default=False,
        help="Include private (local_only) memories and queries in pattern analysis",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args()
    ns = _resolve_namespace(args.namespace)
    query_params: dict[str, Any] = {"namespace": ns}
    if args.include_private:
        query_params["include_private"] = "true"
    raw_proxied = _daemon_request(f"/patterns?{urllib.parse.urlencode(query_params)}", payload=None, method="GET")
    proxied = _check_daemon_read_response(raw_proxied, "mesh patterns")
    if proxied is not None and proxied.get("status") == "ok":
        if proxied.get("namespace_exists") is False:
            _handle_missing_namespace_cli(ns, args.json)
            return
        p = proxied.get("patterns", {})
    else:
        from .memory import Mesh

        try:
            mesh = Mesh(namespace=ns, agent_id="cli", create=False, trusted_local=True)
        except NamespaceNotFoundError:
            _handle_missing_namespace_cli(ns, args.json)
            return
        p = mesh.patterns(include_private=args.include_private)

    if args.json:
        ui.json_out({"namespace": ns, "patterns": p})
        return

    ui.header("Patterns", ns)
    topics = p.get("top_topics", [])[:5]
    gaps = p.get("memory_gaps", [])[:5]
    if not topics and not gaps:
        print()
        ui.info("No search history yet", "Patterns appear after you've run a few searches with `mesh ask`.")
        return

    if topics:
        ui.section("Most searched")
        print()
        peak = max(int(t.get("query_count", 1) or 1) for t in topics)
        rows = [
            [
                (str(t["query_text"]), ""),
                (str(t["query_count"]), "bold"),
                ui.style(ui.sym("meter_on") * max(1, round(8 * int(t["query_count"]) / peak)), "info"),
            ]
            for t in topics
        ]
        ui.table(["Query", "Times", ""], rows, aligns=["l", "r", "l"], flex=0)
    if gaps:
        ui.section("Knowledge gaps")
        ui.hint("Searched often, matched poorly — good candidates to document.", stream=sys.stdout)
        print()
        rows = [
            [
                str(g["query_text"]),
                (str(g["query_count"]), "bold"),
                f"{ui.meter(float(g['avg_similarity']))} {round(float(g['avg_similarity']) * 100):>3}%",
            ]
            for g in gaps
        ]
        ui.table(["Query", "Times", "Avg match"], rows, aligns=["l", "r", "l"], flex=0)
        ui.next_steps([('mesh add "..."', "fill a gap")])


# ─────────────────────────────────────────────────────────────────────────────
# doctor
# ─────────────────────────────────────────────────────────────────────────────


def doctor_cmd() -> None:
    """Diagnostic check for SQLite WAL/FTS5, permissions, ONNX SHA-256, degraded state, daemon, and MCP."""
    from .audit import ensure_secure_mesh_dir
    from .daemon import get_daemon_health, verify_daemon_identity
    from .memory import (
        _ALLOWED_MODEL_SHA256,
        _ALLOWED_TOKENIZER_SHA256,
        _has_local_onnx_pair,
        _verify_sha256,
        get_engine_status,
        get_model_status,
    )
    from .namespaces import list_namespaces

    ui.header("Doctor", f"mesh v{VERSION}", f"{platform.system()} {platform.machine()}", f"Python {platform.python_version()}")
    print()

    counts = {"warn": 0, "fail": 0}
    label_w = 18
    glyphs = {
        "ok": ("ok", "brand"),
        "warn": ("warn", "warn"),
        "fail": ("fail", "error"),
        "skip": ("pending", "muted"),
    }

    def row(status: str, label: str, detail: str, fix: Optional[str] = None) -> None:
        if status in counts:
            counts[status] += 1
        g, color = glyphs[status]
        detail_styled = ui.style(detail, "muted") if status in ("ok", "skip") else ui.style(detail, color)
        print(f"{ui.IND}{ui.style(ui.sym(g), color, 'bold')} {label.ljust(label_w)}{detail_styled}")
        if fix:
            print(f"{ui.IND}  {' ' * label_w}{ui.style(ui.sym('arrow'), 'muted')} {ui.style(fix, 'info')}")

    # 1. Directory & permissions
    try:
        mesh_dir = ensure_secure_mesh_dir()
        if os.name != "nt" and mesh_dir.exists():
            mode = stat.S_IMODE(mesh_dir.stat().st_mode)
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                row("warn", "Data directory", f"{ui.short_path(mesh_dir)}  (mode {oct(mode)}, expected 0o700)", f"chmod 700 {mesh_dir}")
            else:
                row("ok", "Data directory", f"{ui.short_path(mesh_dir)}  (private, mode 0700)")
        else:
            row("ok", "Data directory", ui.short_path(mesh_dir))
    except Exception as e:
        row("fail", "Data directory", str(e))

    # 2. SQLite WAL & FTS5
    try:
        with sqlite3.connect(":memory:") as conn:
            conn.execute("CREATE VIRTUAL TABLE _doc_fts USING fts5(content, tokenize='porter unicode61')")
            conn.execute("INSERT INTO _doc_fts(content) VALUES ('mesh doctor fts5 check')")
            r = conn.execute("SELECT COUNT(*) FROM _doc_fts WHERE _doc_fts MATCH 'mesh'").fetchone()
            assert r and int(r[0]) == 1
        row("ok", "SQLite + FTS5", f"SQLite {sqlite3.sqlite_version} with Porter stemming")
    except Exception as e:
        row("fail", "SQLite + FTS5", str(e), "Install a Python build whose SQLite includes FTS5")

    # 3. Daemon
    ident = verify_daemon_identity(timeout=0.4)
    if ident and ident.get("identity_verified"):
        row("ok", "Daemon", f"running and verified  (pid {ident.get('pid')})")
    else:
        health = get_daemon_health(timeout=0.3)
        if health:
            row(
                "warn",
                "Daemon",
                "something is listening on the Mesh port but failed the identity check",
                "Stop that process or set MESH_HTTP_PORT to another port",
            )
        else:
            row("skip", "Daemon", "not running (optional — it makes searches faster)", "mesh daemon start")

    # 4. ONNX model, SHA-256, degraded state
    pair = _has_local_onnx_pair()
    eng = get_engine_status()
    m_status = get_model_status()
    backend_str = eng.get("backend", "unloaded")
    model_tag_str = eng.get("model_tag", "hash-384")
    last_err = eng.get("last_error") or m_status.get("last_error")

    if pair is not None:
        model_ok = _verify_sha256(pair[0], _ALLOWED_MODEL_SHA256, "MESH_MODEL_SHA256", source_label=str(pair[0]))
        tok_ok = _verify_sha256(pair[1], _ALLOWED_TOKENIZER_SHA256, "MESH_TOKENIZER_SHA256", source_label=str(pair[1]))
        if model_ok and tok_ok:
            row("ok", "Embedding model", "downloaded, SHA-256 verified")
        else:
            row("fail", "Embedding model", "checksum mismatch — model or tokenizer file is corrupted", "mesh setup --yes   (re-downloads)")
    else:
        row("warn", "Embedding model", "not downloaded — using the built-in hash-384 fallback (lower search quality)", "mesh setup")

    row("skip", "Embedding engine", f"{backend_str}  {ui.sym('dot')}  {model_tag_str}" + ("  (fallback active)" if eng.get("embedding_degraded") else ""))
    if last_err:
        # When the model simply isn't installed the row above already explains it; only a
        # failure *despite* an installed model is a separate problem worth a warning.
        row("warn" if pair is not None else "skip", "Last error", str(last_err))

    # 5. MCP config discovery
    home = Path.home()
    if os.name == "nt":
        appdata_raw = os.environ.get("APPDATA", "").strip()
        claude_mcp = (Path(appdata_raw) / "Claude" / "claude_desktop_config.json") if appdata_raw else (home / "claude_desktop_config.json")
    elif sys.platform == "darwin":
        claude_mcp = home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    else:
        config_home = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
        claude_mcp = config_home / "Claude" / "claude_desktop_config.json"
    windsurf_mcp = home / ".codeium" / "windsurf" / "mcp_config.json"

    mcp_configs = [Path.cwd() / ".cursor" / "mcp.json", home / ".cursor" / "mcp.json", claude_mcp, windsurf_mcp]
    found_mcp = [p for p in mcp_configs if p.exists()]
    if found_mcp:
        row("ok", "MCP configs", f"{len(found_mcp)} found  ({ui.short_path(found_mcp[0])})")
    else:
        row("skip", "MCP configs", "none found in the default Claude / Cursor / Windsurf locations")

    # 6. Namespaces
    ns_list = list_namespaces()
    if not ns_list:
        row("skip", "Namespaces", "none yet", 'mesh add "your first memory"')
    else:
        preview = ", ".join(n["namespace"] for n in ns_list[:3]) + (f", +{len(ns_list) - 3} more" if len(ns_list) > 3 else "")
        row("ok", "Namespaces", f"{len(ns_list)} found  ({preview})")
        for n_info in ns_list:
            ns_name = n_info["namespace"]
            pad = f"{ui.IND}  {' ' * label_w}"
            if "error" in n_info:
                counts["warn"] += 1
                print(f"{pad}{ui.style(ui.sym('warn'), 'warn')} {ns_name}: {ui.style(str(n_info['error']), 'error')}")
                continue
            h_cnt, r_cnt = _namespace_degraded_counts(ns_name)
            if h_cnt > 0 or r_cnt > 0:
                bits = []
                if h_cnt:
                    bits.append(f"{h_cnt} on fallback embeddings")
                if r_cnt:
                    bits.append(f"{r_cnt} need re-embedding")
                print(f"{pad}{ui.style(ui.sym('warn'), 'warn')} {ns_name}: {n_info['memory_count']} memories, {ui.style(', '.join(bits), 'warn')}")

    # Summary
    print()
    failures, warnings = counts["fail"], counts["warn"]
    if failures:
        ui.error(f"{ui.plural(failures, 'problem')} found" + (f", {ui.plural(warnings, 'warning')}" if warnings else ""))
        sys.exit(1)
    elif warnings:
        ui.warn(f"Everything works, with {ui.plural(warnings, 'warning')}", stdout=True)
    else:
        ui.success("All checks passed")


# ─────────────────────────────────────────────────────────────────────────────
# shell / completion
# ─────────────────────────────────────────────────────────────────────────────

_SHELL_FORWARD_NS = {
    # Deliberately excludes `import`: the export file normally names its own
    # namespace, and injecting the shell's active `--namespace` silently
    # redirected every restore into the wrong namespace.
    "add", "ask", "inspect", "update", "forget", "status", "why", "context",
    "conflicts", "patterns", "digest", "export", "capture",
}


def shell_cmd() -> None:
    """Interactive REPL with shlex argument parsing and active namespace tracking."""
    parser = _parser("shell", "Interactive Mesh shell.")
    parser.add_argument("--namespace", default=None, metavar="NAME", help="Namespace to use (default: $MESH_NAMESPACE or 'shared')")
    args, _ = parser.parse_known_args()
    ns = _resolve_namespace(args.namespace)

    ui.header("Mesh shell", f"namespace {ns}")
    ui.hint(
        f"{ui.style('help', 'info')} for commands  {ui.sym('dot')}  {ui.style('ns <name>', 'info')} to switch namespace  {ui.sym('dot')}  {ui.style('exit', 'info')} to leave",
        stream=sys.stdout,
    )
    print()

    cmd_map = _get_command_mapping()
    while True:
        try:
            prompt = f"{ui.style('mesh', 'brand', 'bold')}{ui.style(':' + ns, 'muted')} {ui.style(ui.sym('prompt'), 'brand', 'bold')} "
            line = input(ui.readline_safe(prompt)).strip()
        except (KeyboardInterrupt, EOFError):
            print()
            break
        if not line:
            continue
        if line.lower() in ("exit", "quit", "q"):
            break

        try:
            parts = shlex.split(line)
        except ValueError as e:
            ui.error(f"Couldn't parse that: {e}")
            continue
        if not parts:
            continue

        cmd = parts[0].lower()
        cmd_args = parts[1:]

        if cmd == "clear":
            print("\033[H\033[J", end="")
            continue
        if cmd in ("ns", "use"):
            if not cmd_args:
                ui.info(f"Active namespace: {ui.style(ns, 'bold')}")
            else:
                try:
                    ns = validate_namespace(cmd_args[0])
                    ui.success(f"Switched to namespace {ui.style(ns, 'bold')}")
                except ValueError as e:
                    ui.error(str(e))
            print()
            continue
        if cmd in ("shell", "repl", "i"):
            ui.info("You're already inside the Mesh shell")
            print()
            continue
        if cmd in ("version", "-v", "--version"):
            print(f"mesh {VERSION}")
            continue

        resolved_cmd = _COMMAND_ALIASES.get(cmd, cmd)
        handler = cmd_map.get(resolved_cmd)
        if handler is None:
            suggestions = _suggest_did_you_mean(cmd, list(cmd_map.keys()))
            ui.error(f"Unknown command '{cmd}'", ("Did you mean: " + ", ".join(suggestions) + "?") if suggestions else "Type 'help' to see all commands.")
            print()
            continue

        if resolved_cmd in _SHELL_FORWARD_NS and not any(a == "--namespace" or a.startswith("--namespace=") for a in cmd_args):
            cmd_args = [*cmd_args, "--namespace", ns]

        prev_argv = sys.argv
        sys.argv = [f"mesh-{resolved_cmd}", *cmd_args]
        try:
            handler()
        except SystemExit:
            pass
        except KeyboardInterrupt:
            print()
        finally:
            sys.argv = prev_argv
        print()


_COMMAND_LIST = (
    "add ask inspect update forget status why context setup install-skills daemon server "
    "dashboard http namespaces export import capture digest conflicts patterns doctor shell completion help version"
)

_BASH_COMPLETION_SCRIPT = """# Added by Mesh
_mesh_completion() {
    local cur prev words cword
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    local commands="%(commands)s"
    local types="fact preference context result instruction process decision error general"
    if [[ ${COMP_CWORD} -eq 1 ]]; then
        COMPREPLY=( $(compgen -W "${commands}" -- "${cur}") )
        return 0
    fi
    case "${COMP_WORDS[1]}" in
        ask|search|find|add|learn|inspect|context)
            if [[ "${prev}" == "--type" ]]; then
                COMPREPLY=( $(compgen -W "${types}" -- "${cur}") )
                return 0
            fi
            if [[ "${prev}" == "--namespace" ]]; then
                local ns_list=$(python3 -c "from mesh.namespaces import list_namespaces; print(' '.join(n['namespace'] for n in list_namespaces()))" 2>/dev/null)
                COMPREPLY=( $(compgen -W "${ns_list}" -- "${cur}") )
                return 0
            fi
        ;;
    esac
}
complete -F _mesh_completion mesh mesh-add mesh-ask mesh-inspect
""" % {"commands": _COMMAND_LIST}

_ZSH_COMPLETION_SCRIPT = """# Added by Mesh
#compdef mesh
_mesh_zsh_completion() {
    local -a commands types
    commands=(%(commands)s)
    types=(fact preference context result instruction process decision error general)
    if (( CURRENT == 2 )); then
        _describe 'command' commands
        return
    fi
    if [[ "${words[CURRENT-1]}" == "--type" ]]; then
        _describe 'memory type' types
        return
    fi
}
if ! (( $+functions[compdef] )); then
    autoload -Uz compinit && compinit -i
fi
compdef _mesh_zsh_completion mesh
""" % {"commands": _COMMAND_LIST}

_FISH_COMPLETION_SCRIPT = """# Added by Mesh
complete -c mesh -f
complete -c mesh -n "__fish_use_subcommand" -a "%(commands)s"
complete -c mesh -l type -a "fact preference context result instruction process decision error general"
""" % {"commands": _COMMAND_LIST}

_POWERSHELL_COMPLETION_SCRIPT = """# Added by Mesh
Register-ArgumentCompleter -Native -CommandName mesh -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)
    $commands = @(%(ps_commands)s)
    $commands | Where-Object { $_ -like "$wordToComplete*" } | ForEach-Object {
        [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
    }
}
""" % {"ps_commands": ",".join(f"'{c}'" for c in _COMMAND_LIST.split())}


def completion_cmd() -> None:
    parser = _parser(
        "completion",
        "Generate or install shell Tab-completion.",
        [("mesh completion zsh --install", "add to ~/.zshrc"), ("mesh completion bash > ~/.mesh-completion.sh", "print the script")],
    )
    parser.add_argument("shell", nargs="?", choices=["bash", "zsh", "fish", "powershell"], default="zsh")
    parser.add_argument("--install", action="store_true", help="Append completion to your shell rc file (idempotent)")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip interactive y/n permission prompt")
    args = parser.parse_args()

    scripts = {
        "bash": _BASH_COMPLETION_SCRIPT,
        "zsh": _ZSH_COMPLETION_SCRIPT,
        "fish": _FISH_COMPLETION_SCRIPT,
        "powershell": _POWERSHELL_COMPLETION_SCRIPT,
    }
    script = scripts[args.shell]

    if not args.install:
        print(script)
        return

    from .daemon import request_user_permission

    home = Path.home()
    if args.shell == "zsh":
        rc_file = home / ".zshrc"
    elif args.shell == "bash":
        rc_file = home / ".bashrc"
    elif args.shell == "fish":
        rc_file = home / ".config" / "fish" / "completions" / "mesh.fish"
    else:
        rc_file = home / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1"

    ui.header("Shell completion", args.shell)
    print()
    if rc_file.exists():
        try:
            if "# Added by Mesh" in rc_file.read_text(encoding="utf-8", errors="ignore"):
                ui.success("Completion is already installed", _tilde(str(rc_file)))
                return
        except OSError:
            pass

    if not request_user_permission(f"append Mesh shell completion to {_tilde(str(rc_file))}", assume_yes=args.yes):
        ui.step("Cancelled — nothing was changed")
        return

    rc_file.parent.mkdir(parents=True, exist_ok=True)
    with open(rc_file, "a", encoding="utf-8") as f:
        f.write(f"\n{script.strip()}\n")
    ui.success(f"Installed {args.shell} completion", _tilde(str(rc_file)))
    reload_cmd = {"zsh": "source ~/.zshrc", "bash": "source ~/.bashrc", "fish": "open a new terminal"}.get(args.shell, "restart PowerShell")
    ui.hint(f"Activate it now: {reload_cmd}", stream=sys.stdout)


# ─────────────────────────────────────────────────────────────────────────────
# export / import / namespaces
# ─────────────────────────────────────────────────────────────────────────────


def export_cmd() -> None:
    from .store import NamespaceNotFoundError

    parser = _parser(
        "export",
        "Back up a namespace to a JSON file (private memories are never exported).",
        [("mesh export backup.json", "export the active namespace"), ("mesh export work.json --namespace work --no-embeddings", "smaller file")],
    )
    parser.add_argument("output", help="Destination .json path")
    parser.add_argument("--namespace", default=None, metavar="NAME", help="Namespace to use (default: $MESH_NAMESPACE or 'shared')")
    parser.add_argument("--no-embeddings", action="store_true", help="Omit vectors (smaller, re-embedded on import)")
    args = parser.parse_args()
    ns = _resolve_namespace(args.namespace)
    from .io import export_namespace

    try:
        with ui.spinner(f"Exporting {ns}…"):
            export_namespace(namespace=ns, output_path=args.output, include_embeddings=not args.no_embeddings)
    except NamespaceNotFoundError:
        _handle_missing_namespace_cli(ns)
        sys.exit(1)
    except (OSError, ValueError) as e:
        ui.fail(str(e))


def import_cmd() -> None:
    parser = _parser(
        "import",
        "Restore memories from an exported JSON file.",
        [("mesh import backup.json --dry-run", "preview first"), ("mesh import backup.json --namespace restored", "import into another namespace")],
    )
    parser.add_argument("input", help="Exported .json file")
    parser.add_argument("--namespace", default=None, metavar="NAME", help="Target namespace (default: the one in the file)")
    parser.add_argument("--overwrite", action="store_true", help="Replace memories that already exist")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without writing")
    args = parser.parse_args()
    from .io import import_namespace

    target_ns = _resolve_namespace(args.namespace) if args.namespace else None
    try:
        with ui.spinner("Importing…"):
            import_namespace(
                input_path=args.input,
                target_namespace=target_ns,
                merge_strategy="overwrite" if args.overwrite else "skip_existing",
                dry_run=args.dry_run,
            )
    except (FileNotFoundError, ValueError) as e:
        ui.fail(str(e))


def namespaces_cmd() -> None:
    parser = _parser(
        "namespaces",
        "List, inspect, rename or delete namespaces.",
        [
            ("mesh namespaces", "list all"),
            ("mesh namespaces stats work", "details for one"),
            ("mesh namespaces rename old new", "rename"),
            ("mesh namespaces delete scratch", "delete (asks first)"),
        ],
    )
    parser.add_argument("command", nargs="?", default="list", choices=["list", "delete", "rename", "stats"])
    parser.add_argument("args", nargs="*")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt on delete")
    parser.add_argument("--json", action="store_true", help="Machine-readable output (list, stats)")
    args = parser.parse_args()
    from .namespaces import delete_namespace, list_namespaces, namespace_stats, rename_namespace

    if args.command == "list":
        items = list_namespaces()
        if args.json:
            ui.json_out(items)
            return
        ui.header("Namespaces", ui.plural(len(items), "namespace"))
        if not items:
            print()
            ui.info("No namespaces yet", 'Store a memory to create one: mesh add "..." --namespace myproject')
            return
        print()
        active = (os.environ.get("MESH_NAMESPACE") or "shared").strip() or "shared"
        rows = []
        for ns in items:
            mark = (ui.sym("info"), "brand") if ns["namespace"] == active else " "
            if "error" in ns:
                rows.append([mark, (ns["namespace"], "bold"), "-", "-", ("unreadable: " + ui.truncate(str(ns["error"]), 40), "error")])
            else:
                rows.append(
                    [
                        mark,
                        (ns["namespace"], "bold"),
                        str(ns["memory_count"]),
                        (str(ns["local_only_count"]), "muted"),
                        (ui.relative_time(ns.get("last_updated")) or str(ns.get("last_updated", "")), "muted"),
                    ]
                )
        ui.table(["", "Namespace", "Memories", "Private", "Updated"], rows, aligns=["l", "l", "r", "r", "l"])
        print()
        ui.hint(f"● marks the active namespace ({active}). Switch with MESH_NAMESPACE or --namespace.", stream=sys.stdout)

    elif args.command == "stats":
        target = _resolve_namespace(args.args[0] if args.args else None)
        try:
            st = namespace_stats(target)
        except ValueError:
            _handle_missing_namespace_cli(target, args.json)
            sys.exit(1)
        if args.json:
            ui.json_out({"namespace": target, **st})
            return
        ui.header("Namespace", target)
        print()
        stale = int(st["stale_count"])
        ui.kv(
            [
                ("Memories", ui.style(str(st["memory_count"]), "bold")),
                ("Private", f"{st['local_only_count']}  {ui.style('(never exported)', 'muted')}"),
                ("Exportable", str(st["exportable_count"])),
                ("Stale", ui.style(str(stale), "warn") if stale else "0"),
                ("Avg confidence", f"{ui.meter(float(st['avg_confidence']))} {st['avg_confidence']:.2f}"),
                ("Searches", str(st.get("total_searches", 0))),
                ("Memory hits", str(st["total_recalls"])),
            ]
        )
        types = st.get("type_breakdown") or {}
        if types:
            ui.section("By type")
            peak = max(types.values()) or 1
            kw = max(len(t) for t in types) + 2
            for t, c in sorted(types.items(), key=lambda kv_: -kv_[1]):
                bar = ui.style(ui.sym("meter_on") * max(1, round(16 * c / peak)), _TYPE_COLORS.get(t, "muted"))
                print(f"{ui.IND}  {t.ljust(kw)}{bar} {c}")
        if stale:
            ui.next_steps([("mesh digest", "review stale memories")])

    elif args.command == "delete":
        if not args.args:
            ui.fail("Which namespace should be deleted?", "Usage: mesh namespaces delete <name>")
        target = _resolve_namespace(args.args[0])
        from .daemon import request_user_permission

        if not request_user_permission(f"permanently delete namespace '{target}' and all of its memories", assume_yes=args.yes):
            ui.step("Cancelled — nothing was deleted")
            return
        try:
            removed = delete_namespace(target)
            print()
            ui.success(f"Deleted namespace {ui.style(target, 'bold')}", f"{ui.plural(removed, 'memory', 'memories')} removed")
        except ValueError as e:
            ui.fail(str(e))
        except (RuntimeError, OSError) as e:
            ui.fail(
                "Another process still has this database open",
                f"Run `mesh daemon stop`, close agents using '{target}', then retry.  ({e})",
            )

    elif args.command == "rename":
        if len(args.args) < 2:
            ui.fail("Both the old and new names are needed", "Usage: mesh namespaces rename <old> <new>")
        old_ns = _resolve_namespace(args.args[0])
        new_ns = _resolve_namespace(args.args[1])
        try:
            moved = rename_namespace(old_ns, new_ns)
            print()
            ui.success(f"Renamed {ui.style(old_ns, 'bold')} {ui.sym('arrow')} {ui.style(new_ns, 'bold')}", f"{ui.plural(moved, 'memory', 'memories')} moved")
        except (ValueError, RuntimeError, OSError) as e:
            ui.fail(str(e))


# ─────────────────────────────────────────────────────────────────────────────
# context / why / status
# ─────────────────────────────────────────────────────────────────────────────


def context_cmd() -> None:
    """Prints a raw prompt block to stdout — deliberately unstyled so it can be piped or pasted."""
    from .memory import VALID_MEMORY_TYPES

    parser = _parser(
        "context",
        "Print memories as a ready-to-paste system-prompt block (plain output, safe to pipe).",
        [
            ('mesh context --query "billing"', "memories relevant to a topic"),
            ("mesh context --format xml -n 20", "XML, 20 memories"),
        ],
    )
    parser.add_argument("--query", default=None)
    parser.add_argument("--namespace", default=None, metavar="NAME", help="Namespace to use (default: $MESH_NAMESPACE or 'shared')")
    parser.add_argument("--count", "-n", "--n", dest="count", type=int, default=10)
    parser.add_argument("--format", choices=["markdown", "plain", "xml", "json"], default="markdown")
    parser.add_argument("--min-confidence", type=float, default=0.3)
    parser.add_argument("--type", dest="memory_type", choices=sorted(VALID_MEMORY_TYPES), metavar="TYPE", help=_type_help(VALID_MEMORY_TYPES), default=None)
    parser.add_argument("--tags", default="")
    parser.add_argument("--include-private", action="store_true", help="Include local_only private memories")
    args = parser.parse_args()

    ns = _resolve_namespace(args.namespace)
    tags = _split_tags(args.tags) or None

    raw_proxied = _daemon_request(
        "/context",
        payload={
            "query": args.query,
            "namespace": ns,
            "count": args.count,
            "format": args.format,
            "min_confidence": args.min_confidence,
            "memory_type": args.memory_type,
            "tags": tags,
            "include_private": args.include_private,
        },
    )
    proxied = _check_daemon_read_response(raw_proxied, "mesh context")
    if proxied is not None and proxied.get("status") == "ok":
        if proxied.get("namespace_exists") is False:
            _handle_missing_namespace_cli(ns)
            return
        print(proxied.get("context", ""))
        return

    from .context_builder import build_context_bundle

    bundle = build_context_bundle(
        query=args.query,
        namespace=ns,
        count=args.count,
        format=args.format,
        min_confidence=args.min_confidence,
        memory_type=args.memory_type,
        tags=tags,
        include_private=args.include_private,
    )
    if bundle.get("namespace_exists") is False:
        _handle_missing_namespace_cli(ns)
        return
    if not bundle.get("context"):
        ui.note(f"No memories matched in '{ns}' — the context block is empty.")
    print(bundle.get("context", ""))


def why_cmd() -> None:
    parser = _parser(
        "why",
        "Show the audit trail: who read or changed what, and when.",
        [("mesh why", "latest 20 events"), ("mesh why --action write --limit 50", "only writes")],
    )
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--namespace", default=None, metavar="NAME", help="Namespace to use (default: $MESH_NAMESPACE or 'shared')")
    parser.add_argument("--action", choices=["read", "write", "update", "forget", "delete", "all"], default="all")
    args = parser.parse_args()
    ns = _resolve_namespace(args.namespace)

    from .audit import _get_audit_db_path, ensure_secure_mesh_dir, get_audit_log

    db_path = ensure_secure_mesh_dir().resolve() / f"mesh_{ns}.db"
    audit_path = _get_audit_db_path(ns)
    if not db_path.exists() and not audit_path.exists():
        _handle_missing_namespace_cli(ns)
        return

    entries = get_audit_log(namespace=ns, limit=args.limit, action_filter=args.action)
    ui.header("Audit trail", ns, f"{len(entries)} event{'s' if len(entries) != 1 else ''}", "" if args.action == "all" else f"action={args.action}")
    if not entries:
        print()
        ui.info("No audit entries yet")
        return
    print()
    rows = []
    for e in entries:
        ts = str(e.get("timestamp", ""))[:16].replace("T", " ")
        action = str(e.get("action", "unknown"))
        detail = e.get("query") if action == "recall" else e.get("content", "")
        rows.append(
            [
                (ts, "muted"),
                (_ACTION_LABELS.get(action, action), _ACTION_COLORS.get(action, "muted"), "bold"),
                str(e.get("agent_id") or "unknown"),
                " ".join(str(detail or "").split()),
            ]
        )
    ui.table(["When (UTC)", "Action", "Agent", "Detail"], rows, flex=3)


def status_cmd() -> None:
    parser = _parser("status", "Show the health of Mesh and a namespace at a glance.")
    parser.add_argument("--namespace", default=None, metavar="NAME", help="Namespace to use (default: $MESH_NAMESPACE or 'shared')")
    parser.add_argument("--check-updates", action="store_true", help="Check PyPI for a newer Mesh version")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args()
    check_for_updates(force=args.check_updates, blocking=bool(args.check_updates))

    from .memory import get_engine_status, get_model_status
    from .namespaces import namespace_stats

    ns = _resolve_namespace(args.namespace)
    eng = get_engine_status()
    m_status = get_model_status()
    backend_str = eng.get("backend", "unloaded")
    model_tag = eng.get("model_tag", "hash-384")
    last_err = eng.get("last_error") or m_status.get("last_error")
    degraded = bool(eng.get("embedding_degraded"))
    port = _daemon_port_open()

    try:
        st: Optional[dict[str, Any]] = namespace_stats(ns)
        h_cnt, r_cnt = _namespace_degraded_counts(ns)
    except ValueError:
        st, h_cnt, r_cnt = None, 0, 0

    if args.json:
        ui.json_out(
            {
                "version": VERSION,
                "namespace": ns,
                "namespace_exists": st is not None,
                "stats": st,
                "embedding": {"backend": backend_str, "model_tag": model_tag, "degraded": degraded, "last_error": last_err},
                "pending_reembed": {"hash_384": h_cnt, "needs_reembed": r_cnt},
                "daemon_port_open": port,
            }
        )
        return

    ui.header("Status", ns, f"v{VERSION}")
    print()
    rows: list[tuple[str, Any]] = []
    if st is not None:
        bits = [ui.style(str(st["memory_count"]), "bold") + " total"]
        if st["local_only_count"]:
            bits.append(f"{st['local_only_count']} private")
        if st["stale_count"]:
            bits.append(ui.style(f"{st['stale_count']} stale", "warn"))
        rows.append(("Memories", _muted_join(bits)))
        rows.append(("Searches", str(st.get("total_searches", 0))))
    else:
        rows.append(("Memories", ui.style("namespace doesn't exist yet", "muted")))
    rows.append(
        (
            "Embeddings",
            f"{backend_str} {ui.sym('dot')} {model_tag}" + (ui.style("  (fallback)", "warn") if degraded else ""),
        )
    )
    rows.append(("Daemon", ui.style(f"running on :{port}", "brand") if port else ui.style("not running", "muted")))
    if h_cnt > 0 or r_cnt > 0:
        rows.append(("Pending", ui.style(f"{h_cnt} on fallback embeddings, {r_cnt} need re-embedding", "warn")))
    ui.kv(rows)

    if st is None:
        print()
        _handle_missing_namespace_cli(ns)
    if degraded:
        print()
        ui.warn("Search quality is reduced — the built-in fallback embedding is active", "Run `mesh setup` to enable the ONNX model.", stdout=True)
    if last_err:
        print()
        ui.warn(f"Last error: {last_err}", stdout=True)


# ─────────────────────────────────────────────────────────────────────────────
# help / routing
# ─────────────────────────────────────────────────────────────────────────────

_HELP_SECTIONS: list[tuple[str, list[tuple[str, str, str]]]] = [
    (
        "Remember & recall",
        [
            ("add <text>", "Store a memory", "learn, save"),
            ("ask <query>", "Search by meaning and keywords", "search, find"),
            ("inspect", "Browse and filter memories", "ls"),
            ("update <id>", "Edit a memory", "edit"),
            ("forget <id>", "Delete a memory", "rm"),
        ],
    ),
    (
        "Understand your memory",
        [
            ("status", "Health and size at a glance", ""),
            ("digest", "What changed recently", ""),
            ("conflicts", "Review contradictions", ""),
            ("patterns", "Top searches and knowledge gaps", ""),
            ("why", "Audit trail of reads and writes", ""),
            ("context", "Print a prompt-ready memory block", ""),
        ],
    ),
    (
        "Set up & maintain",
        [
            ("setup", "One-time setup: daemon, model, agent skills", ""),
            ("doctor", "Diagnose problems and suggest fixes", ""),
            ("daemon <action>", "start · stop · restart · status · warmup", ""),
            ("install-skills", "Teach your AI coding agents to use Mesh", ""),
            ("completion", "Tab-completion for your shell", ""),
        ],
    ),
    (
        "Data & integrations",
        [
            ("namespaces", "List, inspect, rename, delete namespaces", ""),
            ("export / import", "Back up or restore as JSON", ""),
            ("capture", "Extract memories from session logs", ""),
            ("shell", "Interactive Mesh shell", "i"),
            ("dashboard", "Open the web UI (localhost:7433)", ""),
            ("server", "Run the MCP server for AI agents", ""),
            ("http", "Run the REST API (localhost:7701)", ""),
        ],
    ),
]


def help_cmd() -> None:
    check_for_updates(force=False)
    width = ui.term_width()
    ui.header("Mesh", f"v{VERSION}")
    print(f"{ui.IND}{ui.style('Shared, searchable memory for you and your AI agents.', 'muted')}")

    ui.section("Quick start")
    for cmd, desc in (
        ('mesh add "We deploy the API on Fly.io"', "remember something"),
        ('mesh ask "how do we deploy?"', "find it again later"),
        ("mesh setup", "enable the daemon, model and agent skills"),
    ):
        print(f"{ui.IND}  {ui.style(cmd.ljust(42), 'info')}{ui.style(desc, 'muted')}")

    show_aliases = width >= 84
    for title, rows in _HELP_SECTIONS:
        ui.section(title)
        for cmd, desc, aliases in rows:
            line = f"{ui.IND}  {ui.style(cmd.ljust(18), 'bold')}{desc.ljust(44 if show_aliases else 0)}"
            if aliases and show_aliases:
                line += ui.style(aliases, "muted", "dim")
            print(line.rstrip())

    print()
    print(f"{ui.IND}{ui.style('Tips', 'muted', 'bold')}")
    print(f"{ui.IND}  {ui.style('mesh <command> --help'.ljust(22), 'info')}{ui.style('options and examples for any command', 'muted')}")
    print(f"{ui.IND}  {ui.style('--namespace <name>'.ljust(22), 'info')}{ui.style('work in a different namespace (or set MESH_NAMESPACE)', 'muted')}")
    print(f"{ui.IND}  {ui.style('--json'.ljust(22), 'info')}{ui.style('machine-readable output on ask, inspect, status and more', 'muted')}")
    print()


def _lazy_daemon_cmd() -> None:
    from .daemon import daemon_cmd

    daemon_cmd()


def _lazy_dashboard_cmd() -> None:
    from .dashboard import run as dashboard_cmd

    dashboard_cmd()


def _lazy_digest_cmd() -> None:
    from .digest import digest_cmd

    digest_cmd()


def _lazy_http_cmd() -> None:
    from .http_server import start as http_cmd

    http_cmd()


def _lazy_server_cmd() -> None:
    from .mcp_server import run as server_cmd

    server_cmd()


def _lazy_capture_cmd() -> None:
    from .passive import capture_cmd

    capture_cmd()


def _get_command_mapping() -> dict[str, Callable[[], None]]:
    return {
        "setup": setup_cmd,
        "warmup": setup_cmd,
        "add": add_cmd,
        "ask": ask_cmd,
        "inspect": inspect_cmd,
        "update": update_cmd,
        "forget": forget_cmd,
        "status": status_cmd,
        "why": why_cmd,
        "context": context_cmd,
        "help": help_cmd,
        "export": export_cmd,
        "import": import_cmd,
        "namespaces": namespaces_cmd,
        "capture": _lazy_capture_cmd,
        "digest": _lazy_digest_cmd,
        "daemon": _lazy_daemon_cmd,
        "server": _lazy_server_cmd,
        "dashboard": _lazy_dashboard_cmd,
        "http": _lazy_http_cmd,
        "conflicts": conflicts_cmd,
        "patterns": patterns_cmd,
        "doctor": doctor_cmd,
        "shell": shell_cmd,
        "completion": completion_cmd,
        "install-skills": install_skills_cmd,
    }


# Commands whose stdout is data (or a long-running server): never add decorative spacing.
_RAW_OUTPUT_CMDS = {"context", "completion", "server", "http", "dashboard", "shell"}


def _silence_broken_pipe() -> None:
    """`mesh inspect | head` should exit quietly instead of dumping a traceback."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except Exception:
        pass


def main() -> None:
    try:
        _main()
    except KeyboardInterrupt:
        print(file=sys.stderr)
        sys.exit(130)
    except BrokenPipeError:
        _silence_broken_pipe()
        sys.exit(0)


def _main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        help_cmd()
        return
    if sys.argv[1] in ("-v", "--version", "version"):
        print(f"mesh {VERSION}")
        return

    cmd = sys.argv[1].lower()
    if cmd.startswith("mesh-"):
        cmd = cmd[5:]
    cmd = _COMMAND_ALIASES.get(cmd, cmd)

    mapping = _get_command_mapping()

    # `mesh help add` -> `mesh add --help`
    if cmd == "help" and len(sys.argv) > 2:
        target = _COMMAND_ALIASES.get(sys.argv[2].lower(), sys.argv[2].lower())
        if target in mapping and target != "help":
            sys.argv = [f"mesh-{target}", "--help"]
            mapping[target]()
            return

    if cmd in mapping:
        rest = sys.argv[2:]
        sys.argv = [f"mesh-{cmd}"] + rest
        try:
            mapping[cmd]()
        finally:
            if cmd not in _RAW_OUTPUT_CMDS and "--json" not in rest and sys.stdout.isatty():
                print()
        return

    suggestions = _suggest_did_you_mean(cmd, list(mapping.keys()))
    print(file=sys.stderr)
    ui.error(
        f"'{cmd}' isn't a Mesh command",
        ("Did you mean: " + ", ".join(f"mesh {s}" for s in suggestions) + "?") if suggestions else None,
    )
    ui.hint("Run `mesh help` to see everything Mesh can do.", stream=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()

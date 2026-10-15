"""
mesh/cli.py — Command-line interface for Mesh.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

from .version import VERSION, check_for_updates


def _resolve_namespace(ns_arg: Optional[str]) -> str:
    return (ns_arg or os.environ.get("MESH_NAMESPACE", "shared")).strip() or "shared"


def export_cmd() -> None:
    """Entry point for: mesh-export"""
    parser = argparse.ArgumentParser(
        description="Export Mesh memories to a JSON file.",
        epilog="Example: mesh-export backup.json --namespace work",
    )
    parser.add_argument("output", help="Output file path (e.g. backup.json)")
    parser.add_argument("--namespace", default=None, help="Namespace to export (default: shared)")
    parser.add_argument(
        "--no-embeddings",
        action="store_true",
        help="Exclude embedding vectors (smaller file, human-readable)",
    )
    args = parser.parse_args()

    from .io import export_namespace

    ns = _resolve_namespace(args.namespace)
    export_namespace(
        namespace=ns,
        output_path=args.output,
        include_embeddings=not args.no_embeddings,
    )


def import_cmd() -> None:
    """Entry point for: mesh-import"""
    parser = argparse.ArgumentParser(
        description="Import Mesh memories from a JSON export file.",
        epilog="Example: mesh-import backup.json --namespace work",
    )
    parser.add_argument("input", help="Input export file path")
    parser.add_argument(
        "--namespace",
        default=None,
        help="Override target namespace (default: use namespace from export file)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing memories with same ID (default: skip)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be imported without writing anything",
    )
    args = parser.parse_args()

    from .io import import_namespace

    import_namespace(
        input_path=args.input,
        target_namespace=args.namespace,
        merge_strategy="overwrite" if args.overwrite else "skip_existing",
        dry_run=args.dry_run,
    )


def namespaces_cmd() -> None:
    """Entry point for: mesh-namespaces"""
    parser = argparse.ArgumentParser(
        description="Manage Mesh namespaces.",
        epilog="""Commands:
  mesh-namespaces list                    List all namespaces
  mesh-namespaces delete work             Delete namespace 'work' and all its memories
  mesh-namespaces rename work work-old    Rename namespace 'work' to 'work-old'
  mesh-namespaces stats work              Show stats for namespace 'work'
        """,
    )
    parser.add_argument("command", choices=["list", "delete", "rename", "stats"])
    parser.add_argument("args", nargs="*", help="Arguments for the command")
    parsed = parser.parse_args()

    from .namespaces import delete_namespace, list_namespaces, namespace_stats, rename_namespace

    if parsed.command == "list":
        namespaces = list_namespaces()
        if not namespaces:
            print("No namespaces found.")
            return
        print(f"{'Namespace':<30} {'Memories':>10} {'Local-only':>12} {'Last updated':<25}")
        print("-" * 80)
        for ns in namespaces:
            print(
                f"{ns['namespace']:<30} {ns['memory_count']:>10} "
                f"{ns['local_only_count']:>12} {ns['last_updated']:<25}"
            )

    elif parsed.command == "delete":
        if not parsed.args:
            print("Error: provide a namespace name. Example: mesh-namespaces delete work")
            sys.exit(1)
        name = parsed.args[0]
        confirm = input(f"Delete ALL memories in namespace '{name}'? This cannot be undone. [y/N] ")
        if confirm.lower() != "y":
            print("Cancelled.")
            return
        count = delete_namespace(name)
        print(f"[OK] Deleted namespace '{name}' ({count} memories removed)")

    elif parsed.command == "rename":
        if len(parsed.args) < 2:
            print("Error: provide old and new names. Example: mesh-namespaces rename work work-archived")
            sys.exit(1)
        old, new = parsed.args[0], parsed.args[1]
        count = rename_namespace(old, new)
        print(f"[OK] Renamed '{old}' -> '{new}' ({count} memories updated)")

    elif parsed.command == "stats":
        if not parsed.args:
            print("Error: provide a namespace name. Example: mesh-namespaces stats work")
            sys.exit(1)
        name = parsed.args[0]
        try:
            stats = namespace_stats(name)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"\nNamespace: {name}")
        print(f"  Total memories:      {stats['memory_count']}")
        print(f"  Local-only:          {stats['local_only_count']}")
        print(f"  Exportable:          {stats['exportable_count']}")
        print(f"  Memory types:        {json.dumps(stats['type_breakdown'], indent=4)}")
        print(f"  Avg confidence:      {stats['avg_confidence']:.2f}")
        print(f"  Stale memories:      {stats['stale_count']}")
        print(f"  Total recalls:       {stats['total_recalls']}")
        print(f"  Created:             {stats['oldest_memory']}")
        print(f"  Last updated:        {stats['newest_memory']}")


def context_cmd() -> None:
    """Entry point for: mesh-context"""
    from .memory import VALID_MEMORY_TYPES

    parser = argparse.ArgumentParser(
        description="Output Mesh memories as a formatted system prompt block.",
        epilog="""Examples:
  mesh-context                              # Top 10 memories, markdown format
  mesh-context --query "deploy process"     # Focused on deploy memories
  mesh-context --format plain --count 5     # Plain text, 5 memories
  CONTEXT=$(mesh-context) && echo "$CONTEXT"

Pipe directly into agent invocations:
  aider --system-prompt "$(mesh-context)" ...
  sgpt --system "$(mesh-context)" "help me with the deploy"
        """,
    )
    parser.add_argument("--query", default=None, help="Focus context on a specific topic")
    parser.add_argument("--namespace", default=None, help="Namespace to query (default: shared)")
    parser.add_argument("--count", type=int, default=10, help="Number of memories to include")
    parser.add_argument("--format", choices=["markdown", "plain", "xml", "json"], default="markdown")
    parser.add_argument("--min-confidence", type=float, default=0.3)
    parser.add_argument(
        "--type",
        dest="memory_type",
        choices=sorted(VALID_MEMORY_TYPES),
        default=None,
        help="Filter context to a specific memory type",
    )
    parser.add_argument("--tags", default="", help="Comma-separated tags to filter by")
    args = parser.parse_args()

    from .context_builder import build_context

    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None
    result = build_context(
        query=args.query,
        namespace=args.namespace,
        count=args.count,
        format=args.format,
        min_confidence=args.min_confidence,
        memory_type=args.memory_type,
        tags=tags,
    )
    if result:
        print(result)
    else:
        print("No memories found.", file=sys.stderr)
        sys.exit(1)


def add_cmd() -> None:
    """Entry point for: mesh-add"""
    from .audit import log_learn
    from .memory import VALID_MEMORY_TYPES, Mesh

    parser = argparse.ArgumentParser(
        description="Store a memory directly into Mesh.",
        epilog="Example: mesh-add 'prod DB password rotated on May 1' --type fact --tags db,security",
    )
    parser.add_argument("content", help="The memory to store")
    parser.add_argument(
        "--type",
        dest="memory_type",
        default="fact",
        choices=sorted(VALID_MEMORY_TYPES),
        help="Memory type (default: fact)",
    )
    parser.add_argument("--tags", default="", help="Comma-separated tags")
    parser.add_argument(
        "--private",
        action="store_true",
        help="Mark as local-only (never synced or exported)",
    )
    parser.add_argument("--namespace", default=None)
    parser.add_argument("--confidence", type=float, default=1.0)
    parser.add_argument(
        "--ttl",
        type=float,
        default=None,
        help="Days before this memory is considered stale",
    )
    args = parser.parse_args()

    ns = _resolve_namespace(args.namespace)
    mesh = Mesh(namespace=ns, agent_id="human-cli")
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []

    result = mesh.learn(
        content=args.content,
        memory_type=args.memory_type,
        confidence=args.confidence,
        tags=tags,
        local_only=args.private,
        ttl_days=args.ttl,
    )
    log_learn(
        content=args.content,
        namespace=ns,
        agent_id="human-cli",
        result=result,
    )

    private_note = " (private — will not be exported or synced)" if args.private else ""
    dedup_note = " (deduplicated existing memory)" if result.get("deduplicated") else ""
    conflict_note = ""
    if result.get("conflict"):
        existing_text = result["conflict"].get("existing_content") or result["conflict"].get(
            "existing_memory", ""
        )
        conflict_note = f'\n[WARNING] Possible conflict with existing memory:\n   "{existing_text}"'

    print(f"[OK] Stored [{args.memory_type}] in namespace '{ns}'{private_note}{dedup_note}{conflict_note}")
    if result.get("memory_id"):
        print(f"  ID: {result['memory_id']}")


def ask_cmd() -> None:
    """Entry point for: mesh-ask"""
    from .audit import log_recall
    from .memory import VALID_MEMORY_TYPES, Mesh

    parser = argparse.ArgumentParser(
        description="Search Mesh for memories relevant to a question.",
        epilog="Example: mesh-ask 'what is the deploy process?' --type process",
    )
    parser.add_argument("query", help="What to search for")
    parser.add_argument("--count", type=int, default=5, help="Max results to return (default: 5)")
    parser.add_argument("--namespace", default=None)
    parser.add_argument(
        "--type",
        dest="memory_type",
        choices=sorted(VALID_MEMORY_TYPES),
        default=None,
        help="Filter by memory type",
    )
    parser.add_argument("--tags", default="", help="Comma-separated tags to filter by")
    parser.add_argument("--min-confidence", type=float, default=0.0)
    parser.add_argument("--min-similarity", type=float, default=0.0)
    parser.add_argument("--no-stale", action="store_true", help="Exclude stale memories from results")
    args = parser.parse_args()

    ns = _resolve_namespace(args.namespace)
    mesh = Mesh(namespace=ns, agent_id="human-cli")
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None

    results = mesh.recall(
        query=args.query,
        n=args.count,
        min_similarity=args.min_similarity,
        min_confidence=args.min_confidence,
        include_stale=not args.no_stale,
        memory_type=args.memory_type,
        tags=tags,
    )
    log_recall(
        query=args.query,
        namespace=ns,
        agent_id="human-cli",
        results=results,
    )

    if not results:
        print(f"No memories found for: {args.query}")
        return

    print(f"\nFound {len(results)} memories in namespace '{ns}':\n")
    for i, mem in enumerate(results, 1):
        sim = mem.get("similarity", 0.0)
        conf = mem.get("effective_confidence", mem.get("confidence", 1.0))
        stale = " [STALE]" if mem.get("is_stale") else ""
        private = " [PRIVATE]" if mem.get("local_only") else ""
        mem_tags = mem.get("tags") or []
        tags_str = f" #{' #'.join(mem_tags)}" if mem_tags else ""

        print(f"  {i}. {mem['content']}{stale}{private}")
        print(
            f"     id={mem.get('id', 'unknown')} | type={mem.get('memory_type', 'fact')} | "
            f"similarity={sim:.2f} | confidence={conf:.2f} | "
            f"from={mem.get('source_agent', 'unknown')}{tags_str}"
        )
        print()


def update_cmd() -> None:
    """Entry point for: mesh-update"""
    from .audit import log_update
    from .memory import VALID_MEMORY_TYPES, Mesh

    parser = argparse.ArgumentParser(
        description="Update an existing memory in-place by its ID.",
        epilog="Example: mesh-update <memory_id> --content 'Updated port is 5433' --type fact",
    )
    parser.add_argument("memory_id", help="The UUID of the memory to update")
    parser.add_argument("--content", default=None, help="New content for the memory")
    parser.add_argument(
        "--type",
        dest="memory_type",
        choices=sorted(VALID_MEMORY_TYPES),
        default=None,
        help="New memory type",
    )
    parser.add_argument("--confidence", type=float, default=None, help="New confidence (0.0 to 1.0)")
    parser.add_argument("--tags", default=None, help="New comma-separated tags")
    parser.add_argument("--ttl", type=float, default=None, help="New TTL in days")
    parser.add_argument("--private", dest="private", action="store_true", default=None)
    parser.add_argument("--public", dest="private", action="store_false")
    parser.add_argument("--namespace", default=None)
    args = parser.parse_args()

    ns = _resolve_namespace(args.namespace)
    mesh = Mesh(namespace=ns, agent_id="human-cli")
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags is not None else None

    try:
        mesh.update(
            memory_id=args.memory_id,
            content=args.content,
            memory_type=args.memory_type,
            confidence=args.confidence,
            tags=tags,
            ttl_days=args.ttl,
            local_only=args.private,
        )
        log_update(
            memory_id=args.memory_id,
            content=args.content,
            namespace=ns,
            agent_id="human-cli",
        )
        print(f"[OK] Updated memory '{args.memory_id}' in namespace '{ns}'")
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def forget_cmd() -> None:
    """Entry point for: mesh-forget"""
    from .audit import log_forget
    from .memory import Mesh

    parser = argparse.ArgumentParser(
        description="Delete a specific memory by its ID.",
        epilog="Example: mesh-forget 123e4567-e89b-12d3-a456-426614174000 --namespace shared",
    )
    parser.add_argument("memory_id", help="The UUID of the memory to delete")
    parser.add_argument("--namespace", default=None)
    args = parser.parse_args()

    ns = _resolve_namespace(args.namespace)
    mesh = Mesh(namespace=ns, agent_id="human-cli")
    deleted = mesh.forget(args.memory_id)
    log_forget(memory_id=args.memory_id, namespace=ns, agent_id="human-cli")

    if deleted:
        print(f"[OK] Deleted memory '{args.memory_id}' from namespace '{ns}'")
    else:
        print(f"Memory '{args.memory_id}' not found in namespace '{ns}'", file=sys.stderr)
        sys.exit(1)


def why_cmd() -> None:
    """Entry point for: mesh-why"""
    parser = argparse.ArgumentParser(
        description="Show recent memory reads and writes across all agents (audit trail).",
        epilog="Example: mesh-why --limit 30 --namespace work",
    )
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--namespace", default=None)
    parser.add_argument(
        "--action",
        choices=["read", "write", "update", "forget", "all"],
        default="all",
    )
    args = parser.parse_args()

    from .audit import get_audit_log

    ns = _resolve_namespace(args.namespace)
    entries = get_audit_log(namespace=ns, limit=args.limit, action_filter=args.action)

    if not entries:
        print(f"No audit entries found for namespace '{ns}'.")
        return

    print(f"\nAudit trail for namespace '{ns}' (most recent first):\n")
    icon_map = {"recall": "R", "learn": "W", "update": "U", "forget": "D"}
    for entry in entries:
        act = entry.get("action", "recall")
        action_icon = icon_map.get(act, "W")
        ts = str(entry.get("timestamp", ""))[:19]
        print(f"  {action_icon} [{ts}] {entry.get('agent_id', 'unknown')}")
        if act == "recall":
            print(f'     asked: "{entry.get("query", "")}"')
            top_res = entry.get("top_result") or ""
            if top_res:
                preview = f"{top_res[:80]}..." if len(top_res) > 80 else top_res
                print(f'     got:   "{preview}"')
        elif act == "update":
            print(f'     updated ({entry.get("memory_id", "")}): "{str(entry.get("content", ""))[:80]}"')
        elif act == "forget":
            print(f'     deleted: "{entry.get("memory_id", "")}"')
        else:
            print(f'     stored: "{str(entry.get("content", ""))[:80]}"')
        print()


def status_cmd() -> None:
    """Entry point for: mesh-status"""
    check_for_updates()
    parser = argparse.ArgumentParser(description="Show Mesh status overview.")
    parser.add_argument("--namespace", default=None)
    args = parser.parse_args()

    from .audit import get_audit_log
    from .namespaces import list_namespaces, namespace_stats

    ns = _resolve_namespace(args.namespace)

    try:
        stats = namespace_stats(ns)
    except ValueError:
        print(f"Namespace '{ns}' is empty or does not exist yet.")
        print("Run 'mesh-add \"something\"' to create it.")
        return

    recent = get_audit_log(namespace=ns, limit=5)
    last_activity = str(recent[0]["timestamp"])[:19] if recent else "never"

    print(f"\nMesh Status — namespace: {ns}\n")
    print(
        f"  Memories:       {stats['memory_count']} total, "
        f"{stats['local_only_count']} private, "
        f"{stats['stale_count']} stale"
    )
    print(f"  Exportable:     {stats['exportable_count']}")
    print(f"  Total recalls:  {stats['total_recalls']}")
    print(f"  Last activity:  {last_activity}")
    print(f"  Types:          {stats['type_breakdown']}")

    all_ns = list_namespaces()
    if len(all_ns) > 1:
        others = [n["namespace"] for n in all_ns if n["namespace"] != ns]
        print(f"  Other namespaces: {', '.join(others)}")
    print()


def help_cmd() -> None:
    """Entry point for: mesh-help"""
    check_for_updates()
    print(f"Mesh Collective Memory (v{VERSION})")
    print("Usage: mesh <command> [options]\n")
    print("Available Commands:")
    print("  mesh-add        Store a memory manually")
    print("  mesh-ask        Search for memories by topic or question (hybrid search)")
    print("  mesh-update     Update an existing memory in-place by ID")
    print("  mesh-forget     Delete a specific memory by ID")
    print("  mesh-status     Overview of namespaces and memory counts")
    print("  mesh-why        Show recent audit trail (who read/wrote what)")
    print("  mesh-context    Output memories as system prompt (for AI tools)")
    print("  mesh-server     Start the MCP server (for Claude/Cursor)")
    print("  mesh-dashboard  Open the web-based visualizer")
    print("  mesh-http       Start the REST API server")
    print("  mesh-namespaces List, rename, or delete namespaces")
    print("  mesh-export     Export memories to JSON")
    print("  mesh-import     Import memories from JSON")
    print("  mesh-capture    Extract memories from past AI logs")
    print("  mesh-digest     Generate high-level summaries of activity")
    print("\nFor help with a specific command, run: <command> --help")
    print("Example: mesh-add --help")


def main() -> None:
    """Unified 'mesh' command that routes to all subcommands."""
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        help_cmd()
        return

    if sys.argv[1] in ("-v", "--version", "version"):
        print(f"mesh {VERSION}")
        return

    cmd = sys.argv[1].lower()
    if cmd.startswith("mesh-"):
        cmd = cmd[5:]

    from .dashboard import run as dashboard_cmd
    from .digest import digest_cmd
    from .http_server import start as http_cmd
    from .mcp_server import run as server_cmd
    from .passive import capture_cmd

    mapping = {
        "add": add_cmd,
        "ask": ask_cmd,
        "update": update_cmd,
        "forget": forget_cmd,
        "status": status_cmd,
        "why": why_cmd,
        "context": context_cmd,
        "help": help_cmd,
        "export": export_cmd,
        "import": import_cmd,
        "namespaces": namespaces_cmd,
        "capture": capture_cmd,
        "digest": digest_cmd,
        "server": server_cmd,
        "dashboard": dashboard_cmd,
        "http": http_cmd,
    }

    if cmd in mapping:
        sys.argv = [f"mesh-{cmd}"] + sys.argv[2:]
        mapping[cmd]()
    else:
        print(f"Unknown command: '{cmd}'", file=sys.stderr)
        print("Type 'mesh-help' or 'mesh help' to see available commands.", file=sys.stderr)
        sys.exit(1)
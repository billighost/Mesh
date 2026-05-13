"""
mesh/cli.py — Command-line interface for Mesh.
"""

import sys
import json
import argparse
from .version import VERSION, check_for_updates


def export_cmd():
    """Entry point for: mesh-export"""
    parser = argparse.ArgumentParser(
        description="Export Mesh memories to a JSON file.",
        epilog="Example: mesh-export backup.json --namespace work"
    )
    parser.add_argument("output", help="Output file path (e.g. backup.json)")
    parser.add_argument("--namespace", default="shared", help="Namespace to export (default: shared)")
    parser.add_argument("--no-embeddings", action="store_true",
                        help="Exclude embedding vectors (smaller file, human-readable)")
    args = parser.parse_args()

    from .io import export_namespace
    export_namespace(
        namespace=args.namespace,
        output_path=args.output,
        include_embeddings=not args.no_embeddings
    )


def import_cmd():
    """Entry point for: mesh-import"""
    parser = argparse.ArgumentParser(
        description="Import Mesh memories from a JSON export file.",
        epilog="Example: mesh-import backup.json --namespace work"
    )
    parser.add_argument("input", help="Input export file path")
    parser.add_argument("--namespace", default=None,
                        help="Override target namespace (default: use namespace from export file)")
    parser.add_argument("--overwrite", action="store_true",
                        help="Overwrite existing memories with same ID (default: skip)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview what would be imported without writing anything")
    args = parser.parse_args()

    from .io import import_namespace
    import_namespace(
        input_path=args.input,
        target_namespace=args.namespace,
        merge_strategy="overwrite" if args.overwrite else "skip_existing",
        dry_run=args.dry_run
    )


def namespaces_cmd():
    """Entry point for: mesh-namespaces"""
    parser = argparse.ArgumentParser(
        description="Manage Mesh namespaces.",
        epilog="""Commands:
  mesh-namespaces list                    List all namespaces
  mesh-namespaces delete work             Delete namespace 'work' and all its memories
  mesh-namespaces rename work work-old    Rename namespace 'work' to 'work-old'
  mesh-namespaces stats work              Show stats for namespace 'work'
        """
    )
    parser.add_argument("command", choices=["list", "delete", "rename", "stats"])
    parser.add_argument("args", nargs="*", help="Arguments for the command")
    parsed = parser.parse_args()

    from .namespaces import list_namespaces, delete_namespace, rename_namespace, namespace_stats

    if parsed.command == "list":
        namespaces = list_namespaces()
        if not namespaces:
            print("No namespaces found.")
            return
        print(f"{'Namespace':<30} {'Memories':>10} {'Local-only':>12} {'Last updated':<25}")
        print("-" * 80)
        for ns in namespaces:
            print(f"{ns['namespace']:<30} {ns['memory_count']:>10} {ns['local_only_count']:>12} {ns['last_updated']:<25}")

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
        stats = namespace_stats(name)
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


def context_cmd():
    """Entry point for: mesh-context"""
    import argparse
    parser = argparse.ArgumentParser(
        description="Output Mesh memories as a formatted system prompt block.",
        epilog="""Examples:
  mesh-context                              # Top 10 memories, markdown format
  mesh-context --query "deploy process"     # Focused on deploy memories
  mesh-context --format plain --count 5    # Plain text, 5 memories
  CONTEXT=$(mesh-context) && echo $CONTEXT  # Capture for use in scripts

Pipe directly into agent invocations:
  aider --system-prompt "$(mesh-context)" ...
  sgpt --system "$(mesh-context)" "help me with the deploy"
        """
    )
    parser.add_argument("--query", default=None, help="Focus context on a specific topic")
    parser.add_argument("--namespace", default=None, help="Namespace to query (default: shared)")
    parser.add_argument("--count", type=int, default=10, help="Number of memories to include")
    parser.add_argument("--format", choices=["markdown", "plain", "xml", "json"], default="markdown")
    parser.add_argument("--min-confidence", type=float, default=0.3)
    args = parser.parse_args()

    from .context_builder import build_context
    result = build_context(
        query=args.query,
        namespace=args.namespace,
        count=args.count,
        format=args.format,
        min_confidence=args.min_confidence
    )
    if result:
        print(result)
    else:
        import sys
        print("No memories found.", file=sys.stderr)
        sys.exit(1)


def add_cmd():
    """Entry point for: mesh-add"""
    import argparse
    parser = argparse.ArgumentParser(
        description="Store a memory directly into Mesh.",
        epilog="Example: mesh-add 'prod DB password rotated on May 1' --type fact --tags db,security"
    )
    parser.add_argument("content", help="The memory to store")
    parser.add_argument("--type", dest="memory_type", default="general",
                        choices=["fact", "process", "decision", "error", "general"],
                        help="Memory type (default: general)")
    parser.add_argument("--tags", default="", help="Comma-separated tags")
    parser.add_argument("--private", action="store_true",
                        help="Mark as local-only (never synced or exported)")
    parser.add_argument("--namespace", default=None)
    parser.add_argument("--confidence", type=float, default=1.0)
    parser.add_argument("--ttl", type=int, default=None,
                        help="Days before this memory is considered stale")
    args = parser.parse_args()

    from .memory import Mesh
    import os
    ns = args.namespace or os.environ.get("MESH_NAMESPACE", "shared")
    mesh = Mesh(namespace=ns, agent_id="human-cli")
    
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []
    
    result = mesh.learn(
        content=args.content,
        memory_type=args.memory_type,
        confidence=args.confidence,
        tags=tags,
        local_only=args.private,
        ttl_days=args.ttl
    )
    
    private_note = " (private — will not be exported or synced)" if args.private else ""
    conflict_note = ""
    if result.get("conflict"):
        conflict_note = f"\n[WARNING] Possible conflict with existing memory:\n   \"{result['conflict']['existing_memory']}\""
    
    print(f"[OK] Stored [{args.memory_type}] in namespace '{ns}'{private_note}{conflict_note}")
    if result.get("memory_id"):
        print(f"  ID: {result['memory_id']}")


def ask_cmd():
    """Entry point for: mesh-ask"""
    import argparse
    parser = argparse.ArgumentParser(
        description="Search Mesh for memories relevant to a question.",
        epilog="Example: mesh-ask 'what is the deploy process?'"
    )
    parser.add_argument("query", help="What to search for")
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--namespace", default=None)
    parser.add_argument("--type", dest="memory_type", default=None)
    parser.add_argument("--min-confidence", type=float, default=0.0)
    args = parser.parse_args()

    from .memory import Mesh
    import os
    ns = args.namespace or os.environ.get("MESH_NAMESPACE", "shared")
    mesh = Mesh(namespace=ns, agent_id="human-cli")
    
    results = mesh.recall(
        query=args.query,
        n=args.count,
        min_confidence=args.min_confidence
    )
    
    if not results:
        print(f"No memories found for: {args.query}")
        return
    
    print(f"\nFound {len(results)} memories in namespace '{ns}':\n")
    for i, mem in enumerate(results, 1):
        sim = mem.get("similarity", 0)
        conf = mem.get("effective_confidence", mem.get("confidence", 1.0))
        stale = " [STALE]" if mem.get("is_stale") else ""
        private = " [PRIVATE]" if mem.get("local_only") else ""
        tags = f" #{' #'.join(mem['tags'])}" if mem.get("tags") else ""
        
        print(f"  {i}. {mem['content']}{stale}{private}")
        print(f"     type={mem.get('memory_type','general')} | "
              f"similarity={sim:.2f} | confidence={conf:.2f} | "
              f"from={mem.get('source_agent','unknown')}{tags}")
        print()


def why_cmd():
    """Entry point for: mesh-why"""
    import argparse
    parser = argparse.ArgumentParser(
        description="Show recent memory reads and writes across all agents (audit trail).",
        epilog="Example: mesh-why --limit 30 --namespace work"
    )
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--namespace", default=None)
    parser.add_argument("--action", choices=["read", "write", "all"], default="all")
    args = parser.parse_args()

    from .audit import get_audit_log
    import os
    ns = args.namespace or os.environ.get("MESH_NAMESPACE", "shared")
    entries = get_audit_log(namespace=ns, limit=args.limit, action_filter=args.action)
    
    if not entries:
        print(f"No audit entries found for namespace '{ns}'.")
        return
    
    print(f"\nAudit trail for namespace '{ns}' (most recent first):\n")
    for entry in entries:
        action_icon = "R" if entry["action"] == "recall" else "W"
        print(f"  {action_icon} [{entry['timestamp'][:19]}] {entry['agent_id']}")
        if entry["action"] == "recall":
            print(f"     asked: \"{entry['query']}\"")
            if entry.get("top_result"):
                print(f"     got:   \"{entry['top_result'][:80]}...\"" 
                      if len(entry.get("top_result","")) > 80 
                      else f"     got:   \"{entry['top_result']}\"")
        else:
            print(f"     stored: \"{entry['content'][:80]}\"")
        print()


def status_cmd():
    """Entry point for: mesh-status"""
    check_for_updates()
    import argparse
    parser = argparse.ArgumentParser(description="Show Mesh status overview.")
    parser.add_argument("--namespace", default=None)
    args = parser.parse_args()

    from .namespaces import list_namespaces, namespace_stats
    from .audit import get_audit_log
    import os

    ns = args.namespace or os.environ.get("MESH_NAMESPACE", "shared")
    
    try:
        stats = namespace_stats(ns)
    except ValueError:
        print(f"Namespace '{ns}' is empty or does not exist yet.")
        print(f"Run 'mesh-add \"something\"' to create it.")
        return
    
    recent = get_audit_log(namespace=ns, limit=5)
    last_activity = recent[0]["timestamp"][:19] if recent else "never"
    
    print(f"\nMesh Status — namespace: {ns}\n")
    print(f"  Memories:       {stats['memory_count']} total, "
          f"{stats['local_only_count']} private, "
          f"{stats['stale_count']} stale")
    print(f"  Exportable:     {stats['exportable_count']}")
    print(f"  Total recalls:  {stats['total_recalls']}")
    print(f"  Last activity:  {last_activity}")
    print(f"  Types:          {stats['type_breakdown']}")
    
    all_ns = list_namespaces()
    if len(all_ns) > 1:
        others = [n["namespace"] for n in all_ns if n["namespace"] != ns]
        print(f"  Other namespaces: {', '.join(others)}")
    print()


def help_cmd():
    """Entry point for: mesh-help"""
    check_for_updates()
    print(f"Mesh Collective Memory (v{VERSION})")
    print("Usage: mesh <command> [options]\n")
    print("Available Commands:")
    print("  mesh-add        Store a memory manually")
    print("  mesh-ask        Search for memories by topic or question")
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


def main():
    """Unified 'mesh' command that routes to others."""
    if len(sys.argv) < 2:
        help_cmd()
        return

    cmd = sys.argv[1]
    
    # Map 'mesh <subcommand>' to the corresponding function
    mapping = {
        "add": add_cmd,
        "ask": ask_cmd,
        "status": status_cmd,
        "why": why_cmd,
        "context": context_cmd,
        "help": help_cmd,
        "export": export_cmd,
        "import": import_cmd,
        "namespaces": namespaces_cmd,
    }
    
    if cmd in mapping:
        # Rebuild sys.argv to look like the subcommand was called directly
        sys.argv = [f"mesh-{cmd}"] + sys.argv[2:]
        mapping[cmd]()
    else:
        print(f"Unknown command: '{cmd}'")
        print("Type 'mesh-help' or 'mesh help' to see available commands.")
        sys.exit(1)

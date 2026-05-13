"""
mesh/context_builder.py — Builds formatted system prompt context blocks from Mesh memories.
"""

import os
from typing import Optional
from .memory import Mesh


def build_context(
    query: Optional[str] = None,
    namespace: Optional[str] = None,
    count: int = 10,
    format: str = "markdown",
    min_confidence: float = 0.3,
    agent_id: str = "context-builder"
) -> str:
    """
    Build a formatted context block from the most relevant memories.
    
    Args:
        query: Optional query to focus the context. If None, pulls top memories by access frequency.
        namespace: Namespace to query. Defaults to MESH_NAMESPACE env var or "shared".
        count: Number of memories to include. Default 10.
        format: Output format — "markdown", "plain", "xml", or "json".
        min_confidence: Minimum confidence score to include. Default 0.3.
        agent_id: Agent ID to log this context pull under.
    
    Returns:
        Formatted string ready to paste into a system prompt.
    """
    ns = namespace or os.environ.get("MESH_NAMESPACE", "shared")
    mesh = Mesh(namespace=ns, agent_id=agent_id)
    
    if query:
        memories = mesh.recall(query=query, n=count, min_confidence=min_confidence)
    else:
        # No query: pull most-accessed memories as general context
        memories = mesh.inspect(limit=count * 3)  # get more, then sort
        memories = sorted(memories, key=lambda m: m.get("access_count", 0), reverse=True)
        memories = [m for m in memories if m.get("confidence", 1.0) >= min_confidence][:count]
    
    if not memories:
        return ""
    
    return _format(memories, format, ns)


def _format(memories: list, format: str, namespace: str) -> str:
    if format == "json":
        import json
        return json.dumps({
            "mesh_context": True,
            "namespace": namespace,
            "memories": memories
        }, indent=2)
    
    if format == "xml":
        lines = ["<mesh_context>"]
        for mem in memories:
            stale = ' stale="true"' if mem.get("is_stale") else ""
            lines.append(f'  <memory type="{mem.get("memory_type", "general")}"{stale}>')
            lines.append(f'    {mem["content"]}')
            lines.append(f'  </memory>')
        lines.append("</mesh_context>")
        return "\n".join(lines)
    
    if format == "plain":
        lines = ["[MESH CONTEXT]"]
        for mem in memories:
            stale = " (possibly stale)" if mem.get("is_stale") else ""
            lines.append(f"- {mem['content']}{stale}")
        return "\n".join(lines)
    
    # Default: markdown (best for LLM system prompts)
    lines = [
        "<mesh_context>",
        "The following facts were stored by AI agents working on this project.",
        "Trust these as ground truth unless you have newer information.",
        "",
    ]
    
    by_type: dict = {}
    for mem in memories:
        t = mem.get("memory_type", "general")
        by_type.setdefault(t, []).append(mem)
    
    for mem_type, mems in sorted(by_type.items()):
        lines.append(f"**{mem_type.title()}**")
        for mem in mems:
            stale = " *(possibly stale)*" if mem.get("is_stale") else ""
            lines.append(f"- {mem['content']}{stale}")
        lines.append("")
    
    lines.append("</mesh_context>")
    return "\n".join(lines)

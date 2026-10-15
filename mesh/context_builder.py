"""
mesh/context_builder.py — Builds formatted system prompt context blocks from Mesh memories.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from .memory import Mesh


def build_context(
    query: Optional[str] = None,
    namespace: Optional[str] = None,
    count: int = 10,
    format: str = "markdown",
    min_confidence: float = 0.3,
    memory_type: Optional[str] = None,
    tags: Optional[list[str]] = None,
    agent_id: str = "context-builder",
) -> str:
    """
    Build a formatted context block from the most relevant memories.

    Args:
        query: Optional query to focus the context. If None, pulls top memories by composite access and relevance.
        namespace: Namespace to query. Defaults to MESH_NAMESPACE env var or "shared".
        count: Number of memories to include. Default 10.
        format: Output format — "markdown", "plain", "xml", or "json".
        min_confidence: Minimum effective confidence score to include. Default 0.3.
        memory_type: Optional memory_type filter.
        tags: Optional tag filters.
        agent_id: Agent ID to log this context pull under.

    Returns:
        Formatted string ready to paste into a system prompt.
    """
    ns = namespace or os.environ.get("MESH_NAMESPACE", "shared")
    mesh = Mesh(namespace=ns, agent_id=agent_id)

    if query:
        memories = mesh.recall(
            query=query,
            n=count,
            min_confidence=min_confidence,
            memory_type=memory_type,
            tags=tags,
        )
    else:
        # No query: pull inspect list, sort by access count and composite relevance, then filter
        raw_mems = mesh.inspect(limit=max(count * 3, 30), memory_type=memory_type)
        filtered = [
            m
            for m in raw_mems
            if float(m.get("effective_confidence", m.get("confidence", 1.0))) >= min_confidence
        ]
        if tags:
            tag_set = {str(t).lower() for t in tags}
            filtered = [
                m
                for m in filtered
                if tag_set.issubset({str(t).lower() for t in m.get("tags_list", [])})
            ]
        filtered.sort(
            key=lambda x: (
                int(x.get("access_count", 0)),
                float(x.get("effective_confidence", x.get("confidence", 1.0))),
            ),
            reverse=True,
        )
        memories = filtered[:count]

    if not memories:
        return ""

    return _format(memories, format, ns)


def _format(memories: list[dict[str, Any]], format: str, namespace: str) -> str:
    fmt = (format or "markdown").strip().lower()

    if fmt == "json":
        return json.dumps(
            {
                "mesh_context": True,
                "namespace": namespace,
                "memories": memories,
            },
            indent=2,
        )

    if fmt == "xml":
        lines = ["<mesh_context>"]
        for mem in memories:
            stale = ' stale="true"' if mem.get("is_stale") else ""
            mtype = mem.get("memory_type", "general")
            content = str(mem.get("content", ""))
            lines.append(f'  <memory type="{mtype}"{stale}>')
            lines.append(f"    {content}")
            lines.append("  </memory>")
        lines.append("</mesh_context>")
        return "\n".join(lines)

    if fmt == "plain":
        lines = ["[MESH CONTEXT — Shared Agent Memory]"]
        for mem in memories:
            stale = " (possibly stale)" if mem.get("is_stale") else ""
            content = str(mem.get("content", ""))
            lines.append(f"- {content}{stale}")
        return "\n".join(lines)

    # Default: markdown (best for LLM system prompts)
    lines = [
        "<mesh_context>",
        "The following facts were stored by AI agents working on this project.",
        "Trust these as ground truth unless you have newer information.",
        "",
    ]

    by_type: dict[str, list[dict[str, Any]]] = {}
    for mem in memories:
        t = mem.get("memory_type", "general")
        by_type.setdefault(t, []).append(mem)

    for mem_type, mems in sorted(by_type.items()):
        lines.append(f"**{mem_type.title()}**")
        for mem in mems:
            stale = " *(possibly stale)*" if mem.get("is_stale") else ""
            content = str(mem.get("content", ""))
            lines.append(f"- {content}{stale}")
        lines.append("")

    lines.append("</mesh_context>")
    return "\n".join(lines)
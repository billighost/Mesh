"""Shared startup-brief limits and rendering, without storage or transport imports."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any


MAX_BRIEF_TOKENS = 32768
MAX_BRIEF_FACTS = 100


def validate_brief_options(max_tokens: int = 500, max_facts: int = 20, fact_offset: int = 0) -> None:
    """Reject coercible JSON values before a transport looks up a namespace."""
    if type(max_tokens) is not int or not 1 <= max_tokens <= MAX_BRIEF_TOKENS:
        raise ValueError("max_tokens must be an integer between 1 and 32768.")
    if type(max_facts) is not int or not 0 <= max_facts <= MAX_BRIEF_FACTS:
        raise ValueError("max_facts must be an integer between 0 and 100.")
    if type(fact_offset) is not int or fact_offset < 0:
        raise ValueError("fact_offset must be a nonnegative integer.")


class BriefTextBudget:
    """Admit complete Markdown lines, counting separators and reserved notices."""

    def __init__(self, max_tokens: int):
        validate_brief_options(max_tokens)
        self.limit_chars = max_tokens * 4
        self.lines: list[str] = []
        self._chars = 0

    def append_line(self, line: str, reserve: int = 0) -> bool:
        length = self._chars + bool(self.lines) + len(line)
        if length + reserve > self.limit_chars:
            return False
        self.lines.append(line)
        self._chars = length
        return True

    def markdown(self) -> str:
        return "\n".join(self.lines).strip()


def finalize_project_brief(payload: dict[str, Any]) -> dict[str, Any]:
    """Estimate Markdown and compact logical JSON after adding transport fields.

    JSON-RPC wrappers, pretty printing, and CLI defense banners are transport
    framing. They are not covered by the Markdown budget or this compact estimate.
    """
    result = dict(payload)
    result["budget_scope"] = "brief_markdown"
    result["estimated_tokens"] = (len(result["brief_markdown"]) + 3) // 4
    result["payload_estimated_tokens"] = 0
    # The estimate includes its own digits, so settle the small self-reference.
    while True:
        estimate = (len(json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False)) + 3) // 4
        if estimate == result["payload_estimated_tokens"]:
            return result
        result["payload_estimated_tokens"] = estimate


def empty_project_brief(namespace: str, max_tokens: int = 500, max_facts: int = 20, fact_offset: int = 0) -> dict[str, Any]:
    """Render a validated missing namespace without opening or creating storage."""
    validate_brief_options(max_tokens, max_facts, fact_offset)
    text = BriefTextBudget(max_tokens)
    text.append_line(f"# Project Brief: {namespace}")
    text.append_line("0 visible memories | 0 open work items | 0 pending contradictions")
    text.append_line("Namespace state: new_or_empty. Record durable facts and create work items when needed.")
    return finalize_project_brief({
        "namespace": namespace, "namespace_exists": False, "namespace_state": "new_or_empty",
        "total_memories": 0, "open_tasks_count": 0, "completed_recent_count": 0,
        "pending_contradictions": 0, "key_facts_count": 0, "facts_available": 0,
        "facts_included": 0, "facts_omitted": 0, "selected_memory_ids": [],
        "next_fact_offset": None, "selection_revision": None,
        "as_of": datetime.now(timezone.utc).isoformat(), "handoff_memory_id": None,
        "open_work_items_included": 0, "completed_recent_included": 0,
        "brief_markdown": text.markdown(), "untrusted_reference_data": True,
    })

"""Versioned, original-source passage boundaries for the local derived index."""
from __future__ import annotations

import bisect
import re
from typing import Any, Sequence

from .envutil import env_bool

PASSAGE_FORMAT_VERSION = "paragraph-sentences-v1"
SHORT_DOCUMENT_TOKENS = 254
PASSAGE_TOKENS = 224
OVERSIZED_SENTENCE_OVERLAP = 32
_PARAGRAPH_BREAK = re.compile(r"\r?\n[ \t]*\r?\n")
_SENTENCE_END = re.compile(r"[.!?][\"')\]]*(?=\s|$)")


def passage_index_enabled() -> bool:
    return env_bool("MESH_PASSAGE_INDEX", False)


def validate_representation(value: str) -> str:
    if value not in ("", PASSAGE_FORMAT_VERSION):
        raise ValueError("Unsupported passage representation version.")
    return value


def validate_spans(content: str, spans: Sequence[tuple[int, int]]) -> None:
    """A complete generation covers every accepted non-whitespace character."""
    if not spans or len(spans) > max(1, len(content)):
        raise ValueError("Passage count does not match the accepted source.")
    previous_start = -1
    covered = 0
    for span in spans:
        if not isinstance(span, (tuple, list)) or len(span) != 2:
            raise ValueError("Passage offsets must be pairs.")
        start, end = span
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(content):
            raise ValueError("Passage offsets escaped their accepted parent.")
        if start < previous_start or (start > covered and any(not c.isspace() for c in content[covered:start])):
            raise ValueError("Passage generation has incomplete source coverage.")
        previous_start = start
        covered = max(covered, end)
    if any(not c.isspace() for c in content[covered:]):
        raise ValueError("Passage generation has incomplete source coverage.")


def split_passages(content: str, tokenizer: Any) -> list[tuple[int, int]]:
    """Preserve natural boundaries, using overlapping windows only within oversized sentences."""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("A passage source must contain accepted text.")
    full = tokenizer.encode(content, add_special_tokens=False)
    if len(full.ids) <= SHORT_DOCUMENT_TOKENS:
        if len(tokenizer.encode(" ".join(content.split()), add_special_tokens=False).ids) > SHORT_DOCUMENT_TOKENS:
            raise ValueError("Normalized source exceeded the local model's single-window limit.")
        return [(0, len(content))]
    boundaries = [0]
    for match in _PARAGRAPH_BREAK.finditer(content):
        boundaries.extend((match.start(), match.end()))
    boundaries.append(len(content))
    spans: list[tuple[int, int]] = []
    for left, right in zip(boundaries[::2], boundaries[1::2]):
        while left < right and content[left].isspace():
            left += 1
        while right > left and content[right - 1].isspace():
            right -= 1
        if left == right:
            continue
        paragraph = content[left:right]
        encoding = tokenizer.encode(paragraph, add_special_tokens=False)
        if len(encoding.ids) <= PASSAGE_TOKENS:
            spans.append((left, right))
            continue
        offsets = encoding.offsets
        if len(offsets) != len(encoding.ids) or any(not 0 <= a < b <= len(paragraph) for a, b in offsets):
            raise ValueError("Local tokenizer returned invalid source offsets.")
        ends = [end for _, end in offsets]
        sentence_ends = sorted({bisect.bisect_right(ends, match.end()) for match in _SENTENCE_END.finditer(paragraph)})
        start = 0
        while start < len(offsets):
            ceiling = min(len(offsets), start + PASSAGE_TOKENS)
            position = bisect.bisect_right(sentence_ends, ceiling)
            sentence_end = sentence_ends[position - 1] if position else 0
            natural_boundary = sentence_end > start
            end = sentence_end if natural_boundary else ceiling
            # Tokenizers may ignore source characters. Include the boundary gap
            # in the preceding span so accepted text is never silently omitted.
            span_left = left if start == 0 else left + offsets[start][0]
            span_right = right if end == len(offsets) else left + offsets[end][0]
            spans.append((span_left, span_right))
            if end == len(offsets):
                break
            start = end if natural_boundary else max(start + 1, end - OVERSIZED_SENTENCE_OVERLAP)
    validate_spans(content, spans)
    for start, end in spans:
        cleaned = " ".join(content[start:end].split())
        if len(tokenizer.encode(cleaned, add_special_tokens=False).ids) > SHORT_DOCUMENT_TOKENS:
            raise ValueError("A passage exceeded the local model's single-window limit.")
    return spans

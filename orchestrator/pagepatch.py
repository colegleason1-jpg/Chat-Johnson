"""Patch mode for a large page on the canvas: SEARCH/REPLACE edit blocks applied in Python.

A page over PATCH_THRESHOLD_CHARS cannot be rewritten whole inside the answer length limit without being cut,
and a cut rewrite is the spiral the audit describes. So when the operator asks for a change to such a page the
model is told to return edit blocks only:

    <<<<<<< SEARCH
    the exact lines to find in the current page
    =======
    the lines that replace them
    >>>>>>> REPLACE

The app applies them here (exact match first, then whitespace-tolerant), reports the blocks that matched nothing,
and puts the merged page on the canvas. "Fix one thing at a time" then means exactly that.
"""
from __future__ import annotations

import re
from typing import List, Tuple

PATCH_THRESHOLD_CHARS = 6_000
_BLOCK_RE = re.compile(r"<{7} SEARCH[ \t]*\n(?P<search>.*?)\n?={7}[ \t]*\n(?P<replace>.*?)\n?>{7} REPLACE", re.S)
_FRESH_RE = re.compile(r"\b(?:rewrite (?:it|the page|everything|from scratch)|start over|from scratch|a new page|new page|redo the whole|whole new)\b", re.I)

PATCH_RULES = (
    "PATCH MODE: the current page is large, so do NOT return the whole page (it would be cut at the answer length "
    "limit). Return only edit blocks, each in this exact form:\n"
    "<<<<<<< SEARCH\n<the exact lines to find in the current page, copied verbatim>\n=======\n<the lines that replace them>\n"
    ">>>>>>> REPLACE\n"
    "Rules: every SEARCH text must appear in the current page exactly once, copied character for character (indentation "
    "included); keep each block small (the lines that change plus one or two lines of context); use several blocks for "
    "several places; to insert, search for the line before the insertion point and replace it with itself plus the new "
    "lines; never put ``` fences around the blocks; after the blocks, one plain sentence saying what changed."
)
CURRENT_PAGE_HEADER_PATCH = "CURRENT PAGE (the one on the canvas; it is large: answer with SEARCH/REPLACE edit blocks only, never the whole page):"


def wants_patch_mode(prompt: str, page: str) -> bool:
    """Edits to a page over the threshold go through patch mode unless the operator asks for a fresh page."""
    if len(page or "") <= PATCH_THRESHOLD_CHARS:
        return False
    return not _FRESH_RE.search(prompt or "")


def parse_edits(text: str) -> List[Tuple[str, str]]:
    """The (search, replace) pairs in an answer, in order; an answer without blocks gives an empty list."""
    return [(match.group("search"), match.group("replace")) for match in _BLOCK_RE.finditer(text or "")]


def _loose_pattern(search: str) -> re.Pattern:
    """Whitespace-tolerant form of a search text: runs of blanks match any run, line ends match any spacing."""
    parts = [re.escape(token) for token in search.split()]
    return re.compile(r"\s+".join(parts)) if parts else re.compile(r"(?!x)x")


def apply_edits(page: str, edits: List[Tuple[str, str]]) -> Tuple[str, List[str]]:
    """Apply the edits in order; returns the merged page and a plain problem per block that could not be applied.

    An exact, unique match wins; a search that matches several places is refused (the model must add context); a
    search that matches nowhere exactly is tried with whitespace folded before it is reported.
    """
    merged = page
    problems: List[str] = []
    for index, (search, replace) in enumerate(edits, start=1):
        if not search.strip():
            problems.append(f"edit {index}: the SEARCH part is empty")
            continue
        count = merged.count(search)
        if count == 1:
            merged = merged.replace(search, replace, 1)
            continue
        if count > 1:
            problems.append(f"edit {index}: the SEARCH text appears {count} times; it needs more surrounding lines to be unique")
            continue
        matches = list(_loose_pattern(search).finditer(merged))
        if len(matches) == 1:
            start, end = matches[0].span()
            merged = merged[:start] + replace + merged[end:]
        elif len(matches) > 1:
            problems.append(f"edit {index}: the SEARCH text (ignoring spacing) appears {len(matches)} times; it needs more context")
        else:
            first = search.strip().splitlines()[0][:80] if search.strip() else ""
            problems.append(f"edit {index}: nothing in the page matches the SEARCH text starting \"{first}\"")
    return merged, problems

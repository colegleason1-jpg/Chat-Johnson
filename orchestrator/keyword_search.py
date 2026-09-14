"""Keyword search for the studio's pick-lists: every word must match, any order, best matches first.

Used wherever the operator types to find something the session already knows about: the
repositories a token can see, locked artifacts, chats. Deterministic, no provider call.
"""
from __future__ import annotations

import re
from typing import Callable, Iterable, List, Sequence, Tuple, TypeVar

T = TypeVar("T")

_WORD = re.compile(r"[a-z0-9]+")


def keywords(query: str) -> List[str]:
    return _WORD.findall((query or "").lower())


def _score(text: str, words: Sequence[str]) -> Tuple[int, int, int]:
    """Lower is better: (words not at a word boundary, first match position, length)."""
    lowered = text.lower()
    boundary_misses = 0
    first = len(lowered)
    for word in words:
        position = lowered.find(word)
        first = min(first, position)
        if not (position == 0 or not lowered[position - 1].isalnum()):
            boundary_misses += 1
    return boundary_misses, first, len(lowered)


def keyword_rank(query: str, candidates: Iterable[T], key: Callable[[T], str] = str, limit: int = 20) -> List[T]:
    """Candidates whose text contains every keyword (any order), best matches first; all candidates when the query is empty."""
    words = keywords(query)
    items = list(candidates)
    if not words:
        return items[: max(1, int(limit))]
    matched = [(item, _score(key(item), words)) for item in items if all(word in key(item).lower() for word in words)]
    matched.sort(key=lambda pair: pair[1])
    return [item for item, _ in matched[: max(1, int(limit))]]


def like_clauses(query: str, columns: Sequence[str]) -> Tuple[str, List[str]]:
    """SQL: every keyword must appear in at least one of the columns. Returns (clause, params)."""
    words = keywords(query)
    if not words:
        return "1=1", []
    per_word = "(" + " OR ".join(f"{column} LIKE ?" for column in columns) + ")"
    clause = " AND ".join(per_word for _ in words)
    params = [f"%{word}%" for word in words for _ in columns]
    return clause, params

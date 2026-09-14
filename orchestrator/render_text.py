"""Text flow helpers for the chat surface: fences held whole while streaming, and math-safe markdown."""
from __future__ import annotations

import re
from typing import Iterable, Iterator

_FENCE = "```"
_CURRENCY_RE = re.compile(r"(?<![\\$\w])\$(?=\d)")


def hold_fences(chunks: Iterable[str]) -> Iterator[str]:
    """Stream prose as it arrives, but emit a fenced code block only once it closes.

    Re-rendering a half-open fence on every chunk is what made long code answers flicker and
    crawl; holding the block until its closing fence keeps prose live and code atomic.
    """
    buffer = ""
    inside = False
    for chunk in chunks:
        buffer += chunk
        while buffer:
            if not inside:
                index = buffer.find(_FENCE)
                if index < 0:
                    hold = 2 if buffer.endswith("``") else 1 if buffer.endswith("`") else 0  # a marker split across chunks
                    emit, buffer = buffer[: len(buffer) - hold], buffer[len(buffer) - hold:]
                    if emit:
                        yield emit
                    break
                prose = buffer[:index]
                if prose:
                    yield prose
                buffer = buffer[index:]
                inside = True
            else:
                closing = buffer.find(_FENCE, len(_FENCE))
                if closing < 0:
                    break
                block_end = buffer.find("\n", closing + len(_FENCE))
                block_end = len(buffer) if block_end < 0 else block_end + 1
                yield buffer[:block_end]
                buffer = buffer[block_end:]
                inside = False
    if buffer:
        yield buffer


def prepare_markdown(text: str) -> str:
    """Escape currency dollars so Streamlit's KaTeX does not pair them as math; real ``$…$`` and ``$$…$$`` stay intact."""
    if "$" not in text:
        return text
    return _CURRENCY_RE.sub(r"\\$", text)

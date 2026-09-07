"""Repository ingestion: serialize a codebase into a budgeted context block.

Produces a deterministic file map + per-file contents, truncating the least
important files first so a large-context model (e.g. Gemini) gets global
visibility without blowing the window.
"""
from __future__ import annotations

import os
from typing import Dict, List, Tuple

SKIP_DIRS = {
    ".git", ".orchestrator", "__pycache__", "node_modules", ".venv",
    "venv", ".mypy_cache", ".pytest_cache", "dist", "build", ".ruff_cache",
}
MAX_FILE_BYTES = 120_000

TEXT_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".md", ".txt", ".yml",
    ".yaml", ".toml", ".cfg", ".ini", ".css", ".html", ".sh", ".sql",
    ".env.example", "",
}


def _is_text(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    if ext in TEXT_EXTS:
        return True
    try:
        with open(path, "rb") as f:
            return b"\x00" not in f.read(2048)
    except OSError:
        return False


def walk_repo(root: str) -> List[str]:
    """Deterministic list of text files under root."""
    files: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            if _is_text(full) and os.path.getsize(full) <= MAX_FILE_BYTES:
                files.append(rel)
    return files


def _score(rel: str) -> int:
    """Higher = more important; keep entry points and configs longest."""
    pri = ("requirements", "package.json", "main", "app", "server", "config", "settings")
    return sum(2 for p in pri if p in os.path.basename(rel).lower()) + (
        1 if rel.endswith(".py") else 0
    )


def serialize_repo(root: str, token_budget: int = 600_000) -> Tuple[str, Dict[str, int]]:
    """Build one context string: file map first, then file contents.

    Returns (context_text, stats) where stats has file_count and est_tokens.
    """
    files = walk_repo(root)
    file_map = "\n".join(f"- {rel}" for rel in files)
    header = (
        f"REPOSITORY SNAPSHOT (root: {os.path.abspath(root)})\n"
        f"{len(files)} files. File map:\n{file_map}\n\n"
    )
    budget_chars = token_budget * 4 - len(header)
    parts: List[str] = []
    consumed = 0
    for rel in sorted(files, key=lambda r: (-_score(r), r)):
        try:
            with open(os.path.join(root, rel), "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            continue
        block = f"\n===== FILE: {rel} =====\n{content}\n"
        if consumed + len(block) > budget_chars:
            block = block[: max(0, budget_chars - consumed)] + "\n... [truncated]\n"
            parts.append(block)
            consumed = budget_chars
            break
        parts.append(block)
        consumed += len(block)
    text = header + "".join(parts)
    return text, {"file_count": len(files), "est_tokens": len(text) // 4}

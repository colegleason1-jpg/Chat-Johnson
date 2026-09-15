"""Deterministic patch assembly: never trust a model with whole-repo rewrites.

The model is instructed to emit one of:
1. FILE blocks (preferred for targeted edits):

       path/relative/to/root
       <complete new file content>

2. A unified diff inside a diff-fenced code block (applied with git apply).

Parsing is fully deterministic; anything unrecognized is rejected so the
feedback loop can ask the model to reformat.
"""
from __future__ import annotations

import os
import re
import subprocess
from typing import Dict, List, Optional, Tuple

FILE_BLOCK_RE = re.compile(
    r"```file:\s*(?P<path>[^\n`]+)\n(?P<body>.*?)```", re.DOTALL
)
DIFF_BLOCK_RE = re.compile(r"```diff\s*\n(?P<body>.*?)```", re.DOTALL)
# Any fenced block, with its info string and the two lines above it (models name the file there in many ways).
ANY_FENCE_RE = re.compile(r"(?P<lead>(?:[^\n]*\n){0,2})```(?P<info>[^\n]*)\n(?P<body>.*?)\n?```", re.DOTALL)
_EXTENSIONLESS = {"Dockerfile", "Makefile", "LICENSE", "Procfile", "Jenkinsfile", "Vagrantfile", ".gitignore", ".dockerignore",
                  ".env.example", ".editorconfig", ".gitattributes", "CODEOWNERS"}
_PATH_TOKEN = re.compile(r"(?<![\w./-])((?:[\w.@-]+/)*[\w.@-]+)(?![\w/-])")
_NOT_A_FILE_INFO = {"diff", "output", "console", "text", "txt", "plaintext", "log", "mermaid", ""}

PATCH_INSTRUCTIONS = """OUTPUT FORMAT (mandatory):
- Emit every file you create or change as its own fenced block that names the path, exactly like:
```file: relative/path/from/repo/root.py
<complete new content of that file>
```
- Write the complete content of each file. Never use placeholders like "... rest unchanged ...".
- Folders exist only through file paths, so give every folder at least one file (a README.md or __init__.py is fine).
- Prefer many small file blocks over one giant rewrite. Do not wrap the blocks in prose that repeats them.
- Example for two files:
```file: README.md
# Project
```
```file: src/app.py
print("hello")
```
"""


def _looks_like_path(token: str) -> bool:
    token = token.strip().strip("`*_\"'")
    if not token or " " in token or not _safe_relpath(token):
        return False
    name = token.rsplit("/", 1)[-1]
    if name in _EXTENSIONLESS or name.startswith(".") and len(name) > 1:
        return True
    return bool(re.search(r"\.[A-Za-z0-9]{1,8}$", name)) and not token.endswith(".")


def _path_from_info(info: str) -> str:
    """```file: x, ```python title="x", ```python:x, ```python x, ```x  → x."""
    info = info.strip()
    m = re.search(r"(?:title|filename|file|path)\s*[=:]\s*[\"']?([^\"'\s]+)", info, re.I)
    if m and _looks_like_path(m.group(1)):
        return m.group(1)
    for part in re.split(r"[\s:]+", info):
        if _looks_like_path(part) and part not in _NOT_A_FILE_INFO:
            return part.strip("`")
    return ""


def _path_from_lead(lead: str) -> str:
    """A heading, bold name, backticked name, or 'File: x' line just above the fence."""
    for line in reversed([ln.strip() for ln in lead.splitlines() if ln.strip()]):
        cleaned = re.sub(r"^(?:#+\s*|\*\*|file\s*[:=]\s*|path\s*[:=]\s*|\d+[.)]\s*|[-*]\s*)+", "", line, flags=re.I).strip().rstrip(":").strip("`*_ ")
        if _looks_like_path(cleaned):
            return cleaned
        for token in _PATH_TOKEN.findall(line):
            if "/" in token and _looks_like_path(token):
                return token
        return ""
    return ""


def _path_from_first_line(body: str) -> Tuple[str, str]:
    """A first-line comment naming the file: '# src/app.py', '// x.js', '<!-- x.html -->', '/* x.css */'."""
    first, _, rest = body.partition("\n")
    m = re.match(r"^\s*(?:#|//|<!--|/\*|--|;)\s*(?:file\s*:\s*)?([\w.@/-]+)\s*(?:-->|\*/)?\s*$", first, re.I)
    if m and "/" in m.group(1) and _looks_like_path(m.group(1)):
        return m.group(1), rest
    return "", body


def parse_file_blocks(text: str) -> Dict[str, str]:
    """Extract {relative_path: full_new_content} from fenced blocks that name a file.

    The canonical form is ```file: path. Models also write ```python title="path", a heading or
    bold path just above the fence, or a first-line comment with the path; all are accepted so a
    correct answer in a different dialect is not thrown away. Fences that name no file are ignored.
    """
    patches: Dict[str, str] = {}
    for m in FILE_BLOCK_RE.finditer(text):
        path = m.group("path").strip()
        body = m.group("body")
        if body.startswith("\n"):
            body = body[1:]
        if not body.endswith("\n"):
            body += "\n"
        if _safe_relpath(path):
            patches[path] = body
    for m in ANY_FENCE_RE.finditer(text):
        info = m.group("info").strip()
        if info.lower().startswith("file:") or info.lower().startswith("diff"):
            continue
        body = m.group("body")
        path = _path_from_info(info) or _path_from_lead(m.group("lead"))
        if not path:
            path, body = _path_from_first_line(body)
        if not path or not _safe_relpath(path) or path in patches:
            continue
        if not body.endswith("\n"):
            body += "\n"
        patches[path] = body
    return patches


_ELISION_RE = re.compile(r"^\s*(?:#|//|/\*|<!--)?\s*(?:\.\.\.|…)\s*(?:rest|existing|unchanged|omitted|snip|same as before|remaining|other code)?", re.I | re.M)
_TOPLEVEL_RE = re.compile(r"^(?:def |class |async def )", re.M)


def looks_like_snippet(path: str, body: str, existing: Optional[str]) -> str:
    """Why a fenced body must not replace ``path`` wholesale ("" when it is a complete file).

    A model that answers with an excerpt ("... rest unchanged", a lone function from a module that
    has several) would otherwise overwrite the whole file with the excerpt.
    """
    lines = [line for line in body.splitlines() if line.strip()]
    if any(_ELISION_RE.match(line) and len(line.strip()) <= 60 for line in lines):
        return f"{path}: block elides part of the file ('...'); a complete file is required"
    if existing is None or not path.endswith(".py"):
        return ""
    had_defs = len(_TOPLEVEL_RE.findall(existing))
    has_defs = len(_TOPLEVEL_RE.findall(body))
    if had_defs >= 2 and has_defs < had_defs and len(body) < 0.4 * len(existing):
        return f"{path}: block holds {has_defs} of the file's {had_defs} top-level definitions and is much shorter; looks like an excerpt"
    return ""


def reject_snippets(root: str, patches: Dict[str, str]) -> Tuple[Dict[str, str], List[str]]:
    """Split parsed blocks into (safe to write, reasons for the rejected ones) against the files on disk."""
    accepted: Dict[str, str] = {}
    rejected: List[str] = []
    for path, body in patches.items():
        full = os.path.join(root, path)
        existing: Optional[str] = None
        if os.path.isfile(full):
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as handle:
                    existing = handle.read()
            except OSError:
                existing = None
        reason = looks_like_snippet(path, body, existing)
        if reason:
            rejected.append(reason)
        else:
            accepted[path] = body
    return accepted, rejected


def parse_diff_blocks(text: str) -> List[str]:
    return [m.group("body") for m in DIFF_BLOCK_RE.finditer(text)]


FORBIDDEN_SEGMENTS = frozenset({".git", ".orchestrator", ".hg", ".svn", ".orch_source"})
# Git must never read hooks, fsmonitor, or external diff tools from a tree a model wrote into.
GIT_SAFE = ("-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null", "-c", "diff.external=", "-c", "core.pager=cat")


def _safe_relpath(path: str) -> bool:
    """Reject absolute paths, traversal, and anything under a VCS or orchestrator directory."""
    if not path or path.startswith(("/", "~")):
        return False
    segments = path.replace("\\", "/").split("/")
    if ".." in segments or any(segment.lower() in FORBIDDEN_SEGMENTS for segment in segments):
        return False
    drive = os.path.splitdrive(path)[0]
    return not drive


_DIFF_PATH = re.compile(r"^(?:diff --git a/(?P<a>[^\t\n]+?) b/(?P<b>[^\t\n]+)|\+\+\+ (?P<plus>[^\t\n]+))", re.M)


def diff_paths(diff: str) -> List[str]:
    """Paths a unified diff touches (from ``diff --git`` and ``+++`` headers), for the syntax guardrail."""
    seen: List[str] = []
    for match in _DIFF_PATH.finditer(diff or ""):
        path = (match.group("b") or match.group("plus") or "").strip()
        if path in ("/dev/null", ""):
            continue
        if path.startswith(("a/", "b/")):
            path = path[2:]
        if path not in seen and _safe_relpath(path):
            seen.append(path)
    return seen


def apply_file_blocks(root: str, patches: Dict[str, str]) -> List[str]:
    """Write files deterministically. Returns list of written relative paths."""
    written: List[str] = []
    for rel, content in patches.items():
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        written.append(rel)
    return written


def apply_unified_diffs(root: str, diffs: List[str]) -> Tuple[List[str], List[str]]:
    """Apply diff blocks with `git apply`. Returns (applied, failed_msgs)."""
    applied: List[str] = []
    failed: List[str] = []
    for i, diff in enumerate(diffs):
        proc = subprocess.run(
            ["git", *GIT_SAFE, "apply", "--whitespace=fix", "-"],
            cwd=root, input=diff, capture_output=True, text=True, timeout=60,
        )
        if proc.returncode == 0:
            applied.append(f"diff[{i}]")
        else:
            failed.append(f"diff[{i}]: {proc.stderr.strip()[:300]}")
    return applied, failed


def is_git_sandbox(root: str) -> bool:
    """A git checkout (``.git`` directory) or a worktree (``.git`` file pointing at the main repository)."""
    return os.path.exists(os.path.join(root, ".git"))


def changed_files(root: str) -> List[str]:
    """All files modified vs HEAD (git) — used to scope ast validation."""
    if not is_git_sandbox(root):
        return []
    proc = subprocess.run(
        ["git", *GIT_SAFE, "status", "--porcelain"],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    out = []
    for line in proc.stdout.splitlines():
        if len(line) > 3 and line[:2].strip():  # modified, added, renamed, or untracked (a diff can create files too)
            rel = line[3:].strip().strip('"')
            if " -> " in rel:
                rel = rel.split(" -> ", 1)[1].strip('"')
            out.append(rel)
    return out

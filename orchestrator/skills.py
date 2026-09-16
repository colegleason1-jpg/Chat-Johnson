"""Skills with progressive disclosure: markdown playbooks loaded into the prompt only when their keywords match.

A skill file has a small front matter block (name, description, keywords) and a body of at most a
few hundred words. Nothing is loaded for a prompt that matches no skill, so the static prefix stays
small; a matching skill costs its body once.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import List, Optional, Sequence, Tuple

from .keyword_search import keywords

SKILLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills")
MAX_BODY_CHARS = 2_000
_FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    keywords: Tuple[str, ...]
    body: str
    path: str


def parse_skill(text: str, path: str = "") -> Optional[Skill]:
    match = _FRONT.match(text)
    if not match:
        return None
    meta = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        if key.strip():
            meta[key.strip().lower()] = value.strip()
    name = meta.get("name", "").strip()
    if not name:
        return None
    words = tuple(dict.fromkeys(w for w in keywords(meta.get("keywords", "")) if len(w) >= 3))
    body = text[match.end():].strip()[:MAX_BODY_CHARS]
    return Skill(name=name, description=meta.get("description", ""), keywords=words, body=body, path=path)


@lru_cache(maxsize=8)
def load_skills(root: str = SKILLS_DIR) -> Tuple[Skill, ...]:
    found: List[Skill] = []
    if not os.path.isdir(root):
        return ()
    for entry in sorted(os.listdir(root)):
        if not entry.endswith(".md"):
            continue
        path = os.path.join(root, entry)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                skill = parse_skill(handle.read(), path)
        except OSError:
            continue
        if skill:
            found.append(skill)
    return tuple(found)


# A skill written for one workspace must not steer another: the repository skill demands whole-file blocks that
# a canvas page cannot use, and the company skill demands a 200-word board report. Unlisted skills apply anywhere.
WORKSPACE_ONLY = {"repository-patching": ("repository",), "company-reporting": ("company",)}
AUTOMATIC_PREFIX = "AUTOMATIC "  # an app-authored turn (a sandbox fix, a continuation) carries its own contract


def select_skills(
    prompt: str, limit: int = 2, root: str = SKILLS_DIR, skills: Optional[Sequence[Skill]] = None, workspace: str = "",
) -> List[Skill]:
    """Skills whose keywords appear in the prompt as whole words, best match first; none when nothing matches.

    ``workspace`` keeps workspace-bound skills out of the others; an app-authored turn selects no skill at all.
    """
    if (prompt or "").lstrip().startswith(AUTOMATIC_PREFIX):
        return []
    words = set(keywords(prompt))
    scored = []
    for skill in (skills if skills is not None else load_skills(root)):
        allowed = WORKSPACE_ONLY.get(skill.name)
        if allowed and workspace and workspace not in allowed:
            continue
        hits = sum(1 for k in skill.keywords if k in words)
        if hits:
            scored.append((hits, skill.name, skill))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [skill for _, _, skill in scored[: max(0, int(limit))]]


def skills_block(prompt: str, limit: int = 2, root: str = SKILLS_DIR, workspace: str = "") -> Tuple[str, List[str]]:
    """The text to append to the system prompt and the names it carries."""
    chosen = select_skills(prompt, limit=limit, root=root, workspace=workspace)
    if not chosen:
        return "", []
    parts = [f"SKILL · {s.name}: {s.description}\n{s.body}" for s in chosen]
    return "APPLICABLE SKILLS (follow when relevant):\n" + "\n\n".join(parts), [s.name for s in chosen]

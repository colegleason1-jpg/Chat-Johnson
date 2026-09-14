"""Hub connector: read a GitHub repository into a temporary sandbox through the REST API.

No git binary is needed: the tarball endpoint streams the tree at one commit, which is
extracted with path checks (no absolute paths, no '..', no links) under the staging root.
Public repositories need no token; private ones use the session-only push token. The
sandbox pipeline then runs on the extracted tree in copy mode, and the resulting diff can
be pushed back as a branch plus pull request through the same slot.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import tarfile
import tempfile
import time
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import requests

from .github_push import API_ROOT, API_VERSION, parse_owner_repo

MAX_TARBALL_BYTES = 200 * 1024 * 1024
TEXT_LIMIT_BYTES = 2 * 1024 * 1024


class GitHubRepoError(Exception):
    pass


@dataclass
class FetchedRepo:
    owner: str
    repo: str
    ref: str
    sha: str
    path: str
    files: int
    size_bytes: int

    def as_dict(self) -> Dict[str, object]:
        return asdict(self)


def staging_root() -> str:
    root = os.path.join(tempfile.gettempdir(), "chatjohnson-repos")
    os.makedirs(root, exist_ok=True)
    return root


def _headers(token: str) -> Dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _redact(text: str, token: str) -> str:
    return text.replace(token, "[REDACTED_TOKEN]") if token else text


def _get(url: str, token: str, stream: bool = False, timeout: int = 60) -> requests.Response:
    try:
        response = requests.get(url, headers=_headers(token), stream=stream, timeout=timeout, allow_redirects=True)
    except requests.RequestException as exc:
        raise GitHubRepoError(_redact(f"GitHub network error: {exc}", token)) from None
    if response.status_code >= 400:
        detail = _redact((getattr(response, "text", "") or "")[:200], token)
        hint = " (private repository? arm GitHub push with a token that can read it)" if response.status_code in (401, 403, 404) and not token else ""
        raise GitHubRepoError(f"GitHub GET {url.replace(API_ROOT, '')} -> HTTP {response.status_code}: {detail}{hint}")
    return response


def resolve_sha(owner_repo: str, ref: str = "", token: str = "") -> Tuple[str, str]:
    """(ref, sha): the default branch is used when ``ref`` is empty."""
    owner, repo = parse_owner_repo(owner_repo)
    chosen = (ref or "").strip()
    if not chosen:
        data = _get(f"{API_ROOT}/repos/{owner}/{repo}", token).json()
        chosen = str(data.get("default_branch") or "main")
    data = _get(f"{API_ROOT}/repos/{owner}/{repo}/commits/{chosen}", token).json()
    sha = str(data.get("sha") or "")
    if not re.fullmatch(r"[0-9a-f]{7,64}", sha):
        raise GitHubRepoError(f"could not resolve {chosen} to a commit")
    return chosen, sha


def looks_like_owner_repo(value: str) -> bool:
    try:
        parse_owner_repo(value)
        return True
    except Exception:
        return False


def whoami(token: str) -> str:
    """The login the token belongs to (GET /user); empty when the token cannot read it."""
    if not (token or "").strip():
        return ""
    data = _get(f"{API_ROOT}/user", token).json()
    return str(data.get("login") or "") if isinstance(data, dict) else ""


def qualify_repository(value: str, login: str) -> str:
    """'Chat-Johnson' becomes '<login>/Chat-Johnson' when the operator is known; owner/name passes through."""
    cleaned = (value or "").strip().removeprefix("https://github.com/").removesuffix(".git").strip("/")
    if cleaned and "/" not in cleaned and login:
        return f"{login}/{cleaned}"
    return cleaned


def list_repositories(token: str, limit: int = 100) -> List[str]:
    """Repositories the token can see (owner, collaborator, org member), newest activity first, as owner/name."""
    if not (token or "").strip():
        raise GitHubRepoError("a token is required to list repositories")
    data = _get(f"{API_ROOT}/user/repos?per_page={max(1, min(int(limit), 100))}&sort=updated&affiliation=owner,collaborator,organization_member", token).json()
    names = [str(item.get("full_name") or "") for item in data if isinstance(item, dict)]
    return [name for name in names if name]


def _safe_member(member: tarfile.TarInfo) -> Optional[str]:
    """Relative path with the archive's top-level directory stripped, or None when the member must be skipped."""
    if not (member.isfile() or member.isdir()):
        return None  # links and devices never land in the sandbox
    parts = [part for part in member.name.replace("\\", "/").split("/") if part not in ("", ".")]
    if len(parts) < 2 or any(part == ".." for part in parts) or member.name.startswith("/"):
        return None
    return "/".join(parts[1:])


def extract_tarball(tar_bytes: bytes, dest: str) -> Tuple[int, int]:
    """Extract safely, stripping the top directory. Returns (files, bytes)."""
    files = size = 0
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as archive:
        for member in archive.getmembers():
            rel = _safe_member(member)
            if rel is None:
                continue
            target = os.path.join(dest, rel)
            if not os.path.realpath(target).startswith(os.path.realpath(dest) + os.sep):
                continue
            if member.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                continue
            with open(target, "wb") as handle:
                shutil.copyfileobj(source, handle)
            files += 1
            size += int(member.size)
    return files, size


def fetch_tree(owner_repo: str, ref: str = "", token: str = "", max_bytes: int = MAX_TARBALL_BYTES) -> FetchedRepo:
    """Download one commit's tree into a fresh directory under the staging root."""
    owner, repo = parse_owner_repo(owner_repo)
    chosen, sha = resolve_sha(owner_repo, ref, token)
    response = _get(f"{API_ROOT}/repos/{owner}/{repo}/tarball/{sha}", token, stream=True, timeout=120)
    chunks: List[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=1 << 16):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise GitHubRepoError(f"repository tarball exceeds {max_bytes // (1024 * 1024)} MB")
        chunks.append(chunk)
    dest = tempfile.mkdtemp(prefix=f"{repo}-{sha[:7]}-", dir=staging_root())
    files, size = extract_tarball(b"".join(chunks), dest)
    if files == 0:
        raise GitHubRepoError("the tarball contained no files")
    return FetchedRepo(owner=owner, repo=repo, ref=chosen, sha=sha, path=dest, files=files, size_bytes=size)


def prune_staging(max_age_seconds: int = 6 * 3600) -> int:
    """Drop fetched trees older than ``max_age_seconds``; returns how many were removed."""
    removed = 0
    root = staging_root()
    for name in os.listdir(root):
        path = os.path.join(root, name)
        try:
            if os.path.isdir(path) and time.time() - os.path.getmtime(path) > max_age_seconds:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


_DIFF_HEADER = re.compile(r"^(?:diff --git a/(?P<a>\S+) b/(?P<b>\S+)|\+\+\+ (?P<plus>\S+))", re.M)


def changed_paths_from_diff(diff: str) -> List[str]:
    """Paths named by ``diff --git`` or ``+++`` headers (git and copy-mode diffs), in order, without a/ b/ prefixes."""
    seen: List[str] = []
    for match in _DIFF_HEADER.finditer(diff or ""):
        path = match.group("b") or match.group("plus") or ""
        if path in ("/dev/null", ""):
            continue
        if path.startswith("b/") or path.startswith("a/"):
            path = path[2:]
        if path not in seen:
            seen.append(path)
    return seen


def collect_changed_files(sandbox_path: str, diff: str) -> Tuple[List[Tuple[str, str]], List[str]]:
    """(files to push as (path, text), paths that no longer exist). Binary or huge files are left out."""
    pairs: List[Tuple[str, str]] = []
    missing: List[str] = []
    for rel in changed_paths_from_diff(diff):
        full = os.path.join(sandbox_path, rel)
        if not os.path.isfile(full):
            missing.append(rel)
            continue
        if os.path.getsize(full) > TEXT_LIMIT_BYTES:
            missing.append(rel)
            continue
        with open(full, "rb") as handle:
            raw = handle.read()
        try:
            pairs.append((rel, raw.decode("utf-8")))
        except UnicodeDecodeError:
            missing.append(rel)
    return pairs, missing


def describe(fetched: Sequence[Dict[str, object]]) -> str:
    return ", ".join(f"{item['owner']}/{item['repo']}@{str(item['sha'])[:7]}" for item in fetched)

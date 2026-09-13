"""Session-only GitHub push: one commit on a new branch plus an opened pull request, on a button press.

The token is pasted per session and never stored; every error message has it redacted. The
default branch is never written to. A push is recorded so the same session can open a revert
pull request that restores or removes exactly the files it pushed.
"""
from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"


class GitHubPushError(Exception):
    pass


@dataclass
class PushRecord:
    owner: str
    repo: str
    base_branch: str
    base_sha: str
    branch: str
    commit_sha: str
    pr_number: int
    pr_url: str
    files: List[str]
    previous: Dict[str, Optional[str]] = field(default_factory=dict)  # path -> blob sha at base, None when new
    kind: str = "push"  # push | revert


def parse_owner_repo(value: str) -> Tuple[str, str]:
    cleaned = (value or "").strip().removeprefix("https://github.com/").removesuffix(".git").strip("/")
    match = re.fullmatch(r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", cleaned)
    if not match:
        raise GitHubPushError("repository must be owner/name")
    return match.group(1), match.group(2)


def branch_name_for(app_name: str, files: Sequence[Tuple[str, str]]) -> str:
    digest = hashlib.sha256("".join(f"{path}\n{body}" for path, body in sorted(files)).encode("utf-8")).hexdigest()[:8]
    return f"deploy-kit/{re.sub(r'[^a-z0-9-]+', '-', app_name.lower()).strip('-') or 'app'}-{digest}"


class GitHubWriter:
    def __init__(self, token: str, owner_repo: str, timeout: int = 30) -> None:
        self.token = (token or "").strip()
        if not self.token:
            raise GitHubPushError("a GitHub token is required")
        self.owner, self.repo = parse_owner_repo(owner_repo)
        self.timeout = timeout

    # ------------------------------------------------------------------ transport
    def _redact(self, text: str) -> str:
        return text.replace(self.token, "[REDACTED_TOKEN]")

    def _request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None, ok: Sequence[int] = (200, 201)) -> Any:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        }
        try:
            response = requests.request(method, f"{API_ROOT}{path}", headers=headers, json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            raise GitHubPushError(self._redact(f"GitHub network error: {exc}")) from None
        if response.status_code == 404 and method == "GET":
            return None
        if response.status_code not in ok:
            detail = getattr(response, "text", "") or ""
            raise GitHubPushError(self._redact(f"GitHub {method} {path} -> HTTP {response.status_code}: {detail[:300]}"))
        try:
            return response.json()
        except ValueError:
            return {}

    def _repo(self, path: str) -> str:
        return f"/repos/{self.owner}/{self.repo}{path}"

    # ------------------------------------------------------------------ reads
    def default_branch(self) -> str:
        data = self._request("GET", self._repo(""))
        if not data:
            raise GitHubPushError(f"repository {self.owner}/{self.repo} not found or the token cannot see it")
        return str(data.get("default_branch") or "main")

    def branch_sha(self, branch: str) -> str:
        data = self._request("GET", self._repo(f"/git/ref/heads/{branch}"))
        if not data:
            raise GitHubPushError(f"branch {branch} not found")
        return str(data["object"]["sha"])

    def commit_tree(self, commit_sha: str) -> str:
        data = self._request("GET", self._repo(f"/git/commits/{commit_sha}"))
        return str(data["tree"]["sha"])

    def blob_sha_at(self, path: str, ref: str) -> Optional[str]:
        data = self._request("GET", self._repo(f"/contents/{path}?ref={ref}"))
        return str(data["sha"]) if isinstance(data, dict) and data.get("sha") else None

    # ------------------------------------------------------------------ writes
    def create_blob(self, content: str) -> str:
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        return str(self._request("POST", self._repo("/git/blobs"), {"content": encoded, "encoding": "base64"})["sha"])

    def create_tree(self, base_tree: str, entries: List[Dict[str, Any]]) -> str:
        return str(self._request("POST", self._repo("/git/trees"), {"base_tree": base_tree, "tree": entries})["sha"])

    def create_commit(self, message: str, tree: str, parent: str) -> str:
        return str(self._request("POST", self._repo("/git/commits"), {"message": message, "tree": tree, "parents": [parent]})["sha"])

    def create_branch(self, branch: str, sha: str) -> None:
        self._request("POST", self._repo("/git/refs"), {"ref": f"refs/heads/{branch}", "sha": sha})

    def open_pull_request(self, head: str, base: str, title: str, body: str) -> Dict[str, Any]:
        return self._request("POST", self._repo("/pulls"), {"title": title, "head": head, "base": base, "body": body})

    # ------------------------------------------------------------------ flows
    def push_files(
        self, files: Sequence[Tuple[str, str]], branch: str, message: str, pr_title: str, pr_body: str, base_branch: str = ""
    ) -> PushRecord:
        """One commit with every file on a new branch off the default branch, then a pull request."""
        if not files:
            raise GitHubPushError("nothing to push")
        base = base_branch or self.default_branch()
        if branch == base:
            raise GitHubPushError("refusing to push to the base branch; use a feature branch")
        base_sha = self.branch_sha(base)
        previous = {path: self.blob_sha_at(path, base) for path, _ in files}
        entries = [{"path": path, "mode": "100755" if path.endswith(".sh") else "100644", "type": "blob", "sha": self.create_blob(body)} for path, body in files]
        tree = self.create_tree(self.commit_tree(base_sha), entries)
        commit = self.create_commit(message, tree, base_sha)
        self.create_branch(branch, commit)
        pull = self.open_pull_request(branch, base, pr_title, pr_body)
        return PushRecord(
            owner=self.owner, repo=self.repo, base_branch=base, base_sha=base_sha, branch=branch, commit_sha=commit,
            pr_number=int(pull.get("number", 0)), pr_url=str(pull.get("html_url", "")), files=[path for path, _ in files], previous=previous,
        )

    def open_revert(self, record: PushRecord) -> PushRecord:
        """Restore the pushed paths to what the base branch had at push time (or delete files that were new)."""
        base = record.base_branch
        head_sha = self.branch_sha(base)
        entries: List[Dict[str, Any]] = []
        for path in record.files:
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": record.previous.get(path)})  # sha None deletes
        tree = self.create_tree(self.commit_tree(head_sha), entries)
        commit = self.create_commit(f"Revert deploy kit push ({record.branch})", tree, head_sha)
        branch = f"revert/{record.branch.split('/', 1)[-1]}"
        self.create_branch(branch, commit)
        pull = self.open_pull_request(
            branch, base, f"Revert: {record.branch}",
            f"Restores {len(record.files)} path(s) to their state before pull request #{record.pr_number} ({record.pr_url}). "
            "Files that did not exist before are deleted; files that did are put back.",
        )
        return PushRecord(
            owner=self.owner, repo=self.repo, base_branch=base, base_sha=head_sha, branch=branch, commit_sha=commit,
            pr_number=int(pull.get("number", 0)), pr_url=str(pull.get("html_url", "")), files=list(record.files), kind="revert",
        )

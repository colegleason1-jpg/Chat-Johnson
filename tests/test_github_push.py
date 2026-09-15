"""Session-only GitHub push: call sequence, payloads, redaction, revert semantics (no network)."""
import base64
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import github_push as gp


class FakeResponse:
    def __init__(self, status, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def make_fake(calls, existing=("docs/RUNBOOK.md",)):
    counter = {"blob": 0, "tree": 0, "commit": 0, "pr": 100}

    def fake_request(method, url, headers=None, json=None, timeout=None):
        path = url.replace(gp.API_ROOT, "")
        calls.append((method, path, json))
        assert headers["Authorization"] == "Bearer ghp_secret_token_123"
        if path == "/repos/me/proj":
            return FakeResponse(200, {"default_branch": "main"})
        if path.startswith("/repos/me/proj/git/ref/heads/"):
            branch = path.rsplit("/", 1)[-1]
            return FakeResponse(200, {"object": {"sha": f"sha-of-{branch}"}}) if branch == "main" else FakeResponse(404, text="nope")
        if path.startswith("/repos/me/proj/git/commits/"):
            return FakeResponse(200, {"tree": {"sha": "tree-of-main"}})
        if path.startswith("/repos/me/proj/git/trees/tree-of-main"):
            return FakeResponse(200, {"tree": [{"path": p, "type": "blob", "sha": f"old-{p}", "mode": "100755" if p.endswith(".sh") else "100644"} for p in existing]})
        if path.startswith("/repos/me/proj/pulls/"):
            return FakeResponse(200, {"number": int(path.rsplit("/", 1)[-1]), "merged": True})
        if path.startswith("/repos/me/proj/contents/"):
            file_path = path.split("/contents/", 1)[1].split("?")[0]
            return FakeResponse(200, {"sha": f"old-{file_path}"}) if file_path in existing else FakeResponse(404, text="missing")
        if path == "/repos/me/proj/git/blobs":
            counter["blob"] += 1
            return FakeResponse(201, {"sha": f"blob{counter['blob']}"})
        if path == "/repos/me/proj/git/trees":
            counter["tree"] += 1
            return FakeResponse(201, {"sha": f"tree{counter['tree']}"})
        if path == "/repos/me/proj/git/commits":
            counter["commit"] += 1
            return FakeResponse(201, {"sha": f"commit{counter['commit']}"})
        if path == "/repos/me/proj/git/refs":
            return FakeResponse(201, {"ref": json["ref"]})
        if path == "/repos/me/proj/pulls":
            counter["pr"] += 1
            return FakeResponse(201, {"number": counter["pr"], "html_url": f"https://github.com/me/proj/pull/{counter['pr']}"})
        return FakeResponse(500, text="unexpected " + path)

    return fake_request


def test_push_makes_one_commit_on_a_new_branch_and_opens_a_pr(monkeypatch):
    calls = []
    monkeypatch.setattr(gp.requests, "request", make_fake(calls))
    writer = gp.GitHubWriter("ghp_secret_token_123", "https://github.com/me/proj.git")
    files = [("Dockerfile", "FROM x\n"), ("scripts/rollback.sh", "#!/bin/bash\n"), ("docs/RUNBOOK.md", "# run\n")]
    record = writer.push_files(files, "deploy-kit/app-abc", "Add kit", "Deploy kit", "body")
    assert record.base_branch == "main" and record.base_sha == "sha-of-main"
    assert record.pr_number == 101 and record.pr_url.endswith("/pull/101") and record.kind == "push"
    assert record.previous == {"Dockerfile": None, "scripts/rollback.sh": None, "docs/RUNBOOK.md": "old-docs/RUNBOOK.md"}
    posts = [(m, p, j) for m, p, j in calls if m == "POST"]
    assert [p for _, p, _ in posts] == ["/repos/me/proj/git/blobs"] * 3 + ["/repos/me/proj/git/trees", "/repos/me/proj/git/commits", "/repos/me/proj/git/refs", "/repos/me/proj/pulls"]
    blob = posts[0][2]
    assert base64.b64decode(blob["content"]).decode() == "FROM x\n" and blob["encoding"] == "base64"
    tree = posts[3][2]
    assert tree["base_tree"] == "tree-of-main"
    assert {e["path"]: e["mode"] for e in tree["tree"]} == {"Dockerfile": "100644", "scripts/rollback.sh": "100755", "docs/RUNBOOK.md": "100644"}
    assert posts[4][2] == {"message": "Add kit", "tree": "tree1", "parents": ["sha-of-main"]}
    assert posts[5][2] == {"ref": "refs/heads/deploy-kit/app-abc", "sha": "commit1"}
    assert posts[6][2]["base"] == "main" and posts[6][2]["head"] == "deploy-kit/app-abc"


def test_revert_restores_previous_blobs_and_deletes_new_files(monkeypatch):
    calls = []
    monkeypatch.setattr(gp.requests, "request", make_fake(calls))
    writer = gp.GitHubWriter("ghp_secret_token_123", "me/proj")
    record = writer.push_files([("Dockerfile", "x"), ("docs/RUNBOOK.md", "y")], "deploy-kit/app-1", "m", "t", "b")
    calls.clear()
    reverted = writer.open_revert(record)
    tree = next(j for m, p, j in calls if p.endswith("/git/trees"))
    assert {e["path"]: e["sha"] for e in tree["tree"]} == {"Dockerfile": None, "docs/RUNBOOK.md": "old-docs/RUNBOOK.md"}
    assert reverted.kind == "revert" and reverted.branch == "revert/app-1" and reverted.pr_number == 102
    pr = next(j for m, p, j in calls if p.endswith("/pulls"))
    assert "#101" in pr["body"] and pr["base"] == "main"


def test_refuses_base_branch_and_redacts_the_token(monkeypatch):
    calls = []

    def failing(method, url, headers=None, json=None, timeout=None):
        if url.endswith("/repos/me/proj"):
            return FakeResponse(200, {"default_branch": "main"})
        return FakeResponse(403, text="bad credentials ghp_secret_token_123 rejected")

    monkeypatch.setattr(gp.requests, "request", failing)
    writer = gp.GitHubWriter("ghp_secret_token_123", "me/proj")
    with pytest.raises(gp.GitHubPushError, match="refusing to push to the base branch"):
        writer.push_files([("a", "b")], "main", "m", "t", "b")
    with pytest.raises(gp.GitHubPushError) as excinfo:
        writer.push_files([("a", "b")], "feature", "m", "t", "b")
    assert "ghp_secret_token_123" not in str(excinfo.value) and "[REDACTED_TOKEN]" in str(excinfo.value)
    with pytest.raises(gp.GitHubPushError, match="owner/name"):
        gp.GitHubWriter("t", "not a repo")
    with pytest.raises(gp.GitHubPushError, match="token is required"):
        gp.GitHubWriter("", "me/proj")
    assert calls == []


def test_branch_name_is_stable_for_the_same_kit():
    files = [("a", "1"), ("b", "2")]
    assert gp.branch_name_for("My App", files) == gp.branch_name_for("My App", list(reversed(files)))
    assert gp.branch_name_for("My App", files).startswith("deploy-kit/my-app-")
    assert gp.branch_name_for("My App", files) != gp.branch_name_for("My App", [("a", "changed")])


def test_initialize_repository_uses_contents_api_then_one_commit(monkeypatch):
    calls = []

    def fake_request(method, url, headers=None, json=None, timeout=None):
        path = url.replace(gp.API_ROOT, "")
        calls.append((method, path, json))
        if method == "GET" and path.startswith("/repos/me/blank/git/ref/heads/"):
            return FakeResponse(404, text="nope")  # still empty: no branch exists yet
        if method == "PUT" and path.startswith("/repos/me/blank/contents/"):
            return FakeResponse(201, {"commit": {"sha": "first111"}})
        if path.startswith("/repos/me/blank/git/commits/first111"):
            return FakeResponse(200, {"tree": {"sha": "tree-first"}})
        if path == "/repos/me/blank/git/blobs":
            return FakeResponse(201, {"sha": "blob"})
        if path == "/repos/me/blank/git/trees":
            return FakeResponse(201, {"sha": "tree2"})
        if path == "/repos/me/blank/git/commits":
            return FakeResponse(201, {"sha": "second22"})
        if method == "PATCH" and path == "/repos/me/blank/git/refs/heads/main":
            return FakeResponse(200, {"object": {"sha": json["sha"]}})
        return FakeResponse(500, text="unexpected " + path)

    monkeypatch.setattr(gp.requests, "request", fake_request)
    writer = gp.GitHubWriter("ghp_secret_token_123", "me/blank")
    record = writer.initialize_repository([("README.md", "# hi\n"), ("src/app.py", "print(1)\n"), ("run.sh", "#!/bin/sh\n")], "main", "Scaffold")
    assert record.kind == "init" and record.pr_number == 0 and record.pr_url.endswith("/tree/main")
    assert record.base_sha == "first111" and record.commit_sha == "second22" and record.branch == "main"
    methods = [(m, p) for m, p, _ in calls]
    assert methods[0] == ("GET", "/repos/me/blank/git/ref/heads/main")  # still empty is re-checked right before the first write
    assert methods[1] == ("PUT", "/repos/me/blank/contents/README.md")
    assert ("PATCH", "/repos/me/blank/git/refs/heads/main") == methods[-1]
    tree = next(j for m, p, j in calls if p.endswith("/git/trees"))
    assert {e["path"]: e["mode"] for e in tree["tree"]} == {"src/app.py": "100644", "run.sh": "100755"}
    single = writer.initialize_repository([("README.md", "x")], "main", "one file")
    assert single.commit_sha == "first111"

"""Hub connector: safe tarball extraction, sha resolution, diff path parsing, changed-file collection (no network)."""
import io
import os
import sys
import tarfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import github_repo as gr
from orchestrator import sandbox


def make_tarball(top="me-proj-abc1234", files=None, evil=True):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for rel, body in (files or {"README.md": "# hi\n", "src/app.py": "print(1)\n"}).items():
            data = body.encode("utf-8")
            info = tarfile.TarInfo(f"{top}/{rel}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        if evil:
            bad = tarfile.TarInfo(f"{top}/../../evil.txt")
            bad.size = 4
            archive.addfile(bad, io.BytesIO(b"boom"))
            link = tarfile.TarInfo(f"{top}/link")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            archive.addfile(link)
    return buffer.getvalue()


def test_extract_strips_the_top_directory_and_rejects_traversal_and_links(tmp_path):
    files, size = gr.extract_tarball(make_tarball(), str(tmp_path))
    assert files == 2 and size == len("# hi\n") + len("print(1)\n")
    assert (tmp_path / "README.md").read_text() == "# hi\n" and (tmp_path / "src" / "app.py").exists()
    assert not (tmp_path / "link").exists() and not (tmp_path.parent / "evil.txt").exists() and not (tmp_path / "evil.txt").exists()


class FakeResponse:
    def __init__(self, status, body=None, raw=b"", text=""):
        self.status_code, self._body, self._raw, self.text = status, body, raw, text

    def json(self):
        return self._body

    def iter_content(self, chunk_size=65536):
        for index in range(0, len(self._raw), chunk_size):
            yield self._raw[index:index + chunk_size]


def test_fetch_tree_resolves_default_branch_and_extracts(monkeypatch, tmp_path):
    monkeypatch.setattr(gr, "staging_root", lambda: str(tmp_path))
    calls = []

    def fake_get(url, headers=None, stream=False, timeout=None, allow_redirects=True):
        calls.append((url, headers.get("Authorization")))
        if url.endswith("/repos/me/proj"):
            return FakeResponse(200, {"default_branch": "trunk"})
        if url.endswith("/commits/trunk"):
            return FakeResponse(200, {"sha": "abc1234def5678"})
        if "/tarball/abc1234def5678" in url:
            return FakeResponse(200, raw=make_tarball(evil=False))
        return FakeResponse(404, text="not found ghp_secret")

    monkeypatch.setattr(gr.requests, "get", fake_get)
    fetched = gr.fetch_tree("https://github.com/me/proj", "", token="ghp_secret")
    assert fetched.ref == "trunk" and fetched.sha == "abc1234def5678" and fetched.files == 2
    assert os.path.exists(os.path.join(fetched.path, "src", "app.py")) and fetched.path.startswith(str(tmp_path))
    assert all(auth == "Bearer ghp_secret" for _, auth in calls)
    with pytest.raises(gr.GitHubRepoError) as excinfo:
        gr.resolve_sha("me/proj", "missing", token="ghp_secret")
    assert "ghp_secret" not in str(excinfo.value) and "[REDACTED_TOKEN]" in str(excinfo.value)
    with pytest.raises(gr.GitHubRepoError, match="exceeds"):
        gr.fetch_tree("me/proj", "trunk", max_bytes=10)


def test_public_repository_needs_no_token_and_gets_a_hint_on_404(monkeypatch):
    monkeypatch.setattr(gr.requests, "get", lambda url, headers=None, **kw: FakeResponse(404, text="nope"))
    with pytest.raises(gr.GitHubRepoError, match="private repository"):
        gr.resolve_sha("me/secret", "main")


def test_changed_paths_from_git_and_copy_mode_diffs(tmp_path):
    git_diff = "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-x\n+y\ndiff --git a/new.txt b/new.txt\n--- /dev/null\n+++ b/new.txt\n"
    assert gr.changed_paths_from_diff(git_diff) == ["src/app.py", "new.txt"]
    source = tmp_path / "source"
    box = tmp_path / "box"
    (source / "pkg").mkdir(parents=True)
    (box / "pkg").mkdir(parents=True)
    (source / "pkg" / "a.py").write_text("print(1)\n")
    (box / "pkg" / "a.py").write_text("print(2)\n")
    (box / "pkg" / "b.py").write_text("new = True\n")
    (source / "gone.py").write_text("bye\n")
    diff = sandbox._copy_mode_diff(str(source), str(box))
    paths = gr.changed_paths_from_diff(diff)
    assert "pkg/a.py" in paths and "pkg/b.py" in paths
    pairs, missing = gr.collect_changed_files(str(box), diff)
    assert dict(pairs)["pkg/a.py"] == "print(2)\n" and dict(pairs)["pkg/b.py"] == "new = True\n"
    assert all(rel not in dict(pairs) for rel in missing)


def test_list_repositories_and_owner_repo_shape(monkeypatch):
    assert gr.looks_like_owner_repo("Chazzzer/Chat-Johnson") and gr.looks_like_owner_repo("https://github.com/me/proj.git")
    assert not gr.looks_like_owner_repo("Chazzzer") and not gr.looks_like_owner_repo("")
    seen = {}

    def fake_get(url, headers=None, stream=False, timeout=None, allow_redirects=True):
        seen["url"] = url
        return FakeResponse(200, [{"full_name": "me/newest"}, {"full_name": "org/tool"}, {"nope": 1}])

    monkeypatch.setattr(gr.requests, "get", fake_get)
    assert gr.list_repositories("ghp_x") == ["me/newest", "org/tool"]
    assert "sort=updated" in seen["url"] and "affiliation=owner,collaborator,organization_member" in seen["url"]
    with pytest.raises(gr.GitHubRepoError, match="token is required"):
        gr.list_repositories("")

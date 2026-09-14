"""Free knowledge sources an agent can spend tokens on in leisure: fetch is free, the summary call is charged.

Sources are plain HTTP APIs with no keys: Wikipedia, Project Gutenberg (deep texts for a literature
studio), arXiv, Open Library, Hacker News. Operators can declare their own JSON APIs; their secrets
come from the session or environment by name and are never stored.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import quote

import requests

from ..config import resolve_secret

TIMEOUT = 15
MAX_CHARS = 6_000
USER_AGENT = "ChatJohnson/1.0 (agent society leisure research)"
SOURCES: Tuple[str, ...] = ("wikipedia", "gutenberg", "arxiv", "openlibrary", "hackernews")
_TAG_RE = re.compile(r"<[^>]+>")


def _get(url: str, headers: Optional[Mapping[str, str]] = None) -> requests.Response:
    response = requests.get(url, headers={"User-Agent": USER_AGENT, **(headers or {})}, timeout=TIMEOUT)
    response.raise_for_status()
    return response


def _clip(text: str, limit: int = MAX_CHARS) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit]


def wikipedia(query: str) -> Tuple[str, str]:
    """Search, then the summary of the best page."""
    search = _get(f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={quote(query)}&format=json&srlimit=1").json()
    hits = search.get("query", {}).get("search", [])
    if not hits:
        return "", ""
    title = hits[0]["title"]
    summary = _get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(title)}").json()
    url = summary.get("content_urls", {}).get("desktop", {}).get("page", f"https://en.wikipedia.org/wiki/{quote(title)}")
    return url, _clip(f"{summary.get('title', title)}: {summary.get('extract', '')}")


def gutenberg(query: str) -> Tuple[str, str]:
    """A public-domain text: the opening pages of the best match."""
    books = _get(f"https://gutendex.com/books?search={quote(query)}").json().get("results", [])
    if not books:
        return "", ""
    book = books[0]
    formats = book.get("formats", {})
    text_url = next((u for k, u in formats.items() if k.startswith("text/plain")), "")
    if not text_url:
        return "", ""
    body = _get(text_url).text
    start = body.find("*** START")
    body = body[body.find("\n", start) + 1:] if start >= 0 else body
    authors = ", ".join(a.get("name", "") for a in book.get("authors", []))
    return text_url, _clip(f"{book.get('title', '')} by {authors}. " + body, MAX_CHARS)


def arxiv(query: str) -> Tuple[str, str]:
    feed = _get(f"http://export.arxiv.org/api/query?search_query=all:{quote(query)}&max_results=3").text
    entries = re.findall(r"<entry>(.*?)</entry>", feed, re.S)
    if not entries:
        return "", ""
    parts = []
    first_url = ""
    for entry in entries:
        title = _clip(_TAG_RE.sub("", re.search(r"<title>(.*?)</title>", entry, re.S).group(1)) if re.search(r"<title>", entry) else "", 200)
        summary = _clip(_TAG_RE.sub("", re.search(r"<summary>(.*?)</summary>", entry, re.S).group(1)) if re.search(r"<summary>", entry) else "", 1200)
        link = re.search(r"<id>(.*?)</id>", entry)
        first_url = first_url or (link.group(1).strip() if link else "")
        parts.append(f"{title}: {summary}")
    return first_url, _clip("\n".join(parts))


def openlibrary(query: str) -> Tuple[str, str]:
    docs = _get(f"https://openlibrary.org/search.json?q={quote(query)}&limit=5").json().get("docs", [])
    if not docs:
        return "", ""
    lines = [f"{d.get('title', '')} ({d.get('first_publish_year', '?')}) by {', '.join(d.get('author_name', [])[:2])}; subjects: {', '.join(d.get('subject', [])[:6])}" for d in docs]
    key = docs[0].get("key", "")
    return f"https://openlibrary.org{key}" if key else "https://openlibrary.org", _clip("\n".join(lines))


def hackernews(query: str) -> Tuple[str, str]:
    hits = _get(f"https://hn.algolia.com/api/v1/search?query={quote(query)}&hitsPerPage=5").json().get("hits", [])
    if not hits:
        return "", ""
    lines = [f"{h.get('title', '')} ({h.get('points', 0)} points): {h.get('url', '')}" for h in hits]
    return hits[0].get("url") or "https://news.ycombinator.com", _clip("\n".join(lines))


def custom(spec: Mapping[str, Any], query: str) -> Tuple[str, str]:
    """An operator-declared API: {name, url (with {query}), headers: {name: 'env:SECRET_NAME' | literal}}."""
    url = str(spec.get("url", "")).replace("{query}", quote(query))
    headers: Dict[str, str] = {}
    for name, value in (spec.get("headers") or {}).items():
        value = str(value)
        headers[name] = resolve_secret(value[4:]) if value.startswith("env:") else value
    response = _get(url, headers)
    try:
        payload = response.json()
        text = _clip(str(payload))
    except ValueError:
        text = _clip(_TAG_RE.sub(" ", response.text))
    return url, text


BUILTIN = {"wikipedia": wikipedia, "gutenberg": gutenberg, "arxiv": arxiv, "openlibrary": openlibrary, "hackernews": hackernews}


def inquire(source: str, query: str, custom_sources: Optional[List[Mapping[str, Any]]] = None) -> Tuple[str, str]:
    """(url, text) for one inquiry; empty strings when nothing came back. Network errors surface as exceptions."""
    if source in BUILTIN:
        return BUILTIN[source](query)
    for spec in custom_sources or []:
        if spec.get("name") == source:
            return custom(spec, query)
    raise KeyError(f"unknown inquiry source {source!r}")

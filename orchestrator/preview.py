"""Live preview canvas: allowlist sanitization of generated markup.

Model-generated or pasted HTML is rendered inside the app's preview iframe.
Everything that can execute or reach the network is removed with an
allowlist (nh3, a Rust port of ammonia), not with regexes: scripts, frames,
forms, event handlers in any spelling, javascript:/remote URLs. Images are
allowed only as inline data: URIs; links only as in-page anchors.
"""
from __future__ import annotations

import base64
import binascii
import html
import secrets
import re
from typing import List, Optional, Set, Tuple
from urllib.parse import unquote

try:
    import nh3
except ImportError:  # pragma: no cover
    nh3 = None  # type: ignore[assignment]

PREVIEW_CSS = """
  :root { color-scheme: dark; }
  body { margin: 0; padding: 18px; font-family: -apple-system, Segoe UI, Inter, sans-serif; background: #0d1626; color: #e9eef8; }
  .preview-shell { border: 1px solid #2b4266; border-radius: 16px; padding: 20px; background: #101b2e; }
  button { background: #2fe3b4; color: #06231d; border: 0; border-radius: 10px; padding: 8px 14px; font-weight: 700; }
  .notice { font-size: .8rem; color: #9fb3d1; margin-top: 12px; }
"""

ALLOWED_TAGS: Set[str] = {
    "a", "abbr", "article", "aside", "b", "blockquote", "br", "button", "caption", "code", "dd", "details", "div", "dl",
    "dt", "em", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "i", "img", "input",
    "kbd", "label", "li", "main", "mark", "nav", "ol", "option", "p", "pre", "progress", "section", "select", "small",
    "span", "strong", "style", "sub", "summary", "sup", "table", "tbody", "td", "textarea", "tfoot", "th", "thead",
    "tr", "u", "ul",
}
_GENERIC_ATTRS = {"class", "id", "style", "title", "role", "aria-label", "aria-hidden", "data-preview-status"}
ALLOWED_ATTRIBUTES = {
    "*": _GENERIC_ATTRS,
    "a": _GENERIC_ATTRS | {"href"},
    "img": _GENERIC_ATTRS | {"src", "alt", "width", "height"},
    "input": _GENERIC_ATTRS | {"type", "placeholder", "value", "checked", "disabled", "name"},
    "button": _GENERIC_ATTRS | {"type", "disabled"},
    "select": _GENERIC_ATTRS | {"name", "disabled"},
    "option": _GENERIC_ATTRS | {"value", "selected"},
    "textarea": _GENERIC_ATTRS | {"placeholder", "rows", "cols", "disabled"},
    "td": _GENERIC_ATTRS | {"colspan", "rowspan"},
    "th": _GENERIC_ATTRS | {"colspan", "rowspan"},
    "progress": _GENERIC_ATTRS | {"value", "max"},
    "label": _GENERIC_ATTRS | {"for"},
}
_DATA_IMAGE = re.compile(r"^data:image/(?:png|jpe?g|gif|webp|svg\+xml);base64,[A-Za-z0-9+/=]+$", re.I)
_CSS_URL = re.compile(r"(?i)url\s*\(")


def _attribute_filter(tag: str, attribute: str, value: str) -> Optional[str]:
    """Keep only in-page anchors, inline data images, and harmless inline styles."""
    if attribute == "href":
        return value if value.startswith("#") else None
    if attribute == "src":
        return value if _DATA_IMAGE.match(value.strip()) else None
    if attribute == "style":
        lowered = value.lower()
        if "expression(" in lowered or "javascript:" in lowered or _CSS_URL.search(lowered):
            return None
        return value
    return value


def sanitize_markup(source: str) -> str:
    """Allowlist-sanitize a fragment; falls back to escaped text if nh3 is missing."""
    if nh3 is None:
        return f"<pre>{html.escape(source)}</pre>"
    cleaned = nh3.clean(
        source,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        attribute_filter=_attribute_filter,
        url_schemes={"data"},
        link_rel=None,
        strip_comments=True,
        # nh3 strips <style> with its content by default; we allow it (CSS cannot run
        # scripts, and url() is rewritten below as belt and braces; the document's CSP is the real control) but keep every executable tag stripped.
        clean_content_tags={"script", "iframe", "object", "embed", "form", "base", "link", "noscript", "template", "svg", "math"},
    )
    # <style> text survives as text; strip CSS that could reach the network or run.
    cleaned = re.sub(r"(?is)<style\b[^>]*>(.*?)</style>", lambda m: "<style>" + _CSS_URL.sub("blocked(", m.group(1)) + "</style>", cleaned)
    return cleaned


def looks_like_markup(source: str) -> bool:
    return bool(re.search(r"<\s*(?:!doctype|html|body|main|section|div|article|style|button|table|form|ul|h[1-6])\b", source, re.I))


_BARE_LINK = re.compile(r"^(?:\[[^\]]*\]\()?<?(?:https?://|www\.)\S+>?\)?[.,;:]?$", re.I)


def looks_like_link(source: str) -> bool:
    """A pasted URL or markdown link on its own: the canvas never fetches it, so the app says so instead of showing a blank shell."""
    value = source.strip()
    return bool(value) and not looks_like_markup(value) and bool(_BARE_LINK.match(value))


_DATA_HTML = re.compile(r"data:text/html(?P<params>(?:;[a-z0-9-]+=[^;,\s]*|;base64)*),(?P<body>[^\s)\]>'\"]+)", re.I)


def decode_data_link(text: str) -> str:
    """The markup carried by the first data:text/html link in the text (base64 or percent-encoded); '' when none.

    A model that "sends a preview link" sends one of these; the canvas cannot open links, so it opens the payload.
    """
    match = _DATA_HTML.search(text or "")
    if not match:
        return ""
    body = match.group("body")
    try:
        if "base64" in match.group("params").lower():
            decoded = base64.b64decode(body + "=" * (-len(body) % 4)).decode("utf-8", "replace")
        else:
            decoded = unquote(body)
    except (ValueError, binascii.Error):
        return ""
    return decoded.strip() if looks_like_markup(decoded) else ""


def resolve_preview_source(source: str) -> str:
    """A pasted or generated data:text/html link becomes the markup it carries; anything else is returned as is."""
    return decode_data_link(source) or source


_FENCE = re.compile(r"```(?P<language>[^\n`]*)\n(?P<body>.*?)```", re.DOTALL)
_OPEN_FENCE = re.compile(r"```(?P<language>[^\n`]*)\n(?P<body>.*)\Z", re.DOTALL)


def _fence_source(language: str, body: str) -> str:
    normalized = language.strip().lower()
    if normalized.startswith(("html", "htm")) or looks_like_markup(body):
        return body.strip()
    if normalized.startswith("css"):
        return f"<style>{body.strip()}</style><div class='preview-shell'><h1>CSS preview</h1><button type='button'>Example control</button></div>"
    return ""


_PAGE_SHAPE = re.compile(r"<\s*(?:!doctype|html|body)\b", re.I)
PAGE_MIN_CHARS = 400


def _is_page(language: str, body: str) -> bool:
    """A whole page (doctype/html/body, or a long markup fence), as opposed to a snippet or a CSS stub."""
    normalized = language.strip().lower()
    if normalized.startswith("css"):
        return False
    return bool(_PAGE_SHAPE.search(body)) or (normalized.startswith(("html", "htm")) and len(body.strip()) >= PAGE_MIN_CHARS)


def extract_preview_fence(text: str) -> Tuple[str, bool]:
    """(markup, closed) for the canvas: the LAST closed whole page wins, then a cut page, then the first snippet.

    A model that restarts a page, quotes a snippet, or shows a CSS stub before the real page used to put the wrong
    thing on the canvas; a cut page (its fence never closed) is taken whole so it still renders, marked as not closed.
    """
    closed = [(language, body) for language, body in _FENCE.findall(text)]
    pages = [(language, body) for language, body in closed if _is_page(language, body)]
    if pages:
        language, body = pages[-1]
        return _fence_source(language, body), True
    tail = text[text.rfind("```") :] if "```" in text else ""
    unclosed = _OPEN_FENCE.match(tail) if tail.count("```") == 1 else None
    if unclosed and _is_page(unclosed.group("language"), unclosed.group("body")):
        found = _fence_source(unclosed.group("language"), unclosed.group("body"))
        if found:
            return found, False
    for language, body in closed:
        found = _fence_source(language, body)
        if found:
            return found, True
    if unclosed:
        found = _fence_source(unclosed.group("language"), unclosed.group("body"))
        if found:
            return found, False
    linked = decode_data_link(text)
    if linked:
        return linked, True
    if looks_like_markup(text):
        return text.strip(), True
    return "", True


def extract_preview_source(text: str) -> str:
    """The markup the canvas should show for a model answer; '' when there is none (see ``extract_preview_fence``)."""
    return extract_preview_fence(text)[0]


_TAG_PAIRS = (("<script", "</script"), ("<style", "</style"))
_BRACKETS = {"{": "}", "[": "]", "(": ")"}


def _script_balance(script: str) -> bool:
    """Whether braces, brackets and parentheses balance outside strings, template literals and comments."""
    stack: List[str] = []
    i, n = 0, len(script)
    quote = ""
    while i < n:
        ch = script[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote or (quote == "`" and ch == "`"):
                quote = ""
            i += 1
            continue
        if script.startswith("//", i):
            end = script.find("\n", i)
            i = n if end < 0 else end + 1
            continue
        if script.startswith("/*", i):
            end = script.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        if ch in ("'", '"', "`"):
            quote = ch
        elif ch in _BRACKETS:
            stack.append(_BRACKETS[ch])
        elif ch in _BRACKETS.values():
            if not stack or stack[-1] != ch:
                return False
            stack.pop()
        i += 1
    return not stack and not quote


EXTERNAL_RESOURCE_RE = re.compile(
    r"""<(?:script|link|img|iframe|video|audio|source)\b[^>]*?(?:src|href)\s*=\s*["']?(?:https?:)?//[^"'\s>]+|@import\s+(?:url\()?["']?https?://""",
    re.I,
)


def external_resources(source: str) -> List[str]:
    """Every place the page reaches out to the internet; the canvas blocks all of them, so each one is a dead style or script."""
    return sorted({match.group(0)[:80] for match in EXTERNAL_RESOURCE_RE.finditer(source or "")})


def page_review(source: str, closed: bool = True, finish: str = "") -> str:
    """A deterministic review of a generated page, used in place of the model critique on a page request.

    A model critique costs a provider request, and on a page build every Heavy pass lands on the same endpoint, so
    the critique spends the per-minute window the synthesis needs and its verdict is then thrown away when the
    synthesis is refused for lack of headroom. These checks cost nothing and cannot be wrong about structure.
    """
    if not (source or "").strip():
        return (
            "PAGE REVIEW (automatic, no provider call): the answer carried no complete ```html page. "
            "Return the whole page as one complete ```html fence, inline CSS and JavaScript only."
        )
    problems = list(page_completeness(source, closed, finish))
    outside = external_resources(source)
    if outside:
        problems.append(
            f"loads {len(outside)} resource(s) from the internet, which the canvas always blocks, so they arrive dead: "
            + "; ".join(outside[:4])
        )
    lines = [f"PAGE REVIEW (automatic, no provider call) of {len(source)} characters:"]
    if problems:
        lines.append("Problems that must be fixed in the final answer:")
        lines.extend(f"- {problem}" for problem in problems)
        lines.append(
            "Return the corrected page in full as one complete ```html fence. Keep everything that already works; "
            "do not shorten the page or drop features to make it fit."
        )
    else:
        lines.append(
            "The page is structurally complete and self-contained. Keep it that way: return it in full, "
            "improve only what the request asked for, and do not shorten it."
        )
    return "\n".join(lines)


def page_completeness(source: str, closed: bool = True, finish: str = "") -> List[str]:
    """Why a page is not whole: '' entries never appear; an empty list means complete.

    Cheap and deterministic: fence closed, the document closed, every script and style closed, brackets balanced
    in every script, and the vendor not having reported a cut. An incomplete page is continued, never repaired.
    """
    reasons: List[str] = []
    body = source or ""
    lowered = body.lower()
    if finish == "length":
        reasons.append("the answer was cut at the length limit")
    if not closed:
        reasons.append("the code fence never closed")
    if "<html" in lowered and "</html" not in lowered:
        reasons.append("</html> is missing")
    elif "<body" in lowered and "</body" not in lowered:
        reasons.append("</body> is missing")
    for opener, closer in _TAG_PAIRS:
        if lowered.count(opener) > lowered.count(closer):
            reasons.append(f"a {opener[1:]} block never closes")
    for match in re.finditer(r"<script\b[^>]*>(.*?)</script\s*>", body, re.I | re.S):
        if not _script_balance(match.group(1)):
            reasons.append("a script has unbalanced braces or an unterminated string")
            break
    return reasons


def safe_preview_document(source: str) -> str:
    """A complete, no-network HTML document for the preview iframe."""
    value = source.strip()
    if not value:
        value = (
            "<div class='preview-shell'><h1>Preview canvas</h1>"
            "<p>Generated interface markup will appear here.</p>"
            "<button type='button'>Example control</button></div>"
        )
    if not looks_like_markup(value):
        value = f"<pre>{html.escape(value)}</pre>"
    else:
        value = sanitize_markup(value)
    if not re.search(r"<\s*style\b", value, re.I):
        value = f"<style>{PREVIEW_CSS}</style>{value}"
    notice = (
        "<p class='notice' data-preview-status>Preview sandbox: scripts, frames, forms, event handlers, and remote "
        "resources are removed by an allowlist sanitizer; only inline data: images and in-page links survive.</p>"
    )
    nonce = secrets.token_urlsafe(12)  # the only script the CSP runs is this one; nothing that survives the sanitizer can execute
    runtime = f"""
    <script nonce="{nonce}">
    (() => {{
      const status = document.querySelector('[data-preview-status]');
      document.querySelectorAll('button').forEach((button) => {{
        button.addEventListener('click', () => {{
          if (status) status.textContent = 'Local preview interaction captured.';
        }});
      }});
    }})();
    </script>
    """
    if "preview-shell" not in value:
        value = f"<div class='preview-shell'>{value}{notice}</div>"
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'; "
        f"script-src 'nonce-{nonce}'; img-src data:; connect-src 'none'; frame-src 'none'; form-action 'none'\">"
        f"</head><body>{value}{runtime}</body></html>"
    )

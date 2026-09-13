"""Live preview canvas: allowlist sanitization of generated markup.

Model-generated or pasted HTML is rendered inside the app's preview iframe.
Everything that can execute or reach the network is removed with an
allowlist (nh3, a Rust port of ammonia), not with regexes: scripts, frames,
forms, event handlers in any spelling, javascript:/remote URLs. Images are
allowed only as inline data: URIs; links only as in-page anchors.
"""
from __future__ import annotations

import html
import re
from typing import Optional, Set

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
        # scripts, and url() is neutralised below) but keep every executable tag stripped.
        clean_content_tags={"script", "iframe", "object", "embed", "form", "base", "link", "noscript", "template", "svg", "math"},
    )
    # <style> text survives as text; strip CSS that could reach the network or run.
    cleaned = re.sub(r"(?is)<style\b[^>]*>(.*?)</style>", lambda m: "<style>" + _CSS_URL.sub("blocked(", m.group(1)) + "</style>", cleaned)
    return cleaned


def looks_like_markup(source: str) -> bool:
    return bool(re.search(r"<\s*(?:!doctype|html|body|main|section|div|article|style|button|table|form|ul|h[1-6])\b", source, re.I))


def extract_preview_source(text: str) -> str:
    """Pull the first HTML/CSS fence (or bare markup) out of a model answer; '' when there is none."""
    blocks = re.findall(r"```(?P<language>[^\n`]*)\n(?P<body>.*?)```", text, flags=re.DOTALL)
    for language, body in blocks:
        normalized = language.strip().lower()
        if normalized.startswith(("html", "htm")) or looks_like_markup(body):
            return body.strip()
        if normalized.startswith("css"):
            return f"<style>{body.strip()}</style><div class='preview-shell'><h1>CSS preview</h1><button type='button'>Example control</button></div>"
    if looks_like_markup(text):
        return text.strip()
    return ""


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
    runtime = """
    <script>
    (() => {
      const status = document.querySelector('[data-preview-status]');
      document.querySelectorAll('button').forEach((button) => {
        button.addEventListener('click', () => {
          if (status) status.textContent = 'Local preview interaction captured.';
        });
      });
    })();
    </script>
    """
    if "preview-shell" not in value:
        value = f"<div class='preview-shell'>{value}{notice}</div>"
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'; "
        "script-src 'unsafe-inline'; img-src data:; connect-src 'none'; frame-src 'none'; form-action 'none'\">"
        f"</head><body>{value}{runtime}</body></html>"
    )

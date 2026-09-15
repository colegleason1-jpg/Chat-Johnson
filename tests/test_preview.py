"""Preview sanitizer: allowlist, not regex."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.preview import extract_preview_source, safe_preview_document, sanitize_markup


def test_slash_separated_handlers_and_scripts_are_removed():
    dirty = '<div/onclick="alert(1)">x</div><button/onmouseover=alert(1)>b</button><script>alert(2)</script><img src=x onerror=alert(3)>'
    clean = sanitize_markup(dirty)
    assert "onclick" not in clean and "onmouseover" not in clean and "onerror" not in clean
    assert "<script" not in clean and "alert(" not in clean
    assert "<div>x</div>" in clean and "<button>b</button>" in clean


def test_urls_only_inline_images_and_anchors_survive():
    dirty = (
        '<a href="javascript:alert(1)">j</a><a href="https://evil.example">r</a><a href="#top">ok</a>'
        '<img src="https://evil.example/x.png"><img src="data:image/png;base64,iVBORw0KGgo=">'
        '<iframe src="https://evil.example"></iframe><form action="https://evil.example"><input></form>'
    )
    clean = sanitize_markup(dirty)
    assert "javascript:" not in clean and "evil.example" not in clean
    assert 'href="#top"' in clean
    assert 'src="data:image/png;base64,iVBORw0KGgo="' in clean
    assert "<iframe" not in clean and "<form" not in clean


def test_style_cannot_reach_the_network():
    clean = sanitize_markup('<style>body{background:url(https://evil.example/x)}</style><div style="background:url(x)">t</div>')
    assert "url(https" not in clean
    assert 'style="' not in clean  # inline style with url() is dropped entirely


def test_document_wraps_plain_text_and_keeps_csp():
    doc = safe_preview_document("just words")
    assert "<pre>just words</pre>" in doc
    assert "default-src 'none'" in doc and "form-action 'none'" in doc
    assert "data-preview-status" in doc


def test_extract_prefers_html_fences_and_returns_empty_otherwise():
    assert extract_preview_source("```html\n<main>hi</main>\n```") == "<main>hi</main>"
    assert extract_preview_source("```python\nprint(1)\n```") == ""
    assert extract_preview_source("no markup here") == ""
    assert "<style>" in extract_preview_source("```css\nbody{color:red}\n```")


def test_extract_takes_an_unclosed_fence_from_a_truncated_answer():
    from orchestrator.preview import looks_like_link

    cut = "Here is the mockup:\n```html\n<main><h1>Dash</h1><button>Go</button>"
    assert extract_preview_source(cut) == "<main><h1>Dash</h1><button>Go</button>"
    assert extract_preview_source("```python\nprint(1)") == ""  # an open code fence is not markup
    assert extract_preview_source("```html\n<div>closed</div>\n```\n\n```html\n<p>open") == "<div>closed</div>"
    assert looks_like_link("https://chat-johnson.streamlit.app/?ws=normal_chat&scope=v1#conversation")
    assert looks_like_link("[open it](https://example.com/mockup.html)") and looks_like_link("<https://example.com>")
    assert not looks_like_link("<a href='https://example.com'>x</a>") and not looks_like_link("plain words") and not looks_like_link("")
    assert "<pre>https://example.com</pre>" in safe_preview_document("https://example.com")

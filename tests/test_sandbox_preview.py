"""Batch T: the Run-mode contract (insert, document assembly, hazard stripping, report normalization, fix decisions)."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from orchestrator import sandbox_preview as sp  # noqa: E402

PAGE = "<!doctype html>\n<html>\n<head>\n<title>t</title>\n</head>\n<body>\n<script>\nlet x = 1;\nfoo();\n</script>\n</body>\n</html>"


def test_insert_is_one_line_with_the_csp_the_seq_and_the_loud_failures():
    insert = sp.sandbox_insert(7)
    assert "\n" not in insert and sp.CHILD_CSP in insert and "var SEQ=7," in insert
    assert "x-dns-prefetch-control" in insert and "chatJohnsonPreview" in insert
    for shadow in ("window.alert=", "window.open=", "'RTCPeerConnection'", "writable:false,configurable:false", "navigator.clipboard",
                   "HTMLFormElement.prototype.submit", "'dragstart'", "MutationObserver", "link[rel~=preconnect]"):
        assert shadow in insert
    assert "webrtc" not in sp.CHILD_CSP and "frame-src 'none'" in sp.COMPONENT_CSP and "script-src 'self'" in sp.COMPONENT_CSP


def test_document_assembly_puts_the_insert_first_and_keeps_every_page_line_number():
    insert = sp.sandbox_insert(1)
    doc = sp.build_document(PAGE, insert)
    lines = doc.splitlines()
    assert lines[0] == "<!doctype html><html><head>" + insert + "</head><!doctype html>" and lines[8] == "foo();"
    assert doc.count(insert) == 1 and doc.index(insert) < doc.index("<script>\nlet x")
    # A script before the page's own <head>, or a <head> inside a comment, can no longer run ahead of the shim.
    hostile = "<html><script>window.__RTC=RTCPeerConnection</script><head></head><body></body></html>"
    doc = sp.build_document(hostile, insert)
    assert doc.index(insert) < doc.index("window.__RTC")
    doc = sp.build_document("<!-- <head> --><html><head><title>x</title></head></html>", insert)
    assert doc.startswith("<!doctype html><html><head>" + insert + "</head><!-- <head> -->")
    fragment = "<div>a</div>\n<script>b()</script>"
    doc = sp.build_document(fragment, insert, three_data_url="data:text/javascript;base64,AA==")
    first, second = doc.splitlines()
    assert first.endswith("</head><div>a</div>") and second == "<script>b()</script>"
    assert '<script type="importmap">{"imports": {"three": "data:text/javascript;base64,AA=="}}</script></head>' in first


def test_three_detection_and_hazard_stripping():
    assert sp.needs_three("<script type=module>import * as THREE from 'three';</script>")
    assert sp.needs_three('import("three").then(m => m)') and not sp.needs_three("<p>three</p><script>const three = 3</script>")
    source = (
        "<head><link rel=\"preconnect\" href=\"https://x.test\"><meta http-equiv=\"refresh\" content=\"1;url=https://x.test\">"
        "<script type=\"importmap\">{\"imports\":{\"three/addons/\":\"https://unpkg.com/\"}}</script><base href=\"https://x.test/\">"
        "<link rel=\"stylesheet\" href=\"x.css\"></head><body>ok</body>"
    )
    cleaned, notes = sp.strip_hazards(source)
    assert "preconnect" not in cleaned and "refresh" not in cleaned and "importmap" not in cleaned and "<base" not in cleaned
    assert "stylesheet" in cleaned and "ok" in cleaned  # CSP handles those; only the unpoliced tags go
    assert notes == ["a resource hint link", "a meta refresh", "a page-authored import map", "a base tag"]
    # Attribute order, a '>' inside an earlier attribute value, and a multi-token rel do not hide a tag.
    tricky = (
        "<link title=\">\" rel=\"preconnect\" href=\"https://x.test\"><meta title=\">\" http-equiv=\"refresh\" content=\"0\">"
        "<link href=\"a\" rel='stylesheet dns-prefetch'><LINK REL=PRELOAD HREF=b><p>keep</p>"
    )
    cleaned, notes = sp.strip_hazards(tricky)
    assert cleaned == "<p>keep</p>" and notes == ["3 a resource hint links", "a meta refresh"]
    assert sp.strip_hazards("<p>plain</p>") == ("<p>plain</p>", [])


def test_normalize_report_clips_dedupes_caps_and_infers_status():
    assert sp.normalize_report(None) is None and sp.normalize_report({"errors": []}) is None
    raw = {
        "seq": "3", "page": "abc", "status": "weird",
        "errors": [{"message": "Uncaught ReferenceError: foo is not defined", "line": "9", "column": 1, "stack": "s"}] * 3
        + [{"message": "x" * 900, "line": 2}],
        "blocked": [
            {"what": "csp", "detail": "script-src-elem https://cdn.tailwindcss.com/"},
            {"what": "resource", "detail": "script https://cdn.tailwindcss.com/"},
            {"what": "resource", "detail": "img https://i.pravatar.cc/40"},
            {"what": "api", "detail": "alert()"}, {"what": "api", "detail": "alert()"}, "raw string",
        ],
        "console": [{"level": "error", "text": "boom"}, {"level": "error", "text": "boom"}, "plain", {"level": "warn", "text": ""}],
        "elements": "12", "text": "t" * 400, "ms": 512,
    }
    report = sp.normalize_report(raw)
    assert report["seq"] == 3 and report["page"] == "abc" and report["status"] == "error"
    assert [e["line"] for e in report["errors"]] == [9, 2] and len(report["errors"][1]["message"]) == 500
    assert report["blocked"] == ["script-src-elem https://cdn.tailwindcss.com/", "alert()", "raw string", "img https://i.pravatar.cc/40"]
    assert report["console"] == ["error: boom", "plain"] and report["elements"] == 12 and len(report["text"]) == 300 and report["ms"] == 512
    clean = sp.normalize_report({"seq": 4, "status": "ready"})
    assert clean["status"] == "ready" and clean["errors"] == [] and sp.report_summary(clean) == "ran clean"
    assert sp.normalize_report({"seq": 5, "status": "blank"})["status"] == "blank"
    blocked = sp.normalize_report({"seq": 5, "status": "blocked", "blocked": [{"what": "csp", "detail": "style-src-elem https://fonts.test/"}]})
    assert blocked["status"] == "blocked" and sp.report_summary(blocked) == "1 blocked · the page depends on external scripts or styles"
    many = sp.normalize_report({"seq": 6, "errors": [{"message": f"e{i}", "line": i} for i in range(40)]})
    assert len(many["errors"]) == 20 and sp.report_summary(many) == "20 error(s)"


def test_error_signature_is_order_independent_and_sensitive_to_content():
    a = sp.normalize_report({"seq": 1, "errors": [{"message": "m1", "line": 1}, {"message": "m2", "line": 2}], "blocked": ["u1", "u2"]})
    b = sp.normalize_report({"seq": 9, "errors": [{"message": "m2", "line": 2}, {"message": "m1", "line": 1}], "blocked": ["u2", "u1"]})
    c = sp.normalize_report({"seq": 1, "errors": [{"message": "m1", "line": 1}], "blocked": ["u1", "u2"]})
    assert sp.error_signature(a) == sp.error_signature(b) != sp.error_signature(c)


def test_fix_decision_matrix_and_which_refusals_are_transient():
    error = sp.normalize_report({"seq": 2, "errors": [{"message": "m", "line": 1}]})
    fresh = {"rounds": 0, "last_signature": "", "handled_seq": 0}
    assert sp.fix_decision(fresh, None, True, False) == (False, "no report", True)
    assert sp.fix_decision({"handled_seq": 2}, error, True, False) == (False, "already handled", True)
    assert sp.fix_decision(fresh, sp.normalize_report({"seq": 2, "status": "ready"}), True, False) == (False, "ran clean", True)
    assert sp.fix_decision({"rounds": 2}, error, True, False) == (False, f"stopped: 2 automatic rounds used; {sp.NEXT_STEP}", True)
    same = {"rounds": 1, "last_signature": sp.error_signature(error)}
    assert sp.fix_decision(same, error, True, False) == (False, f"stopped: the same error came back; {sp.NEXT_STEP}", True)
    # Transient refusals leave the report open so the next rerun decides again.
    assert sp.fix_decision(fresh, error, False, False) == (False, "Heavy Mode is off, so nothing is fixed automatically", False)
    assert sp.fix_decision(fresh, error, True, False, keyed=False) == (False, "no provider key is configured, so nothing is fixed automatically", False)
    assert sp.fix_decision(fresh, error, True, True) == (False, "a generation is already running; the fix waits for it", False)
    assert sp.fix_decision({"rounds": 1, "last_signature": "other"}, error, True, False) == (True, "automatic fix round 2 of 2", True)
    for status in ("blocked", "blank", "timeout", "navigated"):
        assert sp.fix_decision(fresh, sp.normalize_report({"seq": 3, "status": status}), True, False)[0] is True


def test_fix_prompt_carries_the_report_the_marker_and_the_page_in_one_fence():
    report = sp.normalize_report({
        "seq": 1, "status": "error",
        "errors": [{"message": "Uncaught ReferenceError: foo is not defined", "line": 9, "column": 1, "stack": "ReferenceError: foo\n    at about:srcdoc:9:1\n    at run (about:srcdoc:12:3)"}, {"message": "rejected", "line": 0}],
        "blocked": ["connect-src https://api.test/", "alert()"], "console": [{"level": "error", "text": "boom"}],
    })
    prompt = sp.fix_prompt(PAGE, report, 1)
    assert prompt.startswith("AUTOMATIC FIX 1/2:") and "status: error" in prompt
    assert "- line 9 col 1: Uncaught ReferenceError: foo is not defined" in prompt and "  stack: at about:srcdoc:9:1 | at run (about:srcdoc:12:3)" in prompt
    assert "- line ?: rejected" in prompt and "Blocked (2): connect-src https://api.test/; alert()" in prompt and "- error: boom" in prompt
    assert prompt.count("\n```html\n") == 1 and prompt.rstrip().endswith("```") and PAGE in prompt
    assert "exactly ONE ```html fence" in prompt and "PREVIEW RULES" in prompt and prompt.count(sp.FIX_MARKER) == 1
    shown = sp.fix_report_part(prompt)
    assert shown.startswith("AUTOMATIC FIX 1/2") and "- error: boom" in shown and sp.FIX_MARKER not in shown and "```" not in shown and "<script>" not in shown
    blank = sp.fix_prompt("<p>x</p>", sp.normalize_report({"seq": 2, "status": "blank"}), 2)
    assert "AUTOMATIC FIX 2/2" in blank and "blank: the page rendered no text" in blank and "Errors (0):" in blank and "- (empty)" in blank
    blocked = sp.fix_prompt("<p>x</p>", sp.normalize_report({"seq": 2, "status": "blocked"}), 1)
    assert "blocked: the page depends on external scripts or styles" in blocked


def test_rules_and_hashes():
    assert len(sp.PREVIEW_RULES) < 2700
    for word in ("```html", "submit event", "pointer events", "from 'three'", "importmap", "localStorage", "720x480", "console.error(err)"):
        assert word in sp.PREVIEW_RULES
    assert "Wrap risky code in try/catch" not in sp.PREVIEW_RULES  # a page that swallows its errors would never be fixed
    assert sp.page_hash(" a ") == sp.page_hash("a") and len(sp.page_hash("x")) == 16 and sp.page_hash("x") != sp.page_hash("y")
    assert sp.FIX_ROUNDS == 2 and sp.READY_TIMEOUT_MS == 8000 and "blocked" in sp.FIX_STATUSES


@pytest.mark.parametrize("status,expected", [("blank", "blank page"), ("timeout", "no ready signal"), ("navigated", "navigate away")])
def test_summaries_name_the_non_error_outcomes(status, expected):
    assert expected in sp.report_summary(sp.normalize_report({"seq": 1, "status": status}))

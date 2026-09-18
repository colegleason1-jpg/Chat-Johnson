"""Run mode for the Live preview canvas: a model-written page executes in a nested sandboxed frame and reports back.

The sanitized canvas (``preview.py``) strips every script. This module is the contract for the other mode: the page
runs in ``<iframe sandbox="allow-scripts">`` (opaque origin: no cookies, no storage, no parent access) under a
no-network Content-Security-Policy, hosted by the declared Streamlit component in ``frontend/sandbox_preview``.
Python owns everything injected into the page (``sandbox_insert``) and everything read back from it
(``normalize_report``), so both are testable without a browser; the component only splices, hosts and forwards.

The insert is the first thing the parser sees: a wrapper ``<!doctype html><html><head>…</head>`` carrying the CSP
and the shim is prepended on the page's first line, so no page script can run before the shim, a ``<head>`` inside
a leading comment cannot swallow it, and every page line keeps its number (the HTML parser merges the page's own
``<html>``/``<head>`` children into the head it already has). The shim makes the sandbox's silent failures loud:
forms never fire submit, dialogs and downloads are ignored, HTML5 drops never arrive; each is reported once as
"blocked" so the automatic fix can name it. A srcdoc frame inherits its host document's CSP on top of its own,
which is why ``COMPONENT_CSP`` allows exactly what ``CHILD_CSP`` allows and pins the frame with ``frame-src 'none'``.
The browser offers no policy for WebRTC, so the shim shadows it; that and the runtime hazard sweep are best effort,
the sandbox and the CSP are the boundary, and the only data inside the frame is the page itself.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

FIX_ROUNDS = 2
NORMAL_FIX_ROUNDS = 1  # without Heavy Mode a page still gets one automatic repair; Heavy Mode buys the second
FIX_PREFIX = "AUTOMATIC FIX "
FIX_MARKER = "INSTRUCTIONS FOR THE FIX:"  # everything before it is the report the operator sees; after it, the model's brief
FIX_STATUSES = ("error", "blocked", "blank", "timeout", "navigated")
STATUSES = ("ready",) + FIX_STATUSES
READY_TIMEOUT_MS = 8000
FRAME_WIDTH, FRAME_HEIGHT = 720, 480
NEXT_STEP = "describe what you see in the chat and the model will try again"

# What the page itself may do. Chrome ignores the ``webrtc`` directive, so RTCPeerConnection is shadowed in the shim.
CHILD_CSP = (
    "default-src 'none'; script-src 'unsafe-inline' data: blob:; style-src 'unsafe-inline'; img-src data: blob:; "
    "font-src data:; media-src data: blob:; worker-src blob:; connect-src 'none'; frame-src 'none'; "
    "form-action 'none'; base-uri 'none'"
)
# The component document: its own scripts come from its directory ('self'); everything else mirrors the child, which
# inherits this policy. frame-src 'none' still lets the srcdoc frame load but refuses any navigation of it to http(s).
COMPONENT_CSP = (
    "default-src 'none'; script-src 'self' 'unsafe-inline' data: blob:; style-src 'unsafe-inline'; img-src data: blob:; "
    "font-src data:; media-src data: blob:; worker-src blob:; connect-src 'none'; frame-src 'none'; "
    "form-action 'none'; base-uri 'none'"
)

PREVIEW_RULES = (
    "PREVIEW RULES (the page runs in a no-network sandbox; follow every rule):\n"
    "- Deliver the whole interface as ONE complete HTML document (doctype, head, body) in a single ```html fence: "
    "no second fence, no partial excerpt, no data: link, no URL.\n"
    "- Everything inline: CSS in <style>, JavaScript in <script>. No external URLs at all: no CDN scripts or "
    "stylesheets (no Tailwind, Chart.js, React, Vue, jQuery, Bootstrap; no framework exists here, write vanilla DOM "
    "code), no Google Fonts, no remote images. Use system fonts, inline SVG, CSS gradients or data: URIs.\n"
    "- No network: fetch, XMLHttpRequest, WebSocket and EventSource fail; simulate data in JavaScript with realistic "
    "sample values.\n"
    "- No storage: localStorage, sessionStorage, cookies and IndexedDB throw; keep state in variables and say in the "
    "UI that persistence is off in the preview.\n"
    "- Forms never fire a submit event: no <form> submission; use a button with a click handler plus a keydown "
    "handler for Enter.\n"
    "- HTML5 drag-and-drop never delivers a drop: drag with pointer events (pointerdown, pointermove, pointerup and "
    "setPointerCapture).\n"
    "- Dialogs, downloads, clipboard and popups fail silently: no alert, confirm, prompt, window.open, a[download] "
    "or navigator.clipboard; draw messages and exportable text inside the page.\n"
    "- 3D: import * as THREE from 'three' inside <script type=\"module\"> is available (Three.js r160 core only; no "
    "addons such as OrbitControls, write your own drag-to-orbit; never write your own <script type=\"importmap\">). "
    "A failed static import cannot be caught: put risky logic in try/catch inside the module and keep the HTML "
    "meaningful without it.\n"
    "- Do not hide failures: if you catch an error to keep the page usable, show it in the page AND call "
    "console.error(err) so the sandbox can report it.\n"
    f"- The frame is {FRAME_WIDTH}x{FRAME_HEIGHT} and scrolls: put the most important content in the first "
    f"{FRAME_HEIGHT}px; canvases scale with CSS (max-width:100%; height:auto); a device mockup sizes its frame to "
    "100vh and scrolls inside; for keyboard games give the canvas tabindex=\"0\", call focus() on load and on click, "
    "and show a \"click to focus\" hint.\n"
    "- Keyboard, pointer, wheel and resize events, requestAnimationFrame, <select> changes and element.animate all work."
)

FIX_SYSTEM = (
    "This is an automatic repair turn from the preview sandbox. Reply with only the complete corrected page in one "
    "```html fence; no explanation before or after it."
)

# Written readable, collapsed to one line by _one_line so a browser line number equals the page's own line number.
# Every reporting path is capped (MAX posts) and de-duplicated (once) because the channel is attacker-controlled.
_SHIM_JS = r"""
(function(){
var SEQ=__SEQ__,sent=0,MAX=200,seen={};
function clip(v,n){v=v==null?'':String(v);return v.length>n?v.slice(0,n):v;}
function text(a){if(a instanceof Error){return clip(a.stack||a.message||a,800);}if(typeof a==='string'){return clip(a,500);}try{return clip(JSON.stringify(a),500);}catch(e){return clip(String(a),500);}}
function post(kind,data){if(sent>=MAX){return;}sent++;var m={chatJohnsonPreview:true,seq:SEQ,kind:kind};for(var k in data){m[k]=data[k];}try{window.parent.postMessage(m,'*');}catch(e){}}
function once(key,kind,data){if(seen[key]){return;}seen[key]=1;post(kind,data);}
var FORM='form submission (the sandbox never fires submit; use a button click handler and an Enter keydown handler)';
window.addEventListener('error',function(e){var t=e.target;if(t&&t!==window&&t.tagName){var u=clip(t.src||t.href||'',200);once('res:'+u,'blocked',{what:'resource',detail:t.tagName.toLowerCase()+' '+u});return;}post('error',{message:clip(e.message||(e.error&&e.error.message)||'error',500),line:e.lineno||0,column:e.colno||0,stack:clip(e.error&&e.error.stack,800)});},true);
window.addEventListener('unhandledrejection',function(e){var r=e.reason||{};post('error',{message:clip(r.message||r,500),line:0,column:0,stack:clip(r.stack,800),rejection:true});});
document.addEventListener('securitypolicyviolation',function(e){once('csp:'+e.violatedDirective+':'+e.blockedURI,'blocked',{what:'csp',detail:e.violatedDirective+' '+clip(e.blockedURI,200),line:e.lineNumber||0});});
['error','warn'].forEach(function(level){var orig=console[level]?console[level].bind(console):function(){};console[level]=function(){var parts=[];for(var i=0;i<arguments.length;i++){parts.push(text(arguments[i]));}post('console',{level:level,text:clip(parts.join(' '),500)});orig.apply(console,arguments);};});
function deny(what,ret){return function(){once('api:'+what,'blocked',{what:'api',detail:what});return ret;};}
window.alert=deny('alert()',undefined);window.confirm=deny('confirm()',false);window.prompt=deny('prompt()',null);window.open=deny('window.open()',null);window.print=deny('window.print()',undefined);
try{var rtc=function(){once('api:rtc','blocked',{what:'api',detail:'RTCPeerConnection'});throw new Error('RTCPeerConnection is not available in the preview sandbox');};['RTCPeerConnection','webkitRTCPeerConnection','RTCDataChannel'].forEach(function(n){try{Object.defineProperty(window,n,{value:rtc,writable:false,configurable:false});}catch(e){}});}catch(e){}
try{if(navigator.clipboard){var cb={};['writeText','readText','write','read'].forEach(function(n){cb[n]=function(){once('api:clipboard','blocked',{what:'api',detail:'navigator.clipboard'});return Promise.reject(new Error('the clipboard is not available in the preview sandbox'));};});Object.defineProperty(navigator,'clipboard',{value:cb,configurable:true});}}catch(e){}
try{HTMLFormElement.prototype.submit=function(){once('api:form','blocked',{what:'api',detail:FORM});};}catch(e){}
document.addEventListener('click',function(e){if(e.defaultPrevented){return;}var t=e.target&&e.target.closest?e.target.closest('button,input,a'):null;if(!t){return;}if(t.tagName==='A'&&t.hasAttribute('download')){once('api:download','blocked',{what:'api',detail:'file download (a[download])'});}if(t.form&&(t.type==='submit'||t.type==='image')){once('api:form','blocked',{what:'api',detail:FORM});}});
document.addEventListener('keydown',function(e){if(e.defaultPrevented){return;}var t=e.target;if(e.key==='Enter'&&t&&t.form&&t.tagName==='INPUT'){once('api:form','blocked',{what:'api',detail:FORM});}});
document.addEventListener('dragstart',function(){once('api:dnd','blocked',{what:'api',detail:'HTML5 drag-and-drop (drops never arrive in the sandbox; use pointer events)'});},true);
var HAZARD='iframe,frame,object,embed,link[rel~=preconnect],link[rel~=dns-prefetch],link[rel~=prefetch],link[rel~=preload],link[rel~=prerender],link[rel~=modulepreload],base,meta[http-equiv=refresh i]';
function sweep(root){try{var nodes=root.querySelectorAll?root.querySelectorAll(HAZARD):[];for(var i=0;i<nodes.length;i++){var n=nodes[i];once('haz:'+n.tagName+':'+(n.getAttribute('rel')||n.getAttribute('src')||''),'blocked',{what:'api',detail:'removed a '+n.tagName.toLowerCase()+' element the sandbox cannot police'});n.parentNode&&n.parentNode.removeChild(n);}}catch(e){}}
try{new MutationObserver(function(list){for(var i=0;i<list.length;i++){var added=list[i].addedNodes;for(var j=0;j<added.length;j++){if(added[j].nodeType===1){sweep(added[j].parentNode||document);}}}}).observe(document.documentElement,{childList:true,subtree:true});}catch(e){}
function blank(){var b=document.body;if(!b){return true;}var t=(b.innerText||'').replace(/\s+/g,'');return !t&&!b.querySelector('canvas,svg,img,video');}
window.addEventListener('load',function(){sweep(document);setTimeout(function(){var b=document.body;post('ready',{title:clip(document.title,120),elements:b?b.querySelectorAll('*').length:0,text:clip(b?b.innerText:'',300),blank:blank()});},400);});
})();
"""


def _one_line(script: str) -> str:
    return "".join(line.strip() for line in script.strip().splitlines())


SHIM = _one_line(_SHIM_JS)

_THREE_IMPORT = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(\s*|\bimport\s+)['"]three['"]""")
# An attribute-aware start tag: quoted values may contain '>' and attributes come in any order.
_TAG = re.compile(
    r"<(?P<name>link|meta|base|script)\b(?P<attrs>(?:\s+[^\s=>/]+(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+))?)*)\s*/?>",
    re.I,
)
_ATTR = re.compile(r"([^\s=>/]+)(?:\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+)))?")
_SCRIPT_END = re.compile(r"</script\s*>", re.I)
_HINT_RELS = {"dns-prefetch", "preconnect", "prefetch", "preload", "prerender", "modulepreload"}


def sandbox_insert(seq: int) -> str:
    """The head content the component prepends to the page: CSP, no DNS prefetch, and the talk-back shim. One line."""
    return (
        f'<meta http-equiv="Content-Security-Policy" content="{CHILD_CSP}">'
        '<meta http-equiv="x-dns-prefetch-control" content="off">'
        f"<script>{SHIM.replace('__SEQ__', str(int(seq)))}</script>"
    )


def three_import_map(data_url: str) -> str:
    return '<script type="importmap">' + json.dumps({"imports": {"three": data_url}}) + "</script>"


def build_document(source: str, insert: str, three_data_url: Optional[str] = None) -> str:
    """The nested document exactly as the component builds it (kept here so tests and the harness share one rule).

    A wrapper head carrying the insert (plus the import map when the page imports 'three') is prepended on the page's
    first line, so the shim runs before anything the page wrote and every page line keeps its number. The page's own
    doctype, ``<html>`` and ``<head>`` tags that follow are parse errors the HTML parser ignores, and their children
    (title, style, script, meta) are merged into the head that already exists.
    """
    head_extra = insert + (three_import_map(three_data_url) if three_data_url else "")
    return "<!doctype html><html><head>" + head_extra + "</head>" + source


def needs_three(source: str) -> bool:
    return bool(_THREE_IMPORT.search(source))


def _attributes(raw: str) -> Dict[str, str]:
    found: Dict[str, str] = {}
    for match in _ATTR.finditer(raw):
        name = match.group(1).lower()
        value = next((v for v in match.groups()[1:] if v is not None), "")
        found.setdefault(name, value)
    return found


def strip_hazards(source: str) -> Tuple[str, List[str]]:
    """Remove the well-formed tags the CSP cannot police (resource hints, meta refresh, import maps, base).

    An aid for the pages models actually write, not a boundary: the sandbox and the CSP are the boundary, and the
    shim's runtime sweep catches the same elements when a script adds them later.
    """
    notes: List[str] = []
    counts: Dict[str, int] = {}
    out: List[str] = []
    cursor = 0
    for match in _TAG.finditer(source):
        name = match.group("name").lower()
        attrs = _attributes(match.group("attrs"))
        end = match.end()
        label = ""
        if name == "link" and _HINT_RELS & set(attrs.get("rel", "").lower().split()):
            label = "a resource hint link"
        elif name == "meta" and attrs.get("http-equiv", "").lower() == "refresh":
            label = "a meta refresh"
        elif name == "base":
            label = "a base tag"
        elif name == "script" and attrs.get("type", "").lower() == "importmap":
            closing = _SCRIPT_END.search(source, end)
            end = closing.end() if closing else len(source)
            label = "a page-authored import map"
        if not label or match.start() < cursor:
            continue
        out.append(source[cursor:match.start()])
        cursor = end
        counts[label] = counts.get(label, 0) + 1
    out.append(source[cursor:])
    for label, count in counts.items():
        notes.append(label if count == 1 else f"{count} {label}s")
    return "".join(out), notes


def page_hash(source: str) -> str:
    return hashlib.sha256(source.strip().encode("utf-8", "replace")).hexdigest()[:16]


def _clip(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    return text[:limit]


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def normalize_report(raw: Any) -> Optional[Dict[str, Any]]:
    """Coerce the component's return value into one trusted shape; None when it is not a report at all.

    Everything is clipped, capped and de-duplicated: the page can forge or flood the channel, so the app only ever
    shows text and never acts on a count the page chose.
    """
    if not isinstance(raw, Mapping) or "seq" not in raw:
        return None
    errors: List[Dict[str, Any]] = []
    seen_errors = set()
    for item in list(raw.get("errors") or [])[:60]:
        if not isinstance(item, Mapping):
            continue
        entry = {
            "message": _clip(item.get("message"), 500) or "error",
            "line": _int(item.get("line")),
            "column": _int(item.get("column")),
            "stack": _clip(item.get("stack"), 800),
        }
        key = (entry["message"], entry["line"])
        if key in seen_errors:
            continue
        seen_errors.add(key)
        errors.append(entry)
        if len(errors) >= 20:
            break
    csp_urls = set()
    blocked: List[str] = []
    resources: List[str] = []
    for item in list(raw.get("blocked") or [])[:60]:
        if isinstance(item, Mapping):
            what, detail = _clip(item.get("what"), 40), _clip(item.get("detail"), 260)
        else:
            what, detail = "", _clip(item, 260)
        if not detail:
            continue
        if what == "csp":
            csp_urls.add(detail.split(" ", 1)[-1])
            blocked.append(detail)
        elif what == "resource":
            resources.append(detail)
        else:
            blocked.append(detail)
    for detail in resources:  # a blocked resource that CSP already reported is the same fact twice
        if detail.split(" ", 1)[-1] not in csp_urls:
            blocked.append(detail)
    blocked = list(dict.fromkeys(blocked))[:20]
    console: List[str] = []
    for item in list(raw.get("console") or [])[:60]:
        if isinstance(item, Mapping):
            text = _clip(item.get("text"), 500)
            line = f"{_clip(item.get('level'), 8) or 'log'}: {text}" if text.strip() else ""
        else:
            line = _clip(item, 500)
        if line.strip():
            console.append(line)
    console = list(dict.fromkeys(console))[:30]
    status = _clip(raw.get("status"), 12)
    if status not in STATUSES:
        status = "error" if errors else "ready"
    return {
        "seq": _int(raw.get("seq")),
        "page": _clip(raw.get("page"), 32),
        "status": status,
        "errors": errors,
        "blocked": blocked,
        "console": console,
        "elements": _int(raw.get("elements")),
        "text": _clip(raw.get("text"), 300),
        "ms": _int(raw.get("ms")),
    }


def error_signature(report: Mapping[str, Any]) -> str:
    """Stable digest of what went wrong: the same errors and blocked items in any order give the same signature."""
    facts = sorted({(e["message"][:160], int(e["line"])) for e in report.get("errors", [])})
    blocked = sorted(set(report.get("blocked", [])))
    status = report.get("status", "") if report.get("status") in FIX_STATUSES else ""
    return hashlib.sha256(json.dumps([status, facts, blocked]).encode()).hexdigest()[:16]


def report_summary(report: Mapping[str, Any]) -> str:
    status = report.get("status", "ready")
    if status == "blank":
        return "blank page (no text, canvas or image rendered)"
    if status == "timeout":
        return "no ready signal (a blocking loop or a script that never finishes loading)"
    if status == "navigated":
        return "the page tried to navigate away (refused)"
    parts = []
    if report.get("errors"):
        parts.append(f"{len(report['errors'])} error(s)")
    if report.get("blocked"):
        parts.append(f"{len(report['blocked'])} blocked")
    if status == "blocked":
        parts.append("the page depends on external scripts or styles")
    return " · ".join(parts) if parts else "ran clean"


INCOMPLETE_REASON = "the page is incomplete (its answer was cut at the length limit), not broken: it is continued, not repaired"
SAME_ERROR_REASON = "repair stopped: the rewrite broke in the same place, which usually means the answer is being cut off"

_PLAIN_SCRIPT_ERRORS = (
    (re.compile(r"unexpected end of (input|script)|unterminated (template|string|regexp)|missing \} |unexpected token '?\}?'? *$", re.I),
     "the page's code stops mid-way: the answer was cut off before the code finished"),
    (re.compile(r"(\S+) is not defined", re.I), "the page calls {0}, which it never defined"),
    (re.compile(r"cannot read propert(?:y|ies) of (?:null|undefined)|null is not an object|undefined is not an object", re.I),
     "the page looks for an element or value that is not there"),
    (re.compile(r"failed to resolve module|failed to fetch|importing a module script|refused to (load|connect)|blocked by content security policy", re.I),
     "the page tries to load something from the internet, which the sandbox never allows: everything must be inline"),
    (re.compile(r"(\S+) is not a function", re.I), "the page calls {0} as a function, but it is not one"),
)


def plain_script_error(message: str) -> str:
    """The browser's words for a script error, said the operator's way; the raw message stays for the model."""
    text = " ".join(str(message or "").split())
    for pattern, plain in _PLAIN_SCRIPT_ERRORS:
        match = pattern.search(text)
        if match:
            return plain.format(*[group for group in match.groups() if group]) if match.groups() and "{0}" in plain else plain
    return text


def fix_decision(
    state: Mapping[str, Any], report: Optional[Mapping[str, Any]], heavy: bool, generating: bool, keyed: bool = True,
    complete: bool = True,
) -> Tuple[bool, str, bool]:
    """Whether a report earns an automatic fix turn: (allowed, reason shown to the operator, settled).

    ``settled`` says the report needs no further consideration (it is clean, capped, repeated, or acted on); a
    transient refusal (Heavy Mode off, no key, a generation running) leaves it open so the next rerun decides again.
    An incomplete page (``complete`` False: cut at the output budget) is never repaired; a repair of a cut page
    is a smaller cut page, which is the spiral this rule ends.
    """
    if report is None:
        return False, "no report", True
    if int(report.get("seq", 0)) <= int(state.get("handled_seq", 0)):
        return False, "already handled", True
    if report.get("status") not in FIX_STATUSES:
        return False, "ran clean", True
    if not complete:
        return False, INCOMPLETE_REASON, True
    rounds = int(state.get("rounds", 0))
    # Repair is no longer gated on Heavy Mode. A page that throws gets one automatic round in either mode, because a
    # broken page is the common case and Heavy Mode is off by default; Heavy Mode buys the second round.
    limit = FIX_ROUNDS if heavy else NORMAL_FIX_ROUNDS
    if rounds >= limit:
        extra = "; turn on Heavy Mode for another round" if limit < FIX_ROUNDS else ""
        return False, f"repair stopped: {limit} automatic round(s) used{extra}; {NEXT_STEP}", True
    if error_signature(report) == state.get("last_signature"):
        return False, f"{SAME_ERROR_REASON}; {NEXT_STEP}", True
    if not keyed:
        return False, "no provider key is configured, so nothing is fixed automatically", False
    if generating:
        return False, "a generation is already running; the fix waits for it", False
    return True, f"automatic fix round {rounds + 1} of {limit}", True


def _status_line(report: Mapping[str, Any]) -> str:
    status = report.get("status", "error")
    if status == "blank":
        return "blank: the page rendered no text, canvas, image or video"
    if status == "timeout":
        return f"timeout: no ready signal within {READY_TIMEOUT_MS // 1000} s (a blocking loop, or a script that never finishes)"
    if status == "navigated":
        return "navigated: the page tried to load another address, which the sandbox refuses"
    if status == "blocked":
        return "blocked: the page depends on external scripts or styles that the sandbox refuses; inline them"
    return "error"


def fix_prompt(source: str, report: Mapping[str, Any], round_no: int, rounds: int = FIX_ROUNDS) -> str:
    """The user turn of an automatic fix: the report, then (after FIX_MARKER) the brief and the page as one fence."""
    lines = [f"{FIX_PREFIX}{round_no}/{rounds}: the page you generated was run in the preview sandbox and did not work.", ""]
    lines.append(f"Sandbox report (status: {_status_line(report)})")
    errors = list(report.get("errors", []))
    lines.append(f"Errors ({len(errors)}):")
    for entry in errors:
        where = f"line {entry['line']}" + (f" col {entry['column']}" if entry.get("column") else "") if entry.get("line") else "line ?"
        lines.append(f"- {where}: {entry['message']}")
        stack = [s.strip() for s in str(entry.get("stack") or "").splitlines() if s.strip()][1:4]
        if stack:
            lines.append("  stack: " + " | ".join(stack))
    blocked = list(report.get("blocked", []))
    lines.append(f"Blocked ({len(blocked)}): " + ("; ".join(blocked) if blocked else "none"))
    console = list(report.get("console", []))[-20:]
    lines.append(f"Console ({len(console)} of the last lines):")
    lines.extend(f"- {line}" for line in console)
    if not console:
        lines.append("- (empty)")
    lines.extend([
        "",
        FIX_MARKER,
        "Line numbers are the page's own lines exactly as fenced below. Fix every reported error at its cause, and in "
        "the same pass remove every remaining use of storage, network, dialogs, form submission, downloads, clipboard "
        "and external URLs: the script stopped at the first error, so later ones are not listed yet. Keep every "
        "feature that already works and follow the PREVIEW RULES. Return the COMPLETE corrected page as exactly ONE "
        "```html fence and nothing else: no explanation, no second fence, no partial excerpt, no \"rest unchanged\".",
        "",
        "```html",
        source.strip(),
        "```",
    ])
    return "\n".join(lines)


def fix_report_part(prompt: str) -> str:
    """The operator-facing part of a stored fix turn: the report only, never the brief or the page it carries."""
    head = prompt.split(FIX_MARKER, 1)[0]
    return head.rsplit("```html", 1)[0].strip()

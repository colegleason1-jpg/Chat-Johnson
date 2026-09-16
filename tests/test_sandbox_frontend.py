"""Batch T: the Run-mode component (frontend/sandbox_preview) in a real Chromium under its real CSP, no bypass."""
import http.server
import io
import os
import shutil
import sys
import tempfile
import threading
import time

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from orchestrator.sandbox_preview import COMPONENT_CSP, sandbox_insert  # noqa: E402

CHROME = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
CHROME_ARGS = ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]
FRONTEND = os.path.join(ROOT, "frontend", "sandbox_preview")
STREAMLIT_SANDBOX = "allow-same-origin allow-scripts allow-downloads allow-forms allow-modals allow-popups allow-popups-to-escape-sandbox"

pytestmark = pytest.mark.skipif(not os.path.exists(CHROME), reason="Playwright Chromium build is not installed")

# Plays Streamlit: renders on componentReady, collects component values, and records any Streamlit-shaped message
# whose source is not the component frame (what a nested page would have to do to spoof a value).
HOST_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>host</title></head><body>
<iframe id="component" src="./index.html" sandbox="%s" style="width:820px;height:600px;border:0"></iframe>
<script>
var IS = "isStreamlitMessage";
window.__values = []; window.__foreign = []; window.__heights = []; window.__ready = false; window.__queue = [];
var frame = document.getElementById("component");
function send(args) { frame.contentWindow.postMessage({type: "streamlit:render", args: args}, "*"); }
window.__render = function (args) { if (window.__ready) { send(args); } else { window.__queue.push(args); } };
window.addEventListener("message", function (event) {
  var d = event.data;
  if (!d || typeof d !== "object" || !d[IS]) { return; }
  if (event.source !== frame.contentWindow) { window.__foreign.push({type: d.type, value: d.value === undefined ? null : d.value}); return; }
  if (d.type === "streamlit:componentReady") { window.__ready = true; window.__queue.forEach(send); window.__queue = []; }
  else if (d.type === "streamlit:setComponentValue") { window.__values.push(d.value); }
  else if (d.type === "streamlit:setFrameHeight") { window.__heights.push(d.height); }
});
</script></body></html>""" % STREAMLIT_SANDBOX

CLEAN = "<!doctype html><html><head><title>ok</title></head><body><h1>Hello</h1><p>sandbox</p></body></html>"
LINE9 = "<!doctype html>\n<html>\n<head>\n<title>t</title>\n</head>\n<body>\n<script>\nlet x = 1;\nfoo();\n</script>\n</body>\n</html>"
CUBE = """<!doctype html><html><head><title>cube</title><style>html,body{margin:0;background:#101820}canvas{display:block}</style></head><body>
<script type="module">
import * as THREE from 'three';
const w = 640, h = 400;
const renderer = new THREE.WebGLRenderer({antialias: true});
renderer.setSize(w, h);
document.body.appendChild(renderer.domElement);
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x101820);
const camera = new THREE.PerspectiveCamera(50, w / h, 0.1, 100);
camera.position.set(2.2, 1.8, 3);
camera.lookAt(0, 0, 0);
const cube = new THREE.Mesh(new THREE.BoxGeometry(1.4, 1.4, 1.4), new THREE.MeshStandardMaterial({color: 0xff7043, roughness: 0.4}));
cube.rotation.set(0.5, 0.8, 0);
scene.add(cube);
const ball = new THREE.Mesh(new THREE.SphereGeometry(0.5, 32, 16), new THREE.MeshStandardMaterial({color: 0x40c0ff}));
ball.position.x = -1.6;
scene.add(ball);
const light = new THREE.DirectionalLight(0xffffff, 3);
light.position.set(3, 4, 2);
scene.add(light);
scene.add(new THREE.AmbientLight(0x4060ff, 0.8));
renderer.render(scene, camera);
</script></body></html>"""
BLOCKED = """<!doctype html><html><head><title>blocked</title>
<script src="https://cdn.tailwindcss.com"></script>
</head><body>
<form id="f"><input id="q" value="x"><button id="go" type="submit">Go</button></form>
<script>alert('x'); document.getElementById('go').click();</script>
<script>fetch('https://example.com').catch(function () {});</script>
<script>localStorage.getItem('k');</script>
</body></html>"""
NAVIGATE = "<!doctype html><html><head><title>nav</title></head><body><p>leaving</p><script>setTimeout(function () { location.href = 'https://example.com/'; }, 300);</script></body></html>"
HANG = "<!doctype html><html><head><title>hang</title></head><body><p>spin</p><script>while (true) {}</script></body></html>"
TAILWIND = """<!doctype html><html><head><title>tw</title>
<script src="https://cdn.tailwindcss.com"></script>
</head><body><p>needs tailwind</p></body></html>"""
REMOTE_IMG = '<!doctype html><html><head><title>img</title></head><body><p>picture</p><img src="https://example.com/x.png" alt="x"></body></html>'
CAUGHT = "<!doctype html><html><head><title>caught</title></head><body><p>caught</p><script>try { nope(); } catch (err) { document.body.append(String(err)); console.error(err); }</script></body></html>"
HOSTILE = "<html><script>window.__RTC = RTCPeerConnection; try { new __RTC({iceServers:[{urls:'stun:127.0.0.1:1'}]}); window.__ok = 1 } catch (e) { window.__ok = 0 }</script><head></head><body><p>x</p></body></html>"
LATE_HAZARDS = """<!doctype html><html><head><title>haz</title></head><body><p>late</p>
<script>window.addEventListener('load', function () { setTimeout(function () {
  var l = document.createElement('link'); l.rel = 'preconnect'; l.href = 'https://x.test'; document.head.appendChild(l);
  var f = document.createElement('iframe'); f.srcdoc = ''; document.body.appendChild(f);
}, 50); });</script></body></html>"""
CLICK_THROWS = "<!doctype html><html><head><title>click</title></head><body><button id=\"boom\">boom</button><script>document.getElementById('boom').addEventListener('click', function () { missing(); });</script></body></html>"
SPOOF = """<!doctype html><html><head><title>spoof</title></head><body><p>spoof</p>
<script>var m = {isStreamlitMessage: true, type: 'streamlit:setComponentValue', value: {fake: true}, dataType: 'json'};
window.top.postMessage(m, '*'); window.parent.postMessage(m, '*');</script></body></html>"""


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def served():
    root = tempfile.mkdtemp(prefix="sandbox_frontend_")
    served_dir = os.path.join(root, "site")
    shutil.copytree(FRONTEND, served_dir)
    with open(os.path.join(served_dir, "host.html"), "w", encoding="utf-8") as handle:
        handle.write(HOST_HTML)

    def handler(*args, **kwargs):
        return _QuietHandler(*args, directory=served_dir, **kwargs)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        instance = pw.chromium.launch(executable_path=CHROME, args=CHROME_ARGS)
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture
def page(browser, served):
    context = browser.new_context(viewport={"width": 900, "height": 700})
    host = context.new_page()
    host.goto(served + "/host.html", wait_until="load")
    try:
        yield host
    finally:
        context.close()


def render(page, source, seq=1, page_id="p1", three=False, height=480):
    args = {"seq": seq, "page": page_id, "source": source, "insert": sandbox_insert(seq), "three": three, "height": height}
    page.evaluate("(args) => window.__render(args)", args)


def values(page):
    return page.evaluate("() => window.__values")


def wait_values(page, count, timeout=12.0, where=None):
    """Polls with evaluate (wait_for_function needs 'unsafe-eval', which the component CSP refuses)."""
    deadline = time.time() + timeout
    while True:
        got = values(page)
        if len(got) >= count and (where is None or any(where(v) for v in got)):
            return got
        if time.time() > deadline:
            pytest.fail("expected %d component value(s) within %.0f s, got %r" % (count, timeout, got))
        time.sleep(0.05)


def test_clean_page_reports_ready(page):
    render(page, CLEAN)
    got = wait_values(page, 1)
    time.sleep(0.6)
    got = values(page)
    assert len(got) == 1, "a clean page must produce exactly one report, got %r" % got
    report = got[0]
    assert report["status"] == "ready", "clean page status should be ready: %r" % report
    assert report["seq"] == 1 and report["page"] == "p1", "seq and page must echo the render args: %r" % report
    assert report["elements"] > 0 and report["ms"] > 0, "ready must carry an element count and elapsed ms: %r" % report
    assert report["errors"] == [] and report["blocked"] == [], "clean page must report nothing blocked: %r" % report
    heights = page.evaluate("() => window.__heights")
    assert heights and heights[-1] > 480, "setFrameHeight must add the status bar to the frame height: %r" % heights


def test_uncaught_error_keeps_the_page_line_number(page):
    render(page, LINE9)
    report = wait_values(page, 1)[0]
    assert report["status"] == "error", "a ReferenceError must give status error: %r" % report
    first = report["errors"][0]
    assert first["line"] == 9, "line 9 of the page must be reported as line 9 (no splice offset): %r" % first
    assert "foo" in first["message"], "the message must name the missing function: %r" % first


def test_three_import_renders_a_lit_cube(page):
    from PIL import Image

    render(page, CUBE, three=True)
    report = wait_values(page, 1, timeout=20.0)[0]
    assert report["status"] == "ready", "the three.js page must settle ready: %r" % report
    assert report["errors"] == [], "importing 'three' from the import map must not error: %r" % report["errors"]
    shot = page.locator("#component").screenshot()
    image = Image.open(io.BytesIO(shot)).convert("RGB")
    colours = image.getcolors(maxcolors=1 << 20)
    assert colours is not None and len(colours) > 24, "a lit cube must paint more than a handful of colours, got %s" % (len(colours) if colours else None)


def test_sandbox_refusals_are_named(page):
    render(page, BLOCKED)
    report = wait_values(page, 1)[0]
    time.sleep(0.6)
    assert len(values(page)) == 1, "one report per run: %r" % values(page)
    assert report["status"] == "error", "a SecurityError outranks the blocked CDN script: %r" % report
    details = [b["detail"] for b in report["blocked"]]
    assert any("form submission" in d for d in details), "a submit click must be reported as blocked: %r" % details
    assert any("alert()" in d for d in details), "alert() must be reported as blocked: %r" % details
    assert any(b["what"] == "csp" and "cdn.tailwindcss.com" in b["detail"] for b in report["blocked"]), "the CDN script must be a CSP entry: %r" % details
    assert any(b["what"] == "csp" and "example.com" in b["detail"] for b in report["blocked"]), "the fetch must be a CSP entry: %r" % details
    assert any("SecurityError" in e["message"] and "localStorage" in e["message"] for e in report["errors"]), "localStorage must throw a SecurityError: %r" % report["errors"]


def test_self_navigation_is_refused_and_never_reaches_the_host(page):
    render(page, NAVIGATE)
    report = wait_values(page, 1)[0]
    mentions = [b["detail"] for b in report["blocked"]] + [e["message"] for e in report["errors"]]
    refused = report["status"] == "navigated" or any("navigat" in m or "frame-src" in m for m in mentions)
    assert refused, "a self-navigation must be reported as refused: %r" % report
    assert page.evaluate("() => window.__foreign") == [], "nothing from the nested frame may reach the host"
    assert page.evaluate("() => location.href").endswith("/host.html"), "the host must stay where it was"


def test_blocking_loop_times_out_without_freezing_the_host(page):
    render(page, HANG)
    report = wait_values(page, 1, timeout=11.0)[0]
    assert report["status"] == "timeout", "a page that never signals ready must time out: %r" % report
    started = time.time()
    assert page.evaluate("1 + 1") == 2
    assert time.time() - started < 2.0, "the host page must stay responsive while the nested frame spins"


def test_same_render_is_ignored_and_a_new_seq_reruns(page):
    render(page, CLEAN)
    wait_values(page, 1)
    render(page, CLEAN)
    time.sleep(1.5)
    assert len(values(page)) == 1, "re-sending the same seq and page must not produce another report"
    render(page, CLEAN, seq=2)
    got = wait_values(page, 2)
    assert got[-1]["seq"] == 2 and got[-1]["status"] == "ready", "a new seq must run again: %r" % got


def test_spoofed_component_values_are_not_relayed(page):
    render(page, SPOOF)
    got = wait_values(page, 1)
    time.sleep(0.6)
    got = values(page)
    assert all(not (isinstance(v, dict) and v.get("fake")) for v in got), "a spoofed value must never be accepted: %r" % got
    assert got[0]["status"] == "ready" and got[0]["seq"] == 1, "the genuine report must still arrive: %r" % got
    foreign = page.evaluate("() => window.__foreign")
    assert any(f["value"] == {"fake": True} for f in foreign), "the spoof must arrive from the nested window, not the component: %r" % foreign


def test_external_script_dependency_settles_blocked_and_a_remote_image_stays_ready(page):
    render(page, TAILWIND)
    report = wait_values(page, 1)[0]
    assert report["status"] == "blocked", "a refused script-src dependency must settle as blocked: %r" % report
    assert report["errors"] == [], "a blocked script is not an error: %r" % report["errors"]
    assert any(b["what"] == "csp" and b["detail"].startswith("script-src") for b in report["blocked"]), "the CSP entry must name script-src: %r" % report["blocked"]
    render(page, REMOTE_IMG, seq=2)
    got = wait_values(page, 2)
    report = got[-1]
    assert report["status"] == "ready", "a blocked image alone must leave the page ready: %r" % report
    assert any(b["what"] == "csp" and b["detail"].startswith("img-src") for b in report["blocked"]), "the image must still be listed as blocked: %r" % report["blocked"]


def test_console_error_counts_as_an_error(page):
    render(page, CAUGHT)
    report = wait_values(page, 1)[0]
    assert report["status"] == "error", "a caught error reported with console.error must give status error: %r" % report
    assert report["errors"] == [], "nothing was uncaught: %r" % report["errors"]
    assert any(c["level"] == "error" and "nope" in c["text"] for c in report["console"]), "the console entry must carry the error: %r" % report["console"]


def test_script_before_head_cannot_beat_the_shim_to_rtcpeerconnection(page):
    render(page, HOSTILE)
    report = wait_values(page, 1)[0]
    assert report["status"] in ("ready", "error"), "the hostile page settles without a working peer connection: %r" % report
    assert any("RTCPeerConnection" in b["detail"] for b in report["blocked"]), "the shim must have shadowed RTCPeerConnection first: %r" % report["blocked"]
    ok = page.frame_locator("#component").frame_locator("iframe").locator("body").evaluate("() => window.__ok")
    assert ok == 0, "constructing a peer connection must throw inside the page, got __ok=%r" % ok


def test_hazards_added_after_load_are_swept(page):
    render(page, LATE_HAZARDS)
    report = wait_values(page, 1)[0]
    removed = [b["detail"] for b in report["blocked"] if b["detail"].startswith("removed a ")]
    assert any("link" in d for d in removed), "the late preconnect link must be removed and named: %r" % report["blocked"]
    assert any("iframe" in d for d in removed), "the late iframe must be removed and named: %r" % report["blocked"]


def test_events_after_settle_never_post_a_second_value(page):
    render(page, CLICK_THROWS)
    report = wait_values(page, 1)[0]
    assert report["status"] == "ready", "the page settles ready before any click: %r" % report
    page.frame_locator("#component").frame_locator("iframe").locator("#boom").click()
    time.sleep(1.5)
    assert len(values(page)) == 1, "a click that throws after settle must not produce a second component value: %r" % values(page)
    status = page.frame_locator("#component").locator("#status").text_content()
    assert "after interaction" in status and "1 error" in status, "the late error must show in the status bar only: %r" % status


def test_component_csp_is_verbatim_in_index_html():
    with open(os.path.join(FRONTEND, "index.html"), encoding="utf-8") as handle:
        html = handle.read()
    assert 'content="%s"' % COMPONENT_CSP in html, "index.html must carry orchestrator.sandbox_preview.COMPONENT_CSP verbatim"
    with open(os.path.join(FRONTEND, "main.js"), encoding="utf-8") as handle:
        js = handle.read()
    assert 'setAttribute("sandbox", "allow-scripts")' in js and "allow-same-origin" not in js, "the nested frame may carry only allow-scripts"

/* Run-mode preview component: prepends the Python-owned insert to the page, hosts it in an opaque-origin
   sandboxed frame, aggregates the shim's talk-back and returns exactly one report per run to Streamlit. Nothing from
   the nested frame is ever relayed to the parent: the only messages this document sends are its own Streamlit ones. */
(function () {
  "use strict";
  var IS = "isStreamlitMessage";
  var READY_TIMEOUT_MS = 8000; // orchestrator.sandbox_preview.READY_TIMEOUT_MS
  var MAX_EVENTS = 200; // the shim's own cap; anything past it is a flood
  var statusBar = document.getElementById("status");
  var host = document.getElementById("host");
  var state = { seq: null, page: null };
  var run = null;
  var runCounter = 0;
  var threeLoading = null;

  function post(type, extra) {
    var message = {};
    message[IS] = true;
    message.type = type;
    for (var key in extra) {
      if (Object.prototype.hasOwnProperty.call(extra, key)) {
        message[key] = extra[key];
      }
    }
    window.parent.postMessage(message, "*");
  }

  function setStatus(text) {
    statusBar.textContent = text;
  }

  function clip(value, limit) {
    value = value == null ? "" : String(value);
    return value.length > limit ? value.slice(0, limit) : value;
  }

  function num(value) {
    value = Number(value);
    return isFinite(value) ? value : 0;
  }

  // Mirrors orchestrator.sandbox_preview.build_document byte for byte: the wrapper head is prepended on the page's
  // first line with no newline, so the shim runs before any page script and a browser line number is the page's own.
  function buildDocument(source, insert, threeDataUrl) {
    var extra = insert;
    if (threeDataUrl) {
      extra += '<script type="importmap">{"imports": {"three": ' + JSON.stringify(threeDataUrl) + "}}</script>";
    }
    return "<!doctype html><html><head>" + extra + "</head>" + source;
  }

  // The 900 KB Three.js data URL is loaded once, and only when a page imports 'three'.
  function loadThree() {
    if (window.__THREE_MODULE_DATA_URL) {
      return Promise.resolve();
    }
    if (!threeLoading) {
      threeLoading = new Promise(function (resolve) {
        var script = document.createElement("script");
        script.src = "./vendor/three.datauri.js";
        script.onload = function () { resolve(); };
        script.onerror = function () { threeLoading = null; resolve(); };
        document.head.appendChild(script);
      });
    }
    return threeLoading;
  }

  function postHeight(frameHeight) {
    post("streamlit:setFrameHeight", { height: frameHeight + statusBar.offsetHeight + 4 });
  }

  function hasConsoleError(r) {
    for (var i = 0; i < r.console.length; i++) {
      if (r.console[i].level === "error") {
        return true;
      }
    }
    return false;
  }

  // A refused external script or stylesheet means the page cannot work without it; blocked images, fonts and
  // connections are cosmetic and leave the page ready.
  function dependsOnExternal(r) {
    for (var i = 0; i < r.blocked.length; i++) {
      var b = r.blocked[i];
      if (b.what === "csp" && (b.detail.indexOf("script-src") === 0 || b.detail.indexOf("style-src") === 0)) {
        return true;
      }
    }
    return false;
  }

  // Precedence: error > navigated > blocked > blank > ready > timeout.
  function statusOf(r) {
    if (r.errors.length || hasConsoleError(r)) {
      return "error";
    }
    if (r.navigated) {
      return "navigated";
    }
    if (dependsOnExternal(r)) {
      return "blocked";
    }
    if (r.ready) {
      return r.blank ? "blank" : "ready";
    }
    return "timeout";
  }

  function countErrors(r) {
    var n = r.errors.length;
    for (var i = 0; i < r.console.length; i++) {
      if (r.console[i].level === "error") {
        n += 1;
      }
    }
    return n;
  }

  function statusText(r, status) {
    var tail = " · " + r.ms + " ms";
    var blocked = r.blocked.length ? " · " + r.blocked.length + " blocked" : "";
    if (status === "ready") {
      return "ready · " + r.elements + " elements" + blocked + tail;
    }
    if (status === "error") {
      var n = countErrors(r);
      return n + (n === 1 ? " error" : " errors") + blocked + tail;
    }
    if (status === "blocked") {
      return "blocked external script/style" + blocked + tail;
    }
    if (status === "blank") {
      return "blank page" + blocked + tail;
    }
    if (status === "navigated") {
      return "the page tried to navigate away (refused)";
    }
    return "no ready signal within " + Math.round(READY_TIMEOUT_MS / 1000) + " s" + blocked;
  }

  // Events after settle are shown, never posted: a second setComponentValue would abort the running script.
  function lateText(r) {
    var parts = [];
    if (r.late.errors) {
      parts.push(r.late.errors + (r.late.errors === 1 ? " error" : " errors"));
    }
    if (r.late.blocked) {
      parts.push(r.late.blocked + " blocked");
    }
    if (r.late.ready) {
      parts.push("ready");
    }
    if (!parts.length) {
      return "";
    }
    return " · then " + parts.join(", ") + " after interaction; press Render to report it";
  }

  function settle(r) {
    if (r.settled) {
      return;
    }
    r.settled = true;
    clearTimeout(r.timer);
    r.ms = Math.max(1, Math.round(performance.now() - r.start));
    var status = statusOf(r);
    r.settledText = statusText(r, status);
    setStatus(r.settledText);
    post("streamlit:setComponentValue", {
      value: {
        seq: r.seq,
        page: r.page,
        status: status,
        errors: r.errors.slice(),
        blocked: r.blocked.slice(),
        console: r.console.slice(),
        elements: r.elements,
        text: r.text,
        ms: r.ms
      },
      dataType: "json"
    });
  }

  function afterEvent(r, kind) {
    if (!r.settled) {
      if (r.ready || r.navigated) {
        settle(r);
      }
      return;
    }
    if (kind === "error") {
      r.late.errors += 1;
    } else if (kind === "blocked" || kind === "navigated") {
      r.late.blocked += 1;
    } else if (kind === "ready") {
      r.late.ready = true;
    }
    if (run === r) {
      setStatus(r.settledText + lateText(r));
    }
  }

  function markNavigated(r, detail) {
    if (r.navigated) {
      return;
    }
    r.navigated = true;
    if (detail) {
      r.blocked.push({ what: "csp", detail: clip(detail, 260), line: 0 });
    }
    afterEvent(r, "navigated");
  }

  function discard(r) {
    clearTimeout(r.timer);
    if (r.frame.parentNode) {
      r.frame.parentNode.removeChild(r.frame);
    }
  }

  function mount(id, args, height) {
    if (run) {
      discard(run);
      run = null;
    }
    var frame = document.createElement("iframe");
    frame.setAttribute("sandbox", "allow-scripts");
    frame.setAttribute("referrerpolicy", "no-referrer");
    frame.style.height = height + "px";
    var source = args.source == null ? "" : String(args.source);
    var insert = args.insert == null ? "" : String(args.insert);
    frame.srcdoc = buildDocument(source, insert, args.three ? window.__THREE_MODULE_DATA_URL : null);
    var r = {
      id: id, seq: args.seq, page: args.page, frame: frame, start: performance.now(), loads: 0, events: 0,
      errors: [], blocked: [], console: [], elements: 0, text: "", blank: false, ready: false, navigated: false,
      settled: false, settledText: "", late: { errors: 0, blocked: 0, ready: false }, ms: 0, timer: null
    };
    run = r;
    // A refused self-navigation replaces the srcdoc document with an error page, which loads a second time.
    frame.addEventListener("load", function () {
      if (run !== r) {
        return;
      }
      r.loads += 1;
      if (r.loads > 1) {
        markNavigated(r, "frame-src (the page navigated itself away)");
      }
    });
    host.appendChild(frame);
    r.timer = setTimeout(function () {
      if (run === r) {
        settle(r);
      }
    }, READY_TIMEOUT_MS);
  }

  function onRender(args) {
    var height = Math.max(120, Math.round(num(args.height)) || 480);
    postHeight(height);
    if (args.seq === state.seq && args.page === state.page) {
      return;
    }
    state.seq = args.seq;
    state.page = args.page;
    var id = ++runCounter;
    setStatus("Sandbox: running…");
    var ready = args.three ? loadThree() : Promise.resolve();
    ready.then(function () {
      if (id === runCounter) {
        mount(id, args, height);
      }
    });
  }

  function onFrameMessage(r, d) {
    if (r.events >= MAX_EVENTS) {
      return;
    }
    r.events += 1;
    var kind = d.kind;
    if (kind === "error") {
      r.errors.push({
        message: clip(d.message, 500) || "error",
        line: num(d.line),
        column: num(d.column),
        stack: clip(d.stack, 800)
      });
    } else if (kind === "blocked") {
      r.blocked.push({ what: clip(d.what, 40), detail: clip(d.detail, 260), line: num(d.line) });
    } else if (kind === "console") {
      var level = clip(d.level, 8);
      r.console.push({ level: level, text: clip(d.text, 500) });
      if (level === "error") {
        kind = "error";
      }
    } else if (kind === "ready") {
      r.ready = true;
      r.elements = num(d.elements);
      r.text = clip(d.text, 300);
      r.blank = d.blank === true;
    } else {
      return;
    }
    afterEvent(r, kind);
  }

  window.addEventListener("message", function (event) {
    var d = event.data;
    if (!d || typeof d !== "object") {
      return;
    }
    if (event.source === window.parent) {
      if (d.type === "streamlit:render") {
        onRender(d.args || {});
      }
      return;
    }
    var r = run;
    if (!r || event.source !== r.frame.contentWindow) {
      return;
    }
    if (d.chatJohnsonPreview !== true || d.seq !== state.seq) {
      return;
    }
    onFrameMessage(r, d);
  });

  // The nested frame's navigation is checked against this document's frame-src, so the refusal is reported here.
  document.addEventListener("securitypolicyviolation", function (event) {
    var directive = event.effectiveDirective || event.violatedDirective || "";
    if (run && directive.indexOf("frame-src") === 0) {
      markNavigated(run, "frame-src " + clip(event.blockedURI, 200));
    }
  });

  post("streamlit:componentReady", { apiVersion: 1 });
})();

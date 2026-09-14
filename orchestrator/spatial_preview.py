"""A self-contained 3D preview of a resolved scene: inline canvas, isometric boxes, drag to rotate, wheel to zoom.

Generated only from solver output (floats and escaped names), under the same strict CSP as the
sanitized canvas and with no external script, so it renders offline and on a shared host alike.
"""
from __future__ import annotations

import html
import json
from typing import Any, Mapping

CSP = "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; connect-src 'none'; frame-src 'none'; form-action 'none'"

_SCRIPT = """
(function () {
  const data = JSON.parse(document.getElementById('scene-data').textContent);
  const canvas = document.getElementById('scene');
  const ctx = canvas.getContext('2d');
  let angle = Math.PI / 4, scale = 1, dragging = false, lastX = 0;
  const room = data.room, objects = data.objects;
  function project(x, y, z) {
    const cx = room[0] / 2, cy = room[1] / 2;
    const rx = (x - cx) * Math.cos(angle) - (y - cy) * Math.sin(angle);
    const ry = (x - cx) * Math.sin(angle) + (y - cy) * Math.cos(angle);
    const base = Math.min(canvas.width, canvas.height) / (Math.max(room[0], room[1], room[2]) * 1.5) * scale;
    return [canvas.width / 2 + (rx - ry) * base * 0.866, canvas.height * 0.62 + (rx + ry) * base * 0.5 - z * base];
  }
  function poly(points, fill, stroke) {
    ctx.beginPath();
    points.forEach((p, i) => i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]));
    ctx.closePath(); ctx.fillStyle = fill; ctx.fill(); ctx.strokeStyle = stroke; ctx.lineWidth = 1; ctx.stroke();
  }
  function box(x, y, z, w, d, h, hue) {
    const c = (X, Y, Z) => project(X, Y, Z);
    const top = [c(x, y, z + h), c(x + w, y, z + h), c(x + w, y + d, z + h), c(x, y + d, z + h)];
    const front = [c(x, y + d, z), c(x + w, y + d, z), c(x + w, y + d, z + h), c(x, y + d, z + h)];
    const side = [c(x + w, y, z), c(x + w, y + d, z), c(x + w, y + d, z + h), c(x + w, y, z + h)];
    const back = [c(x, y, z), c(x + w, y, z), c(x + w, y, z + h), c(x, y, z + h)];
    const left = [c(x, y, z), c(x, y + d, z), c(x, y + d, z + h), c(x, y, z + h)];
    const faces = [[back, 55], [left, 60], [front, 70], [side, 45], [top, 85]];
    faces.forEach(([f, l]) => poly(f, `hsl(${hue} 60% ${l}%)`, `hsl(${hue} 50% 25%)`));
    return c(x + w / 2, y + d / 2, z + h);
  }
  function draw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    poly([project(0, 0, 0), project(room[0], 0, 0), project(room[0], room[1], 0), project(0, room[1], 0)], '#f1f4f8', '#8a97ab');
    poly([project(0, 0, 0), project(room[0], 0, 0), project(room[0], 0, room[2]), project(0, 0, room[2])], 'rgba(200,210,225,0.25)', '#b0bccb');
    poly([project(0, 0, 0), project(0, room[1], 0), project(0, room[1], room[2]), project(0, 0, room[2])], 'rgba(200,210,225,0.25)', '#b0bccb');
    const order = objects.map((o, i) => {
      const p = o.position, s = o.size;
      const cx = p[0] + s[0] / 2 - room[0] / 2, cy = p[1] + s[1] / 2 - room[1] / 2;
      const depth = cx * Math.sin(angle) + cy * Math.cos(angle) + cx * Math.cos(angle) - cy * Math.sin(angle);
      return [depth, i];
    }).sort((a, b) => a[0] - b[0]);
    ctx.font = '12px system-ui, sans-serif';
    order.forEach(([, i]) => {
      const o = objects[i];
      const label = box(o.position[0], o.position[1], o.position[2], o.size[0], o.size[1], o.size[2], (i * 47) % 360);
      ctx.fillStyle = '#1b2330'; ctx.textAlign = 'center'; ctx.fillText(o.name, label[0], label[1] - 6);
    });
  }
  canvas.addEventListener('mousedown', e => { dragging = true; lastX = e.clientX; });
  window.addEventListener('mouseup', () => { dragging = false; });
  canvas.addEventListener('mousemove', e => { if (dragging) { angle += (e.clientX - lastX) * 0.01; lastX = e.clientX; draw(); } });
  canvas.addEventListener('wheel', e => { e.preventDefault(); scale = Math.max(0.4, Math.min(3, scale * (e.deltaY < 0 ? 1.1 : 0.9))); draw(); }, { passive: false });
  canvas.addEventListener('touchstart', e => { if (e.touches.length) { dragging = true; lastX = e.touches[0].clientX; } }, { passive: true });
  canvas.addEventListener('touchmove', e => { if (dragging && e.touches.length) { angle += (e.touches[0].clientX - lastX) * 0.01; lastX = e.touches[0].clientX; draw(); } }, { passive: true });
  draw();
})();
"""


def scene_preview_document(scene: Mapping[str, Any], width: int = 900, height: int = 400) -> str:
    """HTML for one resolved scene (the ``scene_dict`` shape); names are escaped, numbers coerced."""
    room = [float(v) for v in scene.get("room", (10, 10, 3))][:3]
    objects = []
    for raw in scene.get("objects", [])[:200]:
        objects.append({
            "name": html.escape(str(raw.get("name", ""))[:60]),
            "size": [float(v) for v in raw.get("size", (1, 1, 1))][:3],
            "position": [float(v) for v in raw.get("position", (0, 0, 0))][:3],
        })
    payload = json.dumps({"room": room, "objects": objects}).replace("</", "<\\/")
    report = scene.get("report") or {}
    caption = html.escape(
        f"{len(objects)} object(s) · overlaps {report.get('overlaps_before', '?')} → {report.get('overlaps_after', '?')} · "
        f"{'resolved' if report.get('resolved') else 'unresolved'} · drag to rotate, wheel to zoom"
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta http-equiv='Content-Security-Policy' content=\"{CSP}\">"
        "<style>body{margin:0;font-family:system-ui,sans-serif;background:#fff;color:#1b2330}canvas{display:block;width:100%;height:auto;cursor:grab;border:1px solid #d5dce6;border-radius:8px}.cap{font-size:12px;color:#5b6778;margin:6px 4px}</style>"
        "</head><body>"
        f"<canvas id='scene' width='{int(width)}' height='{int(height)}'></canvas>"
        f"<div class='cap'>{caption}</div>"
        f"<script id='scene-data' type='application/json'>{payload}</script>"
        f"<script>{_SCRIPT}</script>"
        "</body></html>"
    )

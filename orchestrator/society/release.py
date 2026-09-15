"""The release loop: manuscripts, board feedback, final edits, release waves, publish, and go-to-market.

A work is a catalog row whose manuscript is assembled from every finished item for it (story bible,
chapters, the final edit last). The studio releases in waves: six wave-1 works reach ``final``, the
wave goes to the board as one release, and the board approves it or returns it with feedback that
reopens the final edits. Publishing locks each manuscript and opens marketing and sales.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .. import vault
from . import store

GO_TO_MARKET_ITEMS = (
    ("marketing", "Positioning and blurb", "Positioning statement (who it is for, what it promises) and a 120-word back-cover blurb for the published work, drawing on the board's feedback themes."),
    ("marketing", "Landing page copy", "Headline, subhead, three reasons to read, an excerpt teaser, and a call to action; deliver as HTML the preview can render."),
    ("marketing", "Launch plan", "A four-week launch plan: channels, dates, assets needed, and how feedback is captured."),
    ("sales", "Outreach drafts", "Three short outreach messages for reviewers, partners, and readers, each with a single ask."),
    ("sales", "Pricing and channel notes", "Recommended price points per channel and the reasoning, with the free path if any."),
)
FINAL_EDIT_PREFIX = "Final edit with board feedback"
WAVE_SIZE = 6
MANUSCRIPT_EXCERPT_CHARS = 6_000


def _slug(title: str) -> str:
    from ..missions import deliverable_slug

    return deliverable_slug(title)


def manuscript_items(scope: str, catalog_id: int) -> List[Dict[str, Any]]:
    """Finished items with a deliverable for one work, in production order; the final edit always comes last."""
    items = store.rows("work_items", scope, "catalog_id = ? AND artifact_id IS NOT NULL AND status IN ('done', 'board')", (int(catalog_id),), order="id ASC", limit=200)
    finals = [i for i in items if str(i["title"]).startswith(FINAL_EDIT_PREFIX)]
    others = [i for i in items if not str(i["title"]).startswith(FINAL_EDIT_PREFIX)]
    return others + finals


def assemble_manuscript(scope: str, catalog_id: int) -> Optional[int]:
    """Concatenate every finished deliverable of a work into one locked manuscript artifact; None when nothing is finished."""
    cat = store.row("catalog", catalog_id, scope)
    if not cat:
        return None
    items = manuscript_items(scope, catalog_id)
    if not items:
        return None
    parts = [f"# {cat['title']}\n"]
    if cat.get("logline"):
        parts.append(f"_{cat['logline']}_\n")
    for item in items:
        body = vault.export_artifact(int(item["artifact_id"]), scope)[1]
        parts.append(f"\n## {item['title']}\n\n{body.strip()}\n")
    company = store.row("companies", int(cat["company_id"]), scope) or {"key": "company"}
    slug = _slug(cat["title"])
    artifact_id, _ = vault.save_artifact(scope, f"{slug}.md", f"company/{company['key']}/works/{slug}.md", "\n".join(parts), "markdown")
    store.update("catalog", int(catalog_id), artifact_id=int(artifact_id))
    return int(artifact_id)


def request_final_edit(scope: str, company_id: int, catalog_id: int, feedback_text: str) -> Optional[int]:
    """After board feedback, one high-priority item carries the feedback and the manuscript into the final edit."""
    cat = store.row("catalog", catalog_id, scope)
    if not cat:
        return None
    existing = store.rows("work_items", scope, "catalog_id = ? AND title LIKE 'Final edit with board feedback%' AND status NOT IN ('done', 'rejected')", (int(catalog_id),), limit=1)
    if existing:
        return int(existing[0]["id"])
    manuscript_id = assemble_manuscript(scope, int(catalog_id)) or cat.get("artifact_id")
    excerpt = vault.export_artifact(int(manuscript_id), scope)[1][:MANUSCRIPT_EXCERPT_CHARS] if manuscript_id else "(no manuscript yet)"
    brief = (
        "Apply the board's feedback to the work and deliver the final text of the whole work.\n"
        f"BOARD FEEDBACK:\n{feedback_text.strip()[:3000]}\n\nTHE WORK AS IT STANDS (edit this, do not start over):\n{excerpt}"
    )
    return store.add_work_item(scope, company_id, f"{FINAL_EDIT_PREFIX} · {cat['title']}", brief, importance=5, catalog_id=int(catalog_id))


def publish_work(scope: str, catalog_id: int) -> Optional[int]:
    """Lock the manuscript (the approved final edit last) as the published artifact; opens marketing and sales the first time."""
    cat = store.row("catalog", catalog_id, scope)
    if not cat:
        return None
    manuscript_id = assemble_manuscript(scope, int(catalog_id))
    body = vault.export_artifact(int(manuscript_id), scope)[1] if manuscript_id else f"# {cat['title']}\n\n(no deliverable was produced yet)\n"
    slug = _slug(cat["title"])
    company = store.row("companies", int(cat["company_id"]), scope) or {"key": "company"}
    artifact_id, _ = vault.save_artifact(scope, f"published-{slug}.md", f"company/{company['key']}/published/{slug}.md", body, "markdown")
    store.update("catalog", int(catalog_id), stage="published", artifact_id=int(artifact_id))
    activate_go_to_market(scope, int(cat["company_id"]))
    for department, title, brief in GO_TO_MARKET_ITEMS:
        store.add_work_item(scope, int(cat["company_id"]), f"{title} · {cat['title']}", f"Published work: {cat['title']}. {cat.get('logline', '')}\n{brief}", importance=4, catalog_id=int(catalog_id))
    return int(artifact_id)


def activate_go_to_market(scope: str, company_id: int) -> List[str]:
    """Open the marketing and sales departments (seats become fillable by graduates)."""
    opened: List[str] = []
    for dept in store.departments_for(scope, company_id):
        if dept["key"] in ("marketing", "sales") and not dept["active"]:
            store.update("departments", int(dept["id"]), active=1)
            opened.append(dept["key"])
    if opened:
        store.log_personnel(scope, "open_department", company_id=company_id, reason=", ".join(opened) + " opened after the first publication", approved_by="board")
        store.fill_open_seats(scope, company_id)
    return opened


def record_board_feedback(scope: str, company_id: int, catalog_id: Optional[int], text: str) -> int:
    return store.insert("feedback", scope, company_id=int(company_id), catalog_id=catalog_id, source="board", text=text.strip()[:4000], created_at=time.time())


# ---- release waves ---------------------------------------------------------------------------

def wave_works(scope: str, company_id: int, wave: int = 1) -> List[Dict[str, Any]]:
    return store.rows("catalog", scope, "company_id = ? AND release_wave = ?", (int(company_id), int(wave)), order="id ASC", limit=50)


def wave_status(scope: str, company_id: int, wave: int = 1, size: int = WAVE_SIZE) -> Dict[str, Any]:
    """Where the wave stands: how many works are final or published, and the release row if one exists."""
    works = wave_works(scope, company_id, wave)
    ready = [w for w in works if w["stage"] in ("final", "published", "marketed")]
    release = store.rows("releases", scope, "company_id = ? AND wave = ? AND status != 'returned'", (int(company_id), int(wave)), order="id DESC", limit=1)
    return {"wave": wave, "works": len(works), "ready": len(ready), "size": size, "gate_met": len(works) >= size and len(ready) >= size, "release": release[0] if release else None}


def assemble_wave(scope: str, company_id: int, wave: int = 1, size: int = WAVE_SIZE) -> Optional[int]:
    """Send a whole wave to the board when every one of its works is final: one release row in ``board_review``."""
    status = wave_status(scope, company_id, wave, size)
    if not status["gate_met"]:
        return None
    if status["release"] and status["release"]["status"] == "board_review":
        return int(status["release"]["id"])
    for work in wave_works(scope, company_id, wave)[:size]:
        assemble_manuscript(scope, int(work["id"]))
    works = [int(w["id"]) for w in wave_works(scope, company_id, wave)[:size]]
    return store.insert("releases", scope, company_id=int(company_id), wave=int(wave), status="board_review", works=works, created_at=time.time())


def approve_release(scope: str, release_id: int, notes: str = "") -> List[int]:
    """The board approves the wave: every work is published (manuscript locked, go-to-market items queued)."""
    release = store.row("releases", release_id, scope)
    if not release or release["status"] != "board_review":
        return []
    published: List[int] = []
    for catalog_id in store.load_json(release.get("works"), []):
        cat = store.row("catalog", int(catalog_id), scope)
        if cat and cat["stage"] not in ("published", "marketed"):
            artifact = publish_work(scope, int(catalog_id))
            if artifact:
                published.append(int(artifact))
    if notes.strip():
        record_board_feedback(scope, int(release["company_id"]), None, notes)
    store.update("releases", int(release_id), status="released", notes=notes.strip()[:2000], reviewed_at=time.time(), released_at=time.time())
    return published


def return_release(scope: str, release_id: int, feedback_text: str) -> int:
    """The board returns the wave: feedback is recorded and every work reopens for a final edit."""
    release = store.row("releases", release_id, scope)
    if not release or release["status"] != "board_review":
        return 0
    reopened = 0
    company_id = int(release["company_id"])
    for catalog_id in store.load_json(release.get("works"), []):
        cat = store.row("catalog", int(catalog_id), scope)
        if not cat or cat["stage"] in ("published", "marketed"):
            continue
        record_board_feedback(scope, company_id, int(catalog_id), feedback_text)
        store.update("catalog", int(catalog_id), stage="board_feedback")
        request_final_edit(scope, company_id, int(catalog_id), feedback_text)
        reopened += 1
    store.update("releases", int(release_id), status="returned", notes=feedback_text.strip()[:2000], reviewed_at=time.time())
    return reopened

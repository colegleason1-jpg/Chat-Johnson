"""The release loop: board feedback → final edit → publish → marketing and sales open."""
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


def request_final_edit(scope: str, company_id: int, catalog_id: int, feedback_text: str) -> Optional[int]:
    """After board feedback, one high-priority item carries the feedback into the final edit."""
    cat = store.row("catalog", catalog_id)
    if not cat:
        return None
    existing = store.rows("work_items", scope, "catalog_id = ? AND title LIKE 'Final edit with board feedback%' AND status NOT IN ('done', 'rejected')", (int(catalog_id),), limit=1)
    if existing:
        return int(existing[0]["id"])
    return store.add_work_item(scope, company_id, f"Final edit with board feedback · {cat['title']}", f"Apply the board's feedback to the work and deliver the final text.\nBOARD FEEDBACK:\n{feedback_text.strip()[:3000]}", importance=5, catalog_id=int(catalog_id))


def publish_work(scope: str, catalog_id: int) -> Optional[int]:
    """Lock the latest deliverable as the published artifact; opens marketing and sales the first time."""
    cat = store.row("catalog", catalog_id)
    if not cat:
        return None
    items = store.rows("work_items", scope, "catalog_id = ? AND artifact_id IS NOT NULL", (int(catalog_id),), order="updated_at DESC", limit=1)
    body = vault.export_artifact(int(items[0]["artifact_id"]))[1] if items else f"# {cat['title']}\n\n(no deliverable was produced yet)\n"
    from ..missions import deliverable_slug

    slug = deliverable_slug(cat["title"])
    company = store.row("companies", int(cat["company_id"])) or {"key": "company"}
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


def feedback_summary(scope: str, company_id: int, limit: int = 5) -> List[Dict[str, Any]]:
    return store.rows("feedback", scope, "company_id = ?", (int(company_id),), order="id DESC", limit=limit)


def record_board_feedback(scope: str, company_id: int, catalog_id: Optional[int], text: str) -> int:
    return store.insert("feedback", scope, company_id=int(company_id), catalog_id=catalog_id, source="board", text=text.strip()[:4000], created_at=time.time())

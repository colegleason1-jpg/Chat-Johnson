"""Org templates on Traction/EOS: AVS Studio, the software company, and founding agents.

Every seat has 3–5 roles and KPIs; departments group seats into teams; the catalog seeds the IP
projects (or products) and the first work items. Founding agents (Philosopher tier) fill the active
seats so a company can run before the academy has graduated anyone.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

from . import store

DAY = 86_400.0
DEFAULT_PRODUCTS: Tuple[Tuple[str, str, str], ...] = (
    ("supply_chain_optimizer", "Supply Chain Optimizer", "Optimises supply chain routing and stock decisions (name to confirm)."),
    ("chatbot_book_system", "Chat Bot Book System", "A chat bot that reads, indexes, and converses about books."),
    ("api", "The API", "The operator's API product: programmatic access to the studio's capabilities."),
)


@dataclass(frozen=True)
class SeatSpec:
    key: str
    title: str
    department: str
    reports_to: str
    roles: Tuple[str, ...]
    kpis: Dict[str, float]
    importance: int = 3


EXEC_SEATS: Tuple[SeatSpec, ...] = (
    SeatSpec("ea_board", "Executive Assistant to the Board", "executive", "",
             ("filter the board's ideas into backlog items", "log directives", "report each cycle to the board", "route the board's questions to the CEO"),
             {"reports": 1}, 5),
    SeatSpec("ceo", "CEO", "executive", "ea_board",
             ("set priorities against the vision", "rate the backlog by importance", "approve hires and fires", "own the quarterly rocks", "report to the board through the EA"),
             {"reports": 1}, 5),
    SeatSpec("ea_ceo", "Executive Assistant to the CEO", "executive", "ceo",
             ("delegate rated items to seats", "run the Level 10 meeting", "direct personnel: positions, hiring, firing", "keep the company timeline"),
             {"reports": 1}, 5),
    SeatSpec("analytics_lead", "Analytics Lead", "executive", "ea_ceo",
             ("break large items into sub-items", "contextualise work with research", "estimate effort", "return decisions to the EA"),
             {"deliverables": 1}, 4),
)

AVS_DEPARTMENTS: Tuple[Tuple[str, str, bool], ...] = (
    ("executive", "Executive", True), ("editorial", "Editorial", True), ("research", "Research", True),
    ("production", "Production", True), ("art", "Art", True), ("coordination", "Coordination", True),
    ("marketing", "Marketing", False), ("sales", "Sales", False),
)
AVS_SEATS: Tuple[SeatSpec, ...] = EXEC_SEATS + (
    SeatSpec("managing_editor", "Managing Editor", "editorial", "ea_ceo",
             ("review every deliverable against its brief", "hold style and continuity", "pass or return work with notes"), {"reviews": 2}, 4),
    SeatSpec("editor_1", "Editor", "editorial", "managing_editor", ("line edit", "continuity notes", "prepare works for preliminary review"), {"deliverables": 1, "review_pass_rate": 0.6}),
    SeatSpec("editor_2", "Editor", "editorial", "managing_editor", ("line edit", "continuity notes", "prepare works for preliminary review"), {"deliverables": 1, "review_pass_rate": 0.6}),
    SeatSpec("research_lead", "Research Lead", "research", "ea_ceo", ("plan research per project", "verify facts and settings", "brief writers"), {"deliverables": 1}, 4),
    SeatSpec("researcher_1", "Researcher", "research", "research_lead", ("gather sources", "summarise findings", "answer writers' questions"), {"deliverables": 1, "review_pass_rate": 0.6}),
    SeatSpec("production_lead", "Head of Production", "production", "ea_ceo", ("schedule drafts", "balance writer load", "keep projects on the timeline"), {"reviews": 1}, 4),
    SeatSpec("lead_writer_1", "Lead Writer", "production", "production_lead", ("outline and draft assigned works", "keep voice consistent", "revise on editor notes"), {"deliverables": 1, "review_pass_rate": 0.6}, 4),
    SeatSpec("lead_writer_2", "Lead Writer", "production", "production_lead", ("outline and draft assigned works", "keep voice consistent", "revise on editor notes"), {"deliverables": 1, "review_pass_rate": 0.6}, 4),
    SeatSpec("lead_writer_3", "Lead Writer", "production", "production_lead", ("outline and draft assigned works", "keep voice consistent", "revise on editor notes"), {"deliverables": 1, "review_pass_rate": 0.6}, 4),
    SeatSpec("writer_1", "Writer", "production", "lead_writer_1", ("draft chapters and scenes", "revise on notes", "keep the story bible current"), {"deliverables": 1, "review_pass_rate": 0.6}),
    SeatSpec("writer_2", "Writer", "production", "lead_writer_2", ("draft chapters and scenes", "revise on notes", "keep the story bible current"), {"deliverables": 1, "review_pass_rate": 0.6}),
    SeatSpec("art_director", "Art Director", "art", "ea_ceo", ("define the visual identity per project", "brief illustration and design", "approve briefs"), {"reviews": 1}, 3),
    SeatSpec("illustration_brief", "Illustration Brief Writer", "art", "art_director", ("write illustration briefs", "reference sheets", "cover concepts"), {"deliverables": 1, "review_pass_rate": 0.6}),
    SeatSpec("designer", "Designer", "art", "art_director", ("layout and cover design briefs", "typography notes", "format for release"), {"deliverables": 1, "review_pass_rate": 0.6}),
    SeatSpec("production_coordinator", "Production Coordinator", "coordination", "ea_ceo", ("hand work between departments", "flag timeline drift", "keep the vision in every brief"), {"reports": 1}, 3),
    SeatSpec("marketing_lead", "Marketing Lead", "marketing", "ea_ceo", ("positioning per work", "launch plans", "turn board feedback into messaging"), {"deliverables": 1}, 3),
    SeatSpec("copywriter", "Copywriter", "marketing", "marketing_lead", ("blurbs", "landing copy", "campaign drafts"), {"deliverables": 1, "review_pass_rate": 0.6}),
    SeatSpec("sales_lead", "Sales Lead", "sales", "ea_ceo", ("channels and pricing", "outreach plans", "track released works"), {"deliverables": 1}, 3),
    SeatSpec("outreach_writer", "Outreach Writer", "sales", "sales_lead", ("outreach drafts", "partner briefs", "follow-ups"), {"deliverables": 1, "review_pass_rate": 0.6}),
)

SOFTWARE_DEPARTMENTS: Tuple[Tuple[str, str, bool], ...] = (
    ("executive", "Executive", True), ("product", "Product", True), ("docs", "Documentation", True), ("support", "Support", True),
    ("marketing", "Marketing", True), ("sales", "Sales", True), ("release", "Release", True), ("qa", "Quality", True),
)


def software_seats(products: Sequence[Tuple[str, str, str]]) -> Tuple[SeatSpec, ...]:
    product_seats = tuple(
        SeatSpec(f"pm_{key}", f"Product Manager · {title}", "product", "ea_ceo",
                 ("own the product's positioning", "keep its backlog", "coordinate docs, support, and release"), {"deliverables": 1}, 4)
        for key, title, _ in products
    )
    return EXEC_SEATS + product_seats + (
        SeatSpec("technical_writer", "Technical Writer", "docs", "ea_ceo", ("docs outlines", "reference pages", "quickstarts"), {"deliverables": 1, "review_pass_rate": 0.6}),
        SeatSpec("faq_writer", "Support and FAQ Writer", "support", "ea_ceo", ("FAQ", "troubleshooting guides", "support macros"), {"deliverables": 1, "review_pass_rate": 0.6}),
        SeatSpec("marketing_lead", "Marketing Lead", "marketing", "ea_ceo", ("positioning", "launch plans", "messaging from feedback"), {"deliverables": 1}, 4),
        SeatSpec("copywriter", "Copywriter", "marketing", "marketing_lead", ("landing copy", "blurbs", "campaign drafts"), {"deliverables": 1, "review_pass_rate": 0.6}),
        SeatSpec("landing_writer", "Landing Page Writer", "marketing", "marketing_lead", ("landing page structure", "calls to action", "preview-ready HTML"), {"deliverables": 1, "review_pass_rate": 0.6}),
        SeatSpec("sales_lead", "Sales Lead", "sales", "ea_ceo", ("channels", "pricing notes", "pipeline"), {"deliverables": 1}, 4),
        SeatSpec("outreach_writer", "Outreach Writer", "sales", "sales_lead", ("outreach sequences", "partner briefs", "follow-ups"), {"deliverables": 1, "review_pass_rate": 0.6}),
        SeatSpec("pricing_analyst", "Pricing Analyst", "sales", "sales_lead", ("pricing pages", "tier comparisons", "cost notes"), {"deliverables": 1, "review_pass_rate": 0.6}),
        SeatSpec("release_manager", "Release Manager", "release", "ea_ceo", ("release notes", "checklists", "versioning"), {"deliverables": 1}),
        SeatSpec("qa_reviewer", "QA Reviewer", "qa", "ea_ceo", ("review every deliverable against its brief", "check claims against the product", "pass or return with notes"), {"reviews": 2}, 4),
    )


PRODUCT_BACKLOG: Tuple[Tuple[str, str], ...] = (
    ("Positioning one-pager", "Who it is for, the problem, the promise, three proof points, and one line on price. Plain language."),
    ("Landing page copy", "Headline, subhead, three benefit blocks, a call to action, and an FAQ of five questions; deliver as HTML the preview can render."),
    ("Docs outline", "A documentation outline: quickstart, concepts, how-to guides, reference; one line per page on what it covers."),
    ("Pricing page", "Two or three tiers with what each includes, the free path, and the upgrade trigger."),
    ("Demo script", "A five-minute demo script: setup, three moments that show value, close."),
    ("FAQ", "Ten questions a careful buyer asks, each answered in under 60 words."),
    ("Outreach sequence", "Three short outreach messages for one clear audience, each with a single ask."),
)

AVS_ROCKS = ("Finish outlines and story bibles for the six wave-1 works", "Draft wave-1 works to editorial pass", "Preliminary review of wave 1 by the board")
SOFTWARE_ROCKS = ("Positioning for every product", "Docs outline and FAQ per product", "Landing pages drafted and previewed")
AVS_TIMELINE = (("Outlines and story bibles done", 14), ("Wave-1 drafts complete", 60), ("Editorial pass complete", 90), ("Board preliminary review", 100), ("Wave-1 publish", 120))
SOFTWARE_TIMELINE = (("Positioning done", 7), ("Docs outlines done", 21), ("Landing pages previewed", 30), ("Pricing set", 45))


def _persona(title: str, roles: Sequence[str], company: str) -> str:
    return f"Founding {title} at {company}. Roles: " + "; ".join(roles) + ". Works precisely, states uncertainty, never pads."


def _seed_org(scope: str, company_id: int, company_name: str, departments, seats: Sequence[SeatSpec], founders: bool) -> Dict[str, int]:
    dept_ids: Dict[str, int] = {}
    dept_active: Dict[str, bool] = {}
    for key, name, active in departments:
        dept_ids[key] = store.insert("departments", scope, company_id=company_id, key=key, name=name, active=int(active))
        dept_active[key] = active
    seat_ids: Dict[str, int] = {}
    for spec in seats:
        seat_ids[spec.key] = store.insert(
            "seats", scope, company_id=company_id, department_id=dept_ids[spec.department], key=spec.key, title=spec.title,
            roles=list(spec.roles), kpis=dict(spec.kpis), importance=spec.importance, status="open",
        )
    for spec in seats:
        if spec.reports_to:
            store.update("seats", seat_ids[spec.key], reports_to=seat_ids[spec.reports_to])
    # One team per department (its lead is the department's first seat); production gets one team per lead writer.
    team_ids: Dict[str, int] = {}
    for key, name, _ in departments:
        first = next((spec for spec in seats if spec.department == key), None)
        team_ids[key] = store.insert("teams", scope, company_id=company_id, department_id=dept_ids[key], name=f"{name} Team", lead_seat_id=seat_ids[first.key] if first else None)
        store.update("departments", dept_ids[key], head_seat_id=seat_ids[first.key] if first else None)
    for spec in seats:
        store.update("seats", seat_ids[spec.key], team_id=team_ids[spec.department])
    for number, spec in enumerate([s for s in seats if s.key.startswith("lead_writer_")], start=1):
        team = store.insert("teams", scope, company_id=company_id, department_id=dept_ids[spec.department], name=f"Production Team {number}", lead_seat_id=seat_ids[spec.key])
        store.update("seats", seat_ids[spec.key], team_id=team)
        for sub in seats:
            if sub.reports_to == spec.key:
                store.update("seats", seat_ids[sub.key], team_id=team)
    if founders:
        for spec in seats:
            if not dept_active[spec.department]:
                continue
            agent_id = store.add_agent(scope, f"{spec.title} (founding)", _persona(spec.title, spec.roles, company_name), tier="philosopher", allowance=2_000)
            store.seat_agent(scope, seat_ids[spec.key], agent_id)
    return seat_ids


def seed_company(scope: str, key: str, products: Optional[Sequence[Tuple[str, str, str]]] = None, founders: bool = True) -> int:
    """Create a company from its template (idempotent per scope and key); returns the company id."""
    existing = store.company_by_key(scope, key)
    if existing:
        return int(existing["id"])
    now = time.time()
    if key == "avs_studio":
        company_id = store.insert(
            "companies", scope, key=key, name="AVS Studio", kind="studio",
            vision="A studio that turns a proprietary catalog of literature and entertainment IP into released works, six at a time, with the board's voice in every decision.",
            core_values=["craft over volume", "the vision in every brief", "honest reporting", "reviewable work"],
            core_focus="Original literature and entertainment IP, written, edited, and released by the studio.",
            ten_year="A living catalog of released works across literature and entertainment with an audience that follows the studio.",
            three_year="Every wave of the 17-project backlog released and marketed; departments for marketing and sales established.",
            one_year="The six wave-1 works published after board review; the rest of the backlog outlined.",
            interval_s=6 * 3600, daily_share=0.35, created_at=now,
        )
        seat_ids = _seed_org(scope, company_id, "AVS Studio", AVS_DEPARTMENTS, AVS_SEATS, founders)
        for number in range(1, 18):
            wave = 1 if number <= 6 else 2
            field = "literature" if number <= 6 or number % 2 else "entertainment"
            catalog_id = store.insert(
                "catalog", scope, company_id=company_id, key=f"ip-{number:02d}", title=f"IP Project {number:02d} (title to be set by the board)",
                field=field, logline="", stage="development" if wave == 1 else "backlog", release_wave=wave,
            )
            if wave == 1:
                store.add_work_item(scope, company_id, f"Story bible and outline · IP Project {number:02d}",
                                    "Write the story bible (premise, world, principal characters, tone) and a chapter-by-chapter outline for this work. Mark open questions for the board.",
                                    importance=5, catalog_id=catalog_id)
                store.add_work_item(scope, company_id, f"Draft chapter one · IP Project {number:02d}",
                                    "Draft chapter one from the outline in the studio's voice; end with a note on what the next chapter needs.",
                                    importance=4, catalog_id=catalog_id)
        store.add_work_item(scope, company_id, "Vision/Traction Organizer draft", "Draft the V/TO: core values, core focus, 10-year target, 3-year picture, 1-year plan, quarterly rocks, issues list. Use the company vision as the source.", importance=5)
        store.add_work_item(scope, company_id, "Release plan for wave 1", "Plan the preliminary review, feedback capture, final edit, and publish steps for the six wave-1 works with dates against the timeline.", importance=4)
        for title in AVS_ROCKS:
            store.insert("rocks", scope, company_id=company_id, quarter=time.strftime("%Y-Q") + str((time.gmtime().tm_mon - 1) // 3 + 1), title=title, owner_seat_id=seat_ids["ceo"], due_at=now + 90 * DAY)
        for milestone, days in AVS_TIMELINE:
            store.insert("timeline", scope, company_id=company_id, milestone=milestone, due_at=now + days * DAY)
        return company_id
    if key == "software_co":
        chosen = tuple(products or DEFAULT_PRODUCTS)
        company_id = store.insert(
            "companies", scope, key=key, name="AVS Software", kind="software",
            vision="Market and sell the operator's software with honest positioning, clear docs, and support that answers before it is asked.",
            core_values=["clarity", "no false claims", "answer the buyer's real question", "ship notes with every release"],
            core_focus="Selling the operator's software products.",
            ten_year="A product line customers recommend.", three_year="Every product documented, priced, and sold through repeatable channels.",
            one_year="Positioning, docs, pricing, and landing pages live for every product; first outreach running.",
            interval_s=6 * 3600, daily_share=0.25, created_at=now,
        )
        seat_ids = _seed_org(scope, company_id, "AVS Software", SOFTWARE_DEPARTMENTS, software_seats(chosen), founders)
        for pkey, title, logline in chosen:
            catalog_id = store.insert("catalog", scope, company_id=company_id, key=pkey, title=title, field="software", logline=logline, stage="development", release_wave=1)
            for item_title, brief in PRODUCT_BACKLOG:
                store.add_work_item(scope, company_id, f"{item_title} · {title}", f"Product: {title}. {logline}\n{brief}", importance=4 if item_title in ("Positioning one-pager", "Landing page copy") else 3, catalog_id=catalog_id)
        for title in SOFTWARE_ROCKS:
            store.insert("rocks", scope, company_id=company_id, quarter=time.strftime("%Y-Q") + str((time.gmtime().tm_mon - 1) // 3 + 1), title=title, owner_seat_id=seat_ids["ceo"], due_at=now + 90 * DAY)
        for milestone, days in SOFTWARE_TIMELINE:
            store.insert("timeline", scope, company_id=company_id, milestone=milestone, due_at=now + days * DAY)
        return company_id
    raise KeyError(f"unknown company template {key!r}")


def add_product(scope: str, company_id: int, key: str, title: str, logline: str) -> int:
    """Add a product to the software company: a catalog row, a product manager seat, and its backlog."""
    catalog_id = store.insert("catalog", scope, company_id=company_id, key=key, title=title, field="software", logline=logline, stage="development", release_wave=1)
    ea = store.seat_by_key(scope, company_id, "ea_ceo")
    product_dept = next((d for d in store.departments_for(scope, company_id) if d["key"] == "product"), None)
    seat_id = store.insert(
        "seats", scope, company_id=company_id, department_id=product_dept["id"] if product_dept else None, key=f"pm_{key}",
        title=f"Product Manager · {title}", roles=["own the product's positioning", "keep its backlog", "coordinate docs, support, and release"],
        kpis={"deliverables": 1}, importance=4, status="open", reports_to=ea["id"] if ea else None,
    )
    company = store.row("companies", company_id) or {"name": "AVS Software"}
    agent_id = store.add_agent(scope, f"Product Manager · {title} (founding)", _persona(f"Product Manager · {title}", ("own the product", "keep its backlog", "coordinate"), str(company["name"])), tier="philosopher", allowance=2_000)
    store.seat_agent(scope, seat_id, agent_id)
    for item_title, brief in PRODUCT_BACKLOG:
        store.add_work_item(scope, company_id, f"{item_title} · {title}", f"Product: {title}. {logline}\n{brief}", importance=4 if item_title in ("Positioning one-pager", "Landing page copy") else 3, catalog_id=catalog_id)
    return catalog_id


def match_seat(item_title: str, item_brief: str, seats: Sequence[Dict], exclude_keys: Sequence[str] = ("ea_board", "ceo", "ea_ceo")) -> Optional[Dict]:
    """Deterministic fallback for delegation: the filled seat whose roles share the most words with the item."""
    from ..keyword_search import keywords

    words = set(keywords(item_title + " " + item_brief))
    best, best_score = None, -1
    for seat in seats:
        if seat["key"] in exclude_keys or not seat.get("agent_id"):
            continue
        roles = " ".join(store.load_json(seat.get("roles"), [])) + " " + seat["title"]
        score = len(words & set(keywords(roles)))
        if score > best_score:
            best, best_score = seat, score
    return best


ALLOWANCES = {"producer": 500, "auxiliary": 1_500, "philosopher": 2_000}
FOCUSES = ("research", "drafting", "editing", "data", "design", "marketing", "support", "planning", "analysis", "dialogue")
TIER_MIX = (("producer", 0.60), ("auxiliary", 0.25), ("philosopher", 0.15))


def seed_academy(scope: str, total: int = 100) -> int:
    """Top the society up to ``total`` agents (seated founders count); returns how many were created.

    Deterministic names and personas, no model calls: Producers on a basic allowance, Auxiliaries
    (guardians and teachers) on more, Philosophers ready to graduate into open seats.
    """
    existing = store.agents_for(scope)
    missing = max(0, int(total) - len(existing))
    if missing == 0:
        return 0
    counts = {tier: int(round(missing * share)) for tier, share in TIER_MIX}
    counts["producer"] += missing - sum(counts.values())
    created = 0
    start = len(existing) + 1
    for tier, _ in TIER_MIX:
        for _ in range(counts[tier]):
            number = start + created
            focus = FOCUSES[number % len(FOCUSES)]
            persona = {
                "producer": f"Producer {number:03d}, focus {focus}: does foundational work exactly as briefed, states what it could not do.",
                "auxiliary": f"Auxiliary {number:03d}, focus {focus}: a guardian and teacher; grades producers strictly against the brief and flags unsafe or empty work.",
                "philosopher": f"Philosopher {number:03d}, focus {focus}: plans before writing, uses only the facts given, and synthesises inputs into one result.",
            }[tier]
            store.add_agent(scope, f"{tier.title()} {number:03d}", persona, tier=tier, allowance=ALLOWANCES[tier], focus=focus)
            created += 1
    return created

"""The synthetic token economy is an allocation of real tokens: earn by production, spend in leisure.

The treasury is what the keys allow today (daily caps minus use, read from the persisted quota
counters); the companies' shares come from their own ``daily_share`` settings and the academy and
leisure split what is left. Production credits equal the tokens the router recorded; allowances
are paid only to free academy agents and are funded from the academy's own cycle budget, so no
credit is minted from nothing. A registered local model is a separate, uncapped pool that only
local-first calls draw on; it never inflates the treasury.
"""
from __future__ import annotations

from typing import Dict, Mapping, Optional, Sequence

from .. import discovery
from ..config import PROVIDERS, daily_cap, provider_api_key
from ..quota import QuotaLedger
from ..router import CORTEX_ENDPOINTS, LOCAL_ENDPOINT_NAME, _endpoint_key, _estimate_tokens, local_endpoint
from . import store

DEFAULT_SHARES: Dict[str, float] = {"company": 0.35, "company_2": 0.25, "academy": 0.25, "leisure": 0.15}
UNCAPPED_VENDOR_ASSUMPTION = 300_000  # tokens per day assumed for a keyed vendor with no cap anywhere
ACADEMY_REMAINDER = 0.625  # of what the companies leave: academy 0.25 / leisure 0.15 at the default company shares
COMPANY_SHARE_KEYS = {"avs_studio": "company"}


def keyed_vendors() -> Sequence[str]:
    """Cloud vendors with a key in this context; the local endpoint is not one of them (see ``local_available``)."""
    vendors = []
    for endpoint in CORTEX_ENDPOINTS.values():
        if endpoint.name == LOCAL_ENDPOINT_NAME:
            continue
        if _endpoint_key(endpoint):
            vendors.append(discovery.vendor_for(endpoint.name))
    for name, cfg in PROVIDERS.items():
        if provider_api_key(cfg):
            vendors.append(discovery.vendor_for(name))
    return tuple(dict.fromkeys(vendors))


def local_available() -> bool:
    return local_endpoint() is not None


def share_key_for(company: Mapping[str, object]) -> str:
    return COMPANY_SHARE_KEYS.get(str(company.get("key", "")), "company_2")


def shares_for(scope: str) -> Dict[str, float]:
    """Every share from the companies' own settings: the board's sliders decide, the academy and leisure split the rest."""
    shares: Dict[str, float] = {}
    for company in store.companies_for(scope):
        shares[share_key_for(company)] = max(0.0, min(0.9, float(company.get("daily_share") or 0.0)))
    used = sum(shares.values())
    remainder = max(0.0, 1.0 - used)
    shares["academy"] = round(remainder * ACADEMY_REMAINDER, 4)
    shares["leisure"] = round(remainder * (1.0 - ACADEMY_REMAINDER), 4)
    return dict(DEFAULT_SHARES, **shares) if not shares else {**{k: 0.0 for k in DEFAULT_SHARES}, **shares}


class Treasury:
    def __init__(self, ledger: QuotaLedger, shares: Optional[Mapping[str, float]] = None) -> None:
        self.ledger = ledger
        self.shares = dict(DEFAULT_SHARES, **(shares or {}))

    def daily_remaining(self) -> int:
        """Tokens the keyed cloud vendors can still serve today: the configured cap minus what the persisted counters hold."""
        total = 0
        for vendor in keyed_vendors():
            cap = daily_cap(vendor)
            used = 0
            if self.ledger.known(vendor):
                use = self.ledger.usage(vendor)
                cap = int(use.get("daily_limit") or 0) or cap
                used = int(use.get("daily_tokens", 0))
            total += max(0, (cap or UNCAPPED_VENDOR_ASSUMPTION) - used)
        return total

    def cycle_budget(self, share_key: str, hard_cap: int, share: Optional[float] = None) -> int:
        """Tokens one cycle may spend: its share of what is left today, never above the cycle's hard cap, no floor."""
        fraction = float(share if share is not None else self.shares.get(share_key, 0.0))
        return max(0, min(int(hard_cap), int(self.daily_remaining() * fraction)))


def charge(scope: str, agent_id: Optional[int], messages: Sequence[Mapping[str, str]], answer: str, decision, cycle_id: Optional[int] = None, job_id: Optional[int] = None) -> int:
    """Credit the working agent with the tokens this production call cost (the same estimate the router records)."""
    tokens = _estimate_tokens(messages, answer)
    if agent_id:
        store.ledger_add(scope, int(agent_id), "earn", tokens, activity=f"{decision.task_type} via {decision.provider}/{decision.model}", vendor=str(decision.provider), cycle_id=cycle_id, job_id=job_id)
    return tokens


def debit(scope: str, agent_id: int, tokens: int, activity: str, vendor: str = "", cycle_id: Optional[int] = None) -> int:
    return store.ledger_add(scope, int(agent_id), "spend", int(tokens), activity=activity, vendor=vendor, cycle_id=cycle_id)


def allowance_run(scope: str, cycle_id: Optional[int] = None, max_total: Optional[int] = None) -> int:
    """Pay free academy agents their allowance once, scaled down so the total never exceeds ``max_total``; returns the total granted.

    Seated agents earn by working and fired agents earn nothing; allowances come out of the academy's cycle budget.
    """
    recipients = [a for a in store.agents_for(scope, employment="free") if int(a.get("allowance") or 0) > 0]
    planned = sum(int(a["allowance"]) for a in recipients)
    if planned <= 0:
        return 0
    factor = min(1.0, float(max_total) / planned) if max_total is not None else 1.0
    total = 0
    for agent in recipients:
        amount = int(int(agent["allowance"]) * factor)
        if amount > 0:
            total += store.ledger_add(scope, int(agent["id"]), "allowance", amount, activity="allowance", cycle_id=cycle_id)
    return total

"""The synthetic token economy is an allocation of real tokens: earn by production, spend in leisure.

The treasury is what the keys allow today (daily caps minus use); shares split it between the
companies, the academy, and leisure; every credit equals the tokens the router actually recorded.
"""
from __future__ import annotations

from typing import Dict, Mapping, Optional, Sequence

from .. import discovery
from ..config import PROVIDERS, provider_api_key
from ..quota import QuotaLedger
from ..router import CORTEX_ENDPOINTS, _endpoint_key, _estimate_tokens
from . import store

DEFAULT_SHARES: Dict[str, float] = {"company": 0.35, "company_2": 0.25, "academy": 0.25, "leisure": 0.15}
UNCAPPED_VENDOR_ASSUMPTION = 300_000  # tokens per day assumed for a keyed vendor without a configured cap


def keyed_vendors() -> Sequence[str]:
    vendors = []
    for endpoint in CORTEX_ENDPOINTS.values():
        if _endpoint_key(endpoint):
            vendors.append(discovery.vendor_for(endpoint.name))
    for name, cfg in PROVIDERS.items():
        if provider_api_key(cfg):
            vendors.append(discovery.vendor_for(name))
    return tuple(dict.fromkeys(vendors))


class Treasury:
    def __init__(self, ledger: QuotaLedger, shares: Optional[Mapping[str, float]] = None) -> None:
        self.ledger = ledger
        self.shares = dict(DEFAULT_SHARES, **(shares or {}))

    def daily_remaining(self) -> int:
        """Tokens the keyed vendors can still serve today (caps minus use; an assumption for uncapped ones)."""
        total = 0
        for vendor in keyed_vendors():
            if not self.ledger.known(vendor):
                total += UNCAPPED_VENDOR_ASSUMPTION
                continue
            use = self.ledger.usage(vendor)
            cap = int(use.get("daily_limit") or 0) or UNCAPPED_VENDOR_ASSUMPTION
            total += max(0, cap - int(use.get("daily_tokens", 0)))
        return total

    def cycle_budget(self, share_key: str, hard_cap: int) -> int:
        """Tokens one cycle may spend: its share of what is left today, never above the cycle's hard cap."""
        share = float(self.shares.get(share_key, 0.0))
        return max(0, min(int(hard_cap), int(self.daily_remaining() * share)))


def charge(scope: str, agent_id: Optional[int], messages: Sequence[Mapping[str, str]], answer: str, decision, cycle_id: Optional[int] = None, job_id: Optional[int] = None) -> int:
    """Credit the working agent with the tokens this production call cost (the same estimate the router records)."""
    tokens = _estimate_tokens(messages, answer)
    if agent_id:
        store.ledger_add(scope, int(agent_id), "earn", tokens, activity=f"{decision.task_type} via {decision.provider}/{decision.model}", vendor=str(decision.provider), cycle_id=cycle_id, job_id=job_id)
    return tokens


def debit(scope: str, agent_id: int, tokens: int, activity: str, vendor: str = "", cycle_id: Optional[int] = None) -> int:
    return store.ledger_add(scope, int(agent_id), "spend", int(tokens), activity=activity, vendor=vendor, cycle_id=cycle_id)


def allowance_run(scope: str, cycle_id: Optional[int] = None) -> int:
    """Pay every agent its allowance once; returns the total granted."""
    total = 0
    for agent in store.agents_for(scope):
        if int(agent.get("allowance") or 0) > 0:
            total += store.ledger_add(scope, int(agent["id"]), "allowance", int(agent["allowance"]), activity="allowance", cycle_id=cycle_id)
    return total

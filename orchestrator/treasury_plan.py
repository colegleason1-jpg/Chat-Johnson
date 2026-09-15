"""Plan of the day: the daily token treasury allocated by the supply-chain resilience engine (``scrcae``).

The society used to split the day by fixed ratio and the tick deferred every cycle on a bursty
forecast. Neither answered "which activities, at what size, so the chat never starves". This module
asks that as a capital-allocation problem, the shape the engine was built for: each activity of
the day is a node with a cost in tokens at full scale, the goal points it delivers, a minimum
economic scale below which it is not worth running, and dependencies; the budget is the day's
remaining tokens across the keyed vendors; the chat reserve is a node pinned at full funding.

- The plan is "the most value today's tokens can buy" (free-tier tokens expire at midnight, so
  saving them buys nothing; the chat reserve is the one thing held back). The goal is a check
  solved in target mode: when it is infeasible the engine's diagnosis names the shortfall in
  tokens or the ceiling, so the tick drops the lowest-value activity, not all of them.
- The budget sweep's saturation point becomes the recommended daily share.
- The correlated Monte Carlo gives P50/P90 of the value delivered when vendors fail together:
  shock size from the route log's failure share, correlation from the dynamics coupling.
- Vendor stress: the engine's calibration fits a failure elasticity to load from hourly route-log
  bins and refuses thin evidence; the current load above anchor scales value down (macro).
- Each keyed vendor is a resource with its remaining daily tokens as capacity, and each activity
  draws on the vendors in the mix its workspace actually routed to (route log, 72 h; an even split
  with no history), so the plan respects per-vendor supply directly; Cortex 2 still enforces every
  limit call by call.
- Every plan carries the engine's content hashes and version; the latest plan is stored per scope.

The engine is optional (``requirements-supply.txt``, Python 3.12+). Without it ``plan_day`` returns
the fixed-share fallback and says so; nothing here touches keys, hard limits, or routing.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import vault

SETTING_LATEST = "treasury_plan:latest"
SETTING_CONFIG = "treasury_plan"
DEFAULT_GOAL_FRACTION = 0.75        # of the day's full-scale value the plan asks for in target mode
CHAT_RESERVE_FRACTION = 0.15        # of the day's remaining tokens held back for the chat
CHAT_RESERVE_MIN_TOKENS = 20_000
MISSION_TOKENS_PER_JOB = 8_000
DEFAULT_VALUE_POINTS = {"chat": 30.0, "company": 25.0, "academy": 18.0, "missions": 15.0, "leisure": 5.0}
DEFAULT_MIN_SCALE = {"chat": 1.0, "company": 0.3, "academy": 0.3, "missions": 0.2, "leisure": 0.2}
BASELINE_POINTS = 100.0
SIGMA_FLOOR, SIGMA_CEILING = 0.12, 0.60
STRESS_ANCHOR = 0.6                 # load fraction above which a vendor is "stressed"
STRESS_MIN_BINS = 24
STRESS_FLOOR = 0.5
MC_ITERATIONS = 10_000
MC_SEED = 20260915


def available() -> bool:
    """True when the engine imports (Python 3.12+ with requirements-supply.txt installed)."""
    try:
        import scrcae  # noqa: F401
    except Exception:
        return False
    return True


def engine_version() -> str:
    try:
        import scrcae

        return str(scrcae.__version__)
    except Exception:
        return ""


@dataclass
class Activity:
    key: str
    label: str
    cost_tokens: int
    value_points: float
    min_scale: float = 0.3
    max_scale: float = 1.0
    lead_days: float = 0.0
    prerequisite: str = ""
    company_id: Optional[int] = None
    mix: Dict[str, float] = field(default_factory=dict)   # vendor -> share of this activity's tokens (sums to 1 when set)

    def usage(self) -> Dict[str, float]:
        """Tokens drawn from each vendor at full scale."""
        return {vendor: float(self.cost_tokens) * share for vendor, share in self.mix.items() if share > 0.0}


@dataclass
class PlanLine:
    key: str
    label: str
    scale: float
    tokens: int
    value_points: float


@dataclass
class DayPlan:
    scope: str
    day: str
    budget_tokens: int
    goal_points: float
    status: str                              # optimal | short | ceiling | fallback | engine-missing | error
    lines: List[PlanLine] = field(default_factory=list)
    spend_tokens: int = 0
    value_points: float = 0.0
    full_value_points: float = 0.0
    shortfall_tokens: int = 0
    diagnosis: str = ""
    saturation_budget: int = 0
    recommended_share: float = 0.0
    stress: float = 1.0
    stress_rows: List[Dict[str, Any]] = field(default_factory=list)
    resources: List[Dict[str, Any]] = field(default_factory=list)   # per vendor: capacity, planned use, headroom
    tail: Dict[str, float] = field(default_factory=dict)
    engine: str = ""
    input_hash: str = ""
    output_hash: str = ""
    ms: int = 0
    notes: List[str] = field(default_factory=list)
    created_at: float = 0.0

    def scale_for(self, key: str, default: float = 1.0) -> float:
        for line in self.lines:
            if line.key == key:
                return float(line.scale)
        return default

    def summary(self) -> str:
        funded = ", ".join(f"{line.label} {line.scale:.0%}" for line in self.lines if line.scale > 0.0) or "nothing"
        head = f"{self.status}: {self.spend_tokens:,} of {self.budget_tokens:,} tokens for {self.value_points:.0f} of {self.full_value_points:.0f} points; funded {funded}"
        if self.shortfall_tokens:
            head += f"; short by {self.shortfall_tokens:,} tokens"
        return head

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


def config_for(scope: str) -> Dict[str, Any]:
    raw = vault.setting_get(scope, SETTING_CONFIG, "")
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        data = {}
    return {"goal_fraction": float(data.get("goal_fraction", DEFAULT_GOAL_FRACTION)), "values": {**DEFAULT_VALUE_POINTS, **dict(data.get("values") or {})},
            "min_scale": {**DEFAULT_MIN_SCALE, **dict(data.get("min_scale") or {})}, "chat_reserve_fraction": float(data.get("chat_reserve_fraction", CHAT_RESERVE_FRACTION))}


def save_config(scope: str, goal_fraction: float, chat_reserve_fraction: float, values: Optional[Mapping[str, float]] = None) -> Dict[str, Any]:
    current = config_for(scope)
    current["goal_fraction"] = max(0.05, min(1.0, float(goal_fraction)))
    current["chat_reserve_fraction"] = max(0.0, min(0.6, float(chat_reserve_fraction)))
    if values:
        current["values"] = {**current["values"], **{k: max(0.0, float(v)) for k, v in values.items()}}
    vault.setting_set(scope, SETTING_CONFIG, json.dumps(current, sort_keys=True))
    return current


def _hours_left(now: float) -> float:
    stamp = time.gmtime(now)
    return max(0.25, 24.0 - (stamp.tm_hour + stamp.tm_min / 60.0))


def vendor_capacities(ledger: Any) -> Dict[str, int]:
    """Tokens each keyed cloud vendor can still serve today (its daily cap minus the persisted counters)."""
    from .config import daily_cap
    from .society import economy

    capacities: Dict[str, int] = {}
    for vendor in economy.keyed_vendors():
        cap, used = daily_cap(vendor), 0
        if ledger is not None and ledger.known(vendor):
            use = ledger.usage(vendor)
            cap = int(use.get("daily_limit") or 0) or cap
            used = int(use.get("daily_tokens", 0))
        capacities[vendor] = max(0, (cap or economy.UNCAPPED_VENDOR_ASSUMPTION) - used)
    return capacities


def vendor_mix(scope: str, workspaces: Sequence[str], vendors: Sequence[str], hours: float = 72.0) -> Dict[str, float]:
    """Share of answered sends per vendor for these workspaces over the window; an even split over ``vendors`` with no history."""
    from . import discovery

    counts: Dict[str, int] = {}
    for row in vault.recent_routes(scope, limit=5000, hours=hours):
        if workspaces and str(row["workspace"] or "") not in workspaces:
            continue
        route = str(row["route"] or "")
        if not route or route == "failed":
            continue
        vendor = discovery.vendor_for(route.split("/", 1)[0])
        if vendor in vendors:
            counts[vendor] = counts.get(vendor, 0) + 1
    total = sum(counts.values())
    if total == 0 or not vendors:
        return {vendor: 1.0 / len(vendors) for vendor in vendors} if vendors else {}
    return {vendor: counts.get(vendor, 0) / total for vendor in vendors}


def build_activities(scope: str, ledger: Any, now: Optional[float] = None) -> Tuple[List[Activity], int, Dict[str, int]]:
    """The day's activities at full scale, the tokens the keyed vendors can still serve today, and that total per vendor."""
    from .society import academy, cycles, store, tick

    stamp = float(now if now is not None else time.time())
    config = config_for(scope)
    values, mins = config["values"], config["min_scale"]
    capacities = vendor_capacities(ledger)
    vendors = list(capacities)
    budget = int(sum(capacities.values()))
    chat_mix = vendor_mix(scope, ("normal_chat", "chat_bot", "task_finder", "repository"), vendors)
    society_mix = vendor_mix(scope, ("society",), vendors)
    hours = _hours_left(stamp)
    reserve = max(CHAT_RESERVE_MIN_TOKENS, int(budget * config["chat_reserve_fraction"]))
    activities = [Activity("chat", "Chat reserve", min(reserve, max(budget, 1)), float(values["chat"]), min_scale=1.0, lead_days=1.0, mix=chat_mix)]
    for company in store.companies_for(scope):
        cid = int(company["id"])
        cycles_left = max(1, int(math.ceil(hours * 3600.0 / max(600.0, float(company.get("interval_s") or cycles.DEFAULT_MAX_TOKENS)))))
        activities.append(Activity(f"company:{cid}", str(company.get("name") or f"Company {cid}"), int(cycles.DEFAULT_MAX_TOKENS * cycles_left),
                                   float(values["company"]), min_scale=float(mins["company"]), lead_days=0.5, prerequisite="chat", company_id=cid, mix=society_mix))
    if store.agents_for(scope, limit=1):
        academy_cycles = max(1, int(math.ceil(hours / 3.0)))
        activities.append(Activity("academy", "Academy", int(academy.DEFAULT_MAX_TOKENS * academy_cycles), float(values["academy"]), min_scale=float(mins["academy"]), mix=society_mix))
        ticks = max(1, int(math.ceil(hours * 2)))
        activities.append(Activity("leisure", "Leisure", int(tick.LEISURE_MAX_TOKENS * ticks), float(values["leisure"]), min_scale=float(mins["leisure"]), mix=society_mix))
    queued = vault.list_jobs(scope, ("queued",), limit=100, kind="mission")
    if queued:
        activities.append(Activity("missions", f"{len(queued)} queued mission(s)", int(MISSION_TOKENS_PER_JOB * len(queued)), float(values["missions"]), min_scale=float(mins["missions"]), lead_days=1.0, mix=chat_mix))
    return [a for a in activities if a.cost_tokens > 0], budget, capacities


def fallback_plan(scope: str, activities: Sequence[Activity], budget: int, goal_points: float, reason: str, now: float) -> DayPlan:
    """No engine: the fixed treasury shares decide; every activity at full scale within its share (today's behaviour)."""
    lines = [PlanLine(a.key, a.label, 1.0, int(a.cost_tokens), float(a.value_points)) for a in activities]
    plan = DayPlan(scope, time.strftime("%Y-%m-%d", time.gmtime(now)), int(budget), float(goal_points), "fallback" if available() else "engine-missing", lines,
                   spend_tokens=sum(line.tokens for line in lines), value_points=sum(line.value_points for line in lines), full_value_points=sum(a.value_points for a in activities),
                   notes=[reason], created_at=now)
    return plan


def vendor_stress(scope: str, hours: float = 72.0, now: Optional[float] = None) -> Tuple[float, List[Dict[str, Any]]]:
    """Fit each vendor's failure elasticity to load from hourly route-log bins; return (macro multiplier, rows).

    Load per bin is calls over the busiest bin; failures per bin are routes marked failed. The engine's
    estimator refuses thin or flat evidence, and a refused vendor contributes no stress. The multiplier
    is 1 / (1 + e * max((load_now - anchor) / anchor, 0)), averaged over usable vendors, floored at 0.5.
    """
    if not available():
        return 1.0, []
    from scrcae.calibration import DeliveryObservation, FitQuality, calibrate_elasticity

    stamp = float(now if now is not None else time.time())
    rows = vault.recent_routes(scope, limit=5000, hours=hours)
    bins: Dict[str, Dict[int, List[int]]] = {}
    for row in rows:
        route = str(row["route"] or "")
        vendor = route.split("/", 1)[0] if route and route != "failed" else ""
        if not vendor and route == "failed":
            vendor = str(row["reason"] or "").split(" ", 1)[0] or "unknown"
        if not vendor:
            continue
        hour = int(float(row["timestamp"]) // 3600)
        calls, fails = bins.setdefault(vendor, {}).setdefault(hour, [0, 0])
        bins[vendor][hour] = [calls + 1, fails + (1 if route == "failed" else 0)]
    stress_rows: List[Dict[str, Any]] = []
    multipliers: List[float] = []
    current_hour = int(stamp // 3600)
    for vendor, per_hour in sorted(bins.items()):
        peak = max(c for c, _ in per_hour.values()) or 1
        observations = [DeliveryObservation(period=f"h{h}", market_price=c / peak, disruption_rate=f / c) for h, (c, f) in sorted(per_hour.items()) if c > 0]
        if len(observations) < STRESS_MIN_BINS:
            stress_rows.append({"vendor": vendor, "bins": len(observations), "quality": "too few bins", "elasticity": None, "load_now": None, "multiplier": 1.0})
            continue
        fit = calibrate_elasticity(observations, anchor=STRESS_ANCHOR)
        load_now = per_hour.get(current_hour, [0, 0])[0] / peak
        multiplier = 1.0
        if fit.quality == FitQuality.USABLE and fit.elasticity is not None and fit.elasticity > 0:
            multiplier = max(STRESS_FLOOR, 1.0 / (1.0 + float(fit.elasticity) * max((load_now - STRESS_ANCHOR) / STRESS_ANCHOR, 0.0)))
            multipliers.append(multiplier)
        stress_rows.append({"vendor": vendor, "bins": len(observations), "quality": fit.quality.name.lower().replace("_", " "),
                            "elasticity": None if fit.elasticity is None else round(float(fit.elasticity), 3),
                            "interval": None if fit.interval_low is None else (round(float(fit.interval_low), 3), round(float(fit.interval_high), 3)),
                            "load_now": round(load_now, 2), "multiplier": round(multiplier, 3)})
    macro = sum(multipliers) / len(multipliers) if multipliers else 1.0
    return max(STRESS_FLOOR, min(1.0, macro)), stress_rows


def tail_risk(scope: str, plan: DayPlan, sigma: Optional[float] = None, rho: Optional[float] = None) -> Dict[str, float]:
    """P50/P90/expected shortfall of delivered value under correlated lognormal shocks; sigma from the failure share, rho from the coupling."""
    if not available():
        return {}
    import numpy as np
    from scrcae import SimulationRequest
    from scrcae.stochastic import LognormalShock, UniformCorrelation, montecarlo

    funded = [line for line in plan.lines if line.scale > 0.0 and line.value_points > 0.0]
    if not funded:
        return {}
    if sigma is None:
        rows = vault.recent_routes(scope, limit=2000, hours=72.0)
        failed = sum(1 for r in rows if str(r["route"]) == "failed")
        sigma = SIGMA_FLOOR + (failed / len(rows) if rows else 0.0)
    sigma = max(SIGMA_FLOOR, min(SIGMA_CEILING, float(sigma)))
    if rho is None:
        from . import dynamics

        coupling = dynamics.cached_coupling(scope)
        rho = sum(abs(v) for v in coupling.values()) / len(coupling) if coupling else 0.0
    rho = max(0.0, min(0.95, float(rho)))
    order = [line.key for line in funded]
    result = montecarlo.run(SimulationRequest(
        baseline_risk_pts=BASELINE_POINTS, risk_reduction_by_node={line.key: float(line.value_points) for line in funded},
        correlation=UniformCorrelation(rho).matrix(order) if len(order) > 1 else np.array([[1.0]]), node_order=order,
        iterations=MC_ITERATIONS, seed=MC_SEED, shock_model=LognormalShock(sigma),
    ))
    delivered = lambda risk: BASELINE_POINTS - float(risk)  # noqa: E731
    return {"p50_points": round(delivered(result.p50_risk_pts), 2), "p90_points": round(delivered(result.p90_risk_pts), 2),
            "p10_points": round(delivered(result.p10_risk_pts), 2), "expected_shortfall_points": round(delivered(result.expected_shortfall_90_pts), 2),
            "deterministic_points": round(delivered(result.deterministic_risk_pts), 2), "bias_points": round(float(result.reduction_bias_pts), 3),
            "sigma": round(sigma, 3), "rho": round(rho, 3), "iterations": float(result.iterations), "p50_standard_error": round(float(result.p50_standard_error), 3)}


def plan_day(scope: str, ledger: Any, goal_points: Optional[float] = None, now: Optional[float] = None, activities: Optional[Sequence[Activity]] = None,
             budget: Optional[int] = None, capacities: Optional[Mapping[str, int]] = None, with_tail: bool = True, with_stress: bool = True,
             store_result: bool = True) -> DayPlan:
    """The plan of the day: the most value within today's tokens (per vendor when the engine knows resources), the goal checked in target mode."""
    stamp = float(now if now is not None else time.time())
    if activities is None or budget is None:
        built, built_budget, built_capacities = build_activities(scope, ledger, stamp)
        activities = built if activities is None else activities
        budget = built_budget if budget is None else budget
        capacities = built_capacities if capacities is None else capacities
    activities = list(activities)
    budget = max(0, int(budget))
    capacities = {str(k): max(0, int(v)) for k, v in (capacities or {}).items()}
    config = config_for(scope)
    full_value = sum(a.value_points for a in activities)
    goal = float(goal_points) if goal_points is not None else round(full_value * config["goal_fraction"], 2)
    if not activities:
        plan = DayPlan(scope, time.strftime("%Y-%m-%d", time.gmtime(stamp)), budget, goal, "fallback", notes=["nothing to plan: no company, academy, or queued mission"], created_at=stamp)
        return _store(scope, plan, store_result)
    if not available():
        return _store(scope, fallback_plan(scope, activities, budget, goal, "engine not installed (requirements-supply.txt, Python 3.12+): fixed treasury shares apply", stamp), store_result)
    started = time.perf_counter()
    try:
        plan = _solve(scope, activities, budget, goal, stamp, with_stress, capacities)
    except Exception as exc:  # the society never stops because a planner failed
        plan = fallback_plan(scope, activities, budget, goal, f"planner error, fixed shares apply: {str(exc)[:160]}", stamp)
        plan.status = "error"
    plan.ms = int((time.perf_counter() - started) * 1000)
    if with_tail and plan.status in ("optimal", "short", "ceiling"):
        try:
            plan.tail = tail_risk(scope, plan)
        except Exception as exc:
            plan.notes.append(f"tail risk unavailable: {str(exc)[:120]}")
    return _store(scope, plan, store_result)


def engine_knows_resources() -> bool:
    try:
        from scrcae import Resource  # noqa: F401
    except Exception:
        return False
    return True


def _solve(scope: str, activities: Sequence[Activity], budget: int, goal: float, stamp: float, with_stress: bool, capacities: Optional[Mapping[str, int]] = None) -> DayPlan:
    import scrcae
    from scrcae import Dependency, Intervention, MinimizeCapitalObjective, OptimizationRequest, SupplyNetwork, solve
    from scrcae.optimization import budget_levels, sweep_budget
    from scrcae.optimization.objectives import MaximizeRiskReductionObjective
    from scrcae.risk import LinearResponse

    full_value = sum(a.value_points for a in activities)
    # Safety stock is not a candidate: the chat reserve is held back from the budget and from every vendor's supply
    # before anything is allocated, so no configured value can ever trade it away.
    reserve = next((a for a in activities if a.key == "chat"), None)
    candidates = [a for a in activities if a.key != "chat"]
    held = min(int(reserve.cost_tokens), budget) if reserve else 0
    solve_budget = max(0, budget - held)
    supply: Dict[str, float] = {k: float(v) for k, v in (capacities or {}).items()}
    if reserve and supply:
        for vendor, tokens in reserve.usage().items():
            if vendor in supply:
                supply[vendor] = max(0.0, supply[vendor] - tokens * (held / max(reserve.cost_tokens, 1)))
    reserve_line = [PlanLine(reserve.key, reserve.label, 1.0 if held >= reserve.cost_tokens else round(held / max(reserve.cost_tokens, 1), 4), held,
                             round(float(reserve.value_points) * (held / max(reserve.cost_tokens, 1)), 3))] if reserve else []
    reserve_value = reserve_line[0].value_points if reserve_line else 0.0
    stress, stress_rows = vendor_stress(scope, now=stamp) if with_stress else (1.0, [])
    day = time.strftime("%Y-%m-%d", time.gmtime(stamp))
    if not candidates:
        return DayPlan(scope, day, budget, goal, "optimal", reserve_line, spend_tokens=held, value_points=reserve_value, full_value_points=round(full_value, 3),
                       stress=round(float(stress), 3), stress_rows=stress_rows, engine=engine_version(), created_at=stamp, notes=["only the chat reserve is planned today"])
    keys = {a.key for a in candidates}
    # Vendors as resources (engine 6f0fc8b and later): each activity draws its tokens from the vendors its workspace routes to.
    with_resources = bool(supply) and engine_knows_resources() and any(a.mix for a in candidates)
    resources = tuple(scrcae.Resource(vendor, float(capacity)) for vendor, capacity in sorted(supply.items())) if with_resources else ()
    declared = {r.name for r in resources}
    interventions = tuple(
        Intervention(a.key, a.label, "", cost=float(a.cost_tokens), risk_reduction_pts=float(a.value_points), lead_time_saved_days=float(a.lead_days),
                     min_funding_scale=float(a.min_scale), max_funding_scale=float(a.max_scale),
                     **({"usage": {v: u for v, u in a.usage().items() if v in declared}} if with_resources else {}))
        for a in candidates
    )
    dependencies = tuple(Dependency(dependent=a.key, prerequisite=a.prerequisite) for a in candidates if a.prerequisite and a.prerequisite in keys and a.prerequisite != a.key)
    network = SupplyNetwork(interventions=interventions, baseline_risk_pts=BASELINE_POINTS, dependencies=dependencies, **({"resources": resources} if with_resources else {}))
    common = {"network": network, "risk_response": LinearResponse(), "macro_multiplier": stress, "enforce_risk_cap": True}
    # Free-tier tokens expire at midnight, so the plan is "the most value today's tokens can buy" (budget mode). The goal
    # is a check, solved in target mode: feasible means the day is on plan; infeasible names the shortfall in tokens or
    # the ceiling, and that verdict rides along with the budget-mode allocation.
    result = solve(OptimizationRequest(objective=MaximizeRiskReductionObjective(), budget=float(solve_budget), diagnose=False, **common))
    target = solve(OptimizationRequest(objective=MinimizeCapitalObjective(), budget=float(solve_budget), required_risk_reduction_pts=max(0.0, float(goal) - reserve_value), **common))
    status, diagnosis, shortfall = "optimal", "", 0
    if target.status != "optimal":
        kind = str(getattr(target.diagnosis, "kind", "") or "")
        diagnosis = str(getattr(target.diagnosis, "summary", lambda: "")() or f"goal infeasible ({target.status})")
        required = getattr(target.diagnosis, "capital_required", None)
        if kind == "exceeds_budget" and required:
            shortfall, status = max(0, int(math.ceil(float(required) - solve_budget)), 1), "short"
        else:
            status = "ceiling"
    lines = reserve_line + [PlanLine(a.node_id, a.name, round(float(a.funding_scale), 4), int(round(float(a.capital))), round(float(a.risk_reduction_pts), 3)) for a in result.allocations]
    plan = DayPlan(scope, day, budget, goal, status if result.status == "optimal" else "error", lines,
                   spend_tokens=held + int(round(float(result.net_capital))), value_points=round(reserve_value + float(result.baseline_risk_pts - result.optimized_risk_pts), 3),
                   full_value_points=round(full_value, 3), shortfall_tokens=shortfall, diagnosis=diagnosis, stress=round(float(stress), 3), stress_rows=stress_rows,
                   engine=engine_version(), input_hash=str(result.audit.get("input_hash", "")), output_hash=str(result.audit.get("output_hash", "")), created_at=stamp)
    if result.status != "optimal":
        plan.notes.append(f"budget-mode solve returned {result.status}")
    if with_resources:
        use = dict(getattr(result, "resource_use", {}) or {})
        reserved_by_vendor = {v: t * (held / max(reserve.cost_tokens, 1)) for v, t in reserve.usage().items()} if reserve else {}
        plan.resources = [
            {"vendor": r.name, "capacity": int((capacities or {}).get(r.name, r.capacity)), "reserved": int(round(reserved_by_vendor.get(r.name, 0.0))),
             "planned": int(round(float(use.get(r.name, 0.0)))), "headroom": int(round(r.capacity - float(use.get(r.name, 0.0))))}
            for r in resources
        ]
        limited = str(getattr(getattr(target.diagnosis, "attainable", None), "limited_by", "") or "")
        if limited.startswith("resource:"):
            plan.notes.append(f"the goal is bounded by the daily supply of {limited.split(':', 1)[1]}, not by the pooled budget")
    violations = getattr(result.constraint_report, "violations", ())
    if violations:
        plan.notes.append(f"{len(violations)} constraint violation(s) reported by the engine's re-check")
    try:
        sweep = sweep_budget(OptimizationRequest(objective=MaximizeRiskReductionObjective(), budget=float(max(solve_budget, 1)), diagnose=False, **common), budget_levels(max(solve_budget, 1)))
        saturation = getattr(sweep, "saturation_budget", None)
        if saturation is not None:
            plan.saturation_budget = held + int(saturation)
            plan.recommended_share = round(min(1.0, float(plan.saturation_budget) / float(max(budget, 1))), 3)
    except Exception as exc:
        plan.notes.append(f"budget sweep unavailable: {str(exc)[:120]}")
    return plan


def _store(scope: str, plan: DayPlan, store_result: bool) -> DayPlan:
    if store_result:
        try:
            vault.setting_set(scope, SETTING_LATEST, plan.to_json())
        except Exception:
            pass
    return plan


def latest(scope: str) -> Optional[Dict[str, Any]]:
    raw = vault.setting_get(scope, SETTING_LATEST, "")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None

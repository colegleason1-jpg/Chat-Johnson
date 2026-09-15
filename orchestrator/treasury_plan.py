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


# ----------------------------------------------------------------------------- commitments: waves and missions

@dataclass
class Commitment:
    """A unit of work that runs whole or not at all: a wave's work, a mission's step."""

    key: str
    label: str
    cost_tokens: int
    value_points: float
    mix: Dict[str, float] = field(default_factory=dict)
    prerequisite: str = ""

    def usage(self) -> Dict[str, float]:
        return {vendor: float(self.cost_tokens) * share for vendor, share in self.mix.items() if share > 0.0}


@dataclass
class Verdict:
    kind: str                    # wave | mission
    fits: bool
    status: str                  # fits | short | supply | fallback | engine-missing | empty | error
    cost_tokens: int
    budget_tokens: int
    shortfall_tokens: int = 0
    funded: List[str] = field(default_factory=list)
    unfunded: List[str] = field(default_factory=list)
    limited_by: str = ""
    diagnosis: str = ""
    resources: List[Dict[str, Any]] = field(default_factory=list)
    engine: str = ""
    input_hash: str = ""
    ms: int = 0
    notes: List[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.status == "empty":
            return "nothing left to fund"
        if self.fits:
            return f"fits today: {self.cost_tokens:,} of {self.budget_tokens:,} tokens left after the chat reserve"
        if self.status == "supply" and self.limited_by:
            return f"bounded by {self.limited_by}'s supply today: {len(self.unfunded)} of {len(self.funded) + len(self.unfunded)} left out; {self.cost_tokens:,} tokens needed"
        return f"short by {self.shortfall_tokens:,} tokens today ({self.cost_tokens:,} needed, {self.budget_tokens:,} left after the chat reserve); {len(self.unfunded)} of {len(self.funded) + len(self.unfunded)} left out"


def spendable_today(scope: str, ledger: Any) -> Tuple[int, Dict[str, int]]:
    """Tokens left today after the chat reserve, pooled and per vendor (the reserve comes off every vendor by the chat mix)."""
    config = config_for(scope)
    capacities = vendor_capacities(ledger)
    budget = int(sum(capacities.values()))
    reserve = min(budget, max(CHAT_RESERVE_MIN_TOKENS, int(budget * config["chat_reserve_fraction"])))
    mix = vendor_mix(scope, ("normal_chat", "chat_bot", "task_finder", "repository"), list(capacities))
    per_vendor = {vendor: max(0, int(capacity - reserve * mix.get(vendor, 0.0))) for vendor, capacity in capacities.items()}
    return max(0, budget - reserve), per_vendor


def feasibility(scope: str, ledger: Any, items: Sequence[Commitment], kind: str, budget: Optional[int] = None, capacities: Optional[Mapping[str, int]] = None) -> Verdict:
    """Can these commitments all run today? Whole-or-nothing nodes; the answer names the shortfall or the vendor that binds."""
    started = time.perf_counter()
    items = [item for item in items if item.cost_tokens > 0]
    if budget is None or capacities is None:
        pooled, per_vendor = spendable_today(scope, ledger)
        budget = pooled if budget is None else budget
        capacities = per_vendor if capacities is None else capacities
    budget = max(0, int(budget))
    supply = {str(k): max(0, int(v)) for k, v in (capacities or {}).items()}
    total = sum(item.cost_tokens for item in items)
    if not items:
        return Verdict(kind, True, "empty", 0, budget)
    if not available():
        verdict = _greedy(kind, items, budget, supply)
        verdict.status = "engine-missing" if verdict.fits else verdict.status
        verdict.notes.append("engine not installed: greedy check by value per token")
        verdict.ms = int((time.perf_counter() - started) * 1000)
        return verdict
    try:
        verdict = _solve_commitments(kind, items, budget, supply)
    except Exception as exc:
        verdict = _greedy(kind, items, budget, supply)
        verdict.status = "error"
        verdict.notes.append(f"planner error, greedy check applies: {str(exc)[:160]}")
    verdict.cost_tokens = total
    verdict.ms = int((time.perf_counter() - started) * 1000)
    return verdict


def _greedy(kind: str, items: Sequence[Commitment], budget: int, supply: Mapping[str, int]) -> Verdict:
    total = sum(item.cost_tokens for item in items)
    left, room = budget, dict(supply)
    funded, unfunded = [], []
    for item in sorted(items, key=lambda i: -(i.value_points / max(i.cost_tokens, 1))):
        usage = item.usage() if room else {}
        if item.cost_tokens <= left and all(room.get(v, 0) >= u for v, u in usage.items() if v in room):
            funded.append(item.key)
            left -= item.cost_tokens
            for v, u in usage.items():
                if v in room:
                    room[v] -= int(u)
        else:
            unfunded.append(item.key)
    fits = not unfunded
    order = {item.key: index for index, item in enumerate(items)}
    funded.sort(key=order.get)
    unfunded.sort(key=order.get)
    return Verdict(kind, fits, "fits" if fits else "short", total, budget, max(0, total - budget) if not fits else 0, funded, unfunded,
                   resources=[{"vendor": v, "capacity": int(c), "headroom": int(room.get(v, 0))} for v, c in supply.items()])


def _solve_commitments(kind: str, items: Sequence[Commitment], budget: int, supply: Mapping[str, int]) -> Verdict:
    import scrcae
    from scrcae import Dependency, Intervention, MinimizeCapitalObjective, OptimizationRequest, SupplyNetwork, solve
    from scrcae.optimization.objectives import MaximizeRiskReductionObjective
    from scrcae.risk import LinearResponse

    keys = {item.key for item in items}
    with_resources = bool(supply) and engine_knows_resources() and any(item.mix for item in items)
    resources = tuple(scrcae.Resource(v, float(c)) for v, c in sorted(supply.items())) if with_resources else ()
    declared = {r.name for r in resources}
    total_value = sum(item.value_points for item in items)
    scale = min(1.0, BASELINE_POINTS / total_value) if total_value > 0 else 1.0  # keep points inside the engine's 100-point baseline
    interventions = tuple(
        Intervention(item.key, item.label, "", cost=float(item.cost_tokens), risk_reduction_pts=float(item.value_points) * scale, min_funding_scale=1.0, max_funding_scale=1.0,
                     **({"usage": {v: u for v, u in item.usage().items() if v in declared}} if with_resources else {}))
        for item in items
    )
    dependencies = tuple(Dependency(dependent=i.key, prerequisite=i.prerequisite) for i in items if i.prerequisite and i.prerequisite in keys and i.prerequisite != i.key)
    network = SupplyNetwork(interventions=interventions, baseline_risk_pts=BASELINE_POINTS, dependencies=dependencies, **({"resources": resources} if with_resources else {}))
    common = {"network": network, "risk_response": LinearResponse(), "enforce_risk_cap": True}
    best = solve(OptimizationRequest(objective=MaximizeRiskReductionObjective(), budget=float(budget), diagnose=False, **common))
    funded = [a.node_id for a in best.allocations if a.funding_scale > 0.5]
    unfunded = [item.key for item in items if item.key not in funded]
    total = sum(item.cost_tokens for item in items)
    verdict = Verdict(kind, not unfunded, "fits" if not unfunded else "short", total, budget, 0, funded, unfunded, engine=engine_version(), input_hash=str(best.audit.get("input_hash", "")))
    if with_resources:
        use = dict(getattr(best, "resource_use", {}) or {})
        verdict.resources = [{"vendor": r.name, "capacity": int(r.capacity), "planned": int(round(float(use.get(r.name, 0.0)))), "headroom": int(round(r.capacity - float(use.get(r.name, 0.0))))} for r in resources]
    if unfunded:
        check = solve(OptimizationRequest(objective=MinimizeCapitalObjective(), budget=float(budget), required_risk_reduction_pts=float(total_value) * scale - 1e-6, **common))
        diagnosis = getattr(check, "diagnosis", None)
        verdict.diagnosis = str(getattr(diagnosis, "summary", lambda: "")() or "")
        required = getattr(diagnosis, "capital_required", None)
        limited = str(getattr(getattr(diagnosis, "attainable", None), "limited_by", "") or "")
        if limited.startswith("resource:"):
            verdict.status, verdict.limited_by = "supply", limited.split(":", 1)[1]
        verdict.shortfall_tokens = max(0, int(math.ceil(float(required) - budget))) if required else max(0, total - budget)
    if best.status != "optimal":
        verdict.notes.append(f"solve returned {best.status}")
    return verdict


def mission_feasibility(scope: str, ledger: Any, plan: Sequence[Mapping[str, Any]], budget_per_step: int, heavy: bool = False, budget: Optional[int] = None, capacities: Optional[Mapping[str, int]] = None) -> Verdict:
    """Before Launch: every model step must run, in order; sub-missions count their sections; deterministic nodes cost nothing."""
    from .router import prompt_context_chars

    prompt_tokens = prompt_context_chars(int(budget_per_step)) // 4
    per_call = int(budget_per_step) * (1.83 if heavy else 1.0) + prompt_tokens  # Heavy: draft b/2 + critique b/3 + synthesis b
    if budget is None or capacities is None:
        pooled, per_vendor = spendable_today(scope, ledger)
        budget = pooled if budget is None else budget
        capacities = per_vendor if capacities is None else capacities
    mix = vendor_mix(scope, ("task_finder", "normal_chat"), list(capacities or {}))
    items: List[Commitment] = []
    previous_model = ""
    for node in plan:
        executor = str(node.get("executor") or "model")
        if executor == "model":
            calls = 1
        elif executor == "sub_mission":
            calls = int((node.get("config") or {}).get("sections") or 3)
        else:
            continue
        key = f"step:{int(node.get('id') or len(items) + 1)}"
        items.append(Commitment(key, str(node.get("title") or key), int(per_call * calls), 1.0, mix=dict(mix), prerequisite=previous_model))
        previous_model = key
    return feasibility(scope, ledger, items, "mission", budget=budget, capacities=capacities)


STAGES_TO_FINAL = ("backlog", "development", "draft", "edit", "preliminary_review", "board_feedback")
STAGE_CALLS = {"backlog": 6, "development": 6, "draft": 4, "edit": 3, "preliminary_review": 2, "board_feedback": 2}  # calls (task + review) left from that stage to final


def wave_items(scope: str, company_id: int, call_tokens: Optional[int] = None) -> Tuple[List[Commitment], Dict[str, Any]]:
    """The current wave's unfinished works as commitments: tokens to reach ``final`` from each work's stage, equal points toward the gate."""
    from .society import cycles, release

    status = release.wave_status(scope, company_id)
    works = release.wave_works(scope, company_id, int(status["wave"]))
    per_call = int(call_tokens or cycles.DEFAULT_CALL_TOKENS) * 2  # a call plus the context it carries
    needed = max(1, int(status["needed"]))
    capacities = list(vendor_capacities(None))
    mix = vendor_mix(scope, ("society",), capacities)
    items = [
        Commitment(f"work:{int(w['id'])}", str(w.get("title") or f"Work {w['id']}"), STAGE_CALLS.get(str(w.get("stage")), 0) * per_call, BASELINE_POINTS / needed, mix=dict(mix))
        for w in works if str(w.get("stage")) in STAGES_TO_FINAL
    ]
    return items, status


def wave_feasibility(scope: str, ledger: Any, company_id: int, call_tokens: Optional[int] = None) -> Tuple[Verdict, Dict[str, Any]]:
    """Can the company's current wave reach its gate with what the company may spend today?

    The budget is the company's line in the plan of the day when one exists, else its treasury share for the
    cycles left today; the vendors' supply is what is left after the chat reserve.
    """
    from .society import cycles, economy, store

    items, status = wave_items(scope, company_id, call_tokens)
    company = store.row("companies", int(company_id)) or {}
    pooled, per_vendor = spendable_today(scope, ledger)
    plan = latest(scope)
    line = next((line for line in (plan or {}).get("lines", []) if line.get("key") == f"company:{int(company_id)}"), None) if plan else None
    if line and int(line.get("tokens") or 0) > 0:
        budget = int(line["tokens"])
    else:
        treasury = economy.Treasury(ledger, economy.shares_for(scope))
        cycles_left = max(1, int(math.ceil(_hours_left(time.time()) * 3600.0 / max(600.0, float(company.get("interval_s") or 21_600)))))
        budget = treasury.cycle_budget(economy.share_key_for(company), cycles.DEFAULT_MAX_TOKENS, share=float(company.get("daily_share") or 0.0) or None) * cycles_left
    budget = min(int(budget), pooled)
    return feasibility(scope, ledger, items, "wave", budget=budget, capacities=per_vendor), status

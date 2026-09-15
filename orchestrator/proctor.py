"""The Monte Carlo proctor: statistics a single run cannot give; it reports, it never decides alone.

Two instruments, both CPU only and both fed by the same validated 1/f generator the router uses:

- ``simulate_routing`` re-runs Cortex 2 over many pink-wave realizations at amplified chaos and
  reports, per endpoint, how often it wins. *Fragility* is how often the deterministic winner loses
  at production strength; an *outlier* is an endpoint that never wins at production strength but
  does when the chaos is amplified. Capacity rows and keys are the same as in production, so a
  blocked endpoint never wins in the simulation either.
- ``forecast_budget`` models the rest of the UTC day's token demand as 1/f-correlated bursts around
  the observed hourly rate and reports the probability of hitting a vendor's daily cap and the hour
  it happens. ``should_defer`` turns the vendor reports into one scheduling answer for the society
  tick: wait when every keyed vendor is likely to cap inside the horizon.

Reports are computed on demand (a button, once per tick, a cached per-minute fragility for the
outcome log), never per send. Nothing here is a physical claim.
"""
from __future__ import annotations

import math
import threading
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import pinkwave
from .config import daily_cap
from .quota import QuotaLedger

AMPLIFICATIONS: Tuple[float, ...] = (1.0, 2.0, 4.0)   # 1.0 is production strength at gain 1
DEFAULT_PATHS = 64
FRAGILITY_TTL_SECONDS = 60.0
FRAGILITY_PATHS = 24
FORECAST_PATHS = 256
FORECAST_STEP_HOURS = 0.25
FORECAST_SIGMA = 0.6            # relative burstiness of demand around the observed rate
EARLY_MARGIN_HOURS = 2.0        # a path capping this much before the median is an early capper
DEFER_PROBABILITY = 0.5
DEFER_HORIZON_HOURS = 1.0


# ----------------------------------------------------------------------------- routing fragility

@dataclass
class RoutingReport:
    task_type: str
    estimated_tokens: int
    deterministic: str
    paths: int
    amplifications: Tuple[float, ...]
    win_rates: Dict[str, Dict[str, float]] = field(default_factory=dict)  # "x1.0" -> endpoint -> rate
    fragility: float = 0.0            # 1 - win rate of the deterministic winner at production strength
    fragility_amplified: float = 0.0  # the same at the strongest amplification
    outliers: List[str] = field(default_factory=list)
    blocked: List[str] = field(default_factory=list)
    ms: int = 0

    def rows(self) -> List[Dict[str, Any]]:
        names = sorted({name for rates in self.win_rates.values() for name in rates})
        return [
            {"endpoint": name, **{label: round(rates.get(name, 0.0), 3) for label, rates in self.win_rates.items()},
             "deterministic": name == self.deterministic, "outlier": name in self.outliers, "blocked": name in self.blocked}
            for name in names
        ]

    def summary(self) -> str:
        outliers = ", ".join(self.outliers) or "none"
        return (
            f"{self.task_type}: {self.deterministic} wins {1 - self.fragility:.0%} of {self.paths} paths at production strength "
            f"(fragility {self.fragility:.2f}, {self.fragility_amplified:.2f} at x{max(self.amplifications):g}); outliers: {outliers}"
        )


def _path_jitter(names: Sequence[str], path_seed: int, amplification: float, profile: str) -> Dict[str, float]:
    """One realization: endpoint k reads its own stretch of a fresh 1/f series, scaled like production at gain 1."""
    return {
        name: min(1.0, amplification * pinkwave.ROUTING_MAX_JITTER * pinkwave.unit("proctor", index * pinkwave.PHASE, profile, seed=path_seed))
        for index, name in enumerate(names)
    }


def simulate_routing(
    task_type: str,
    estimated_tokens: int,
    ledger: Optional[QuotaLedger] = None,
    paths: int = DEFAULT_PATHS,
    amplifications: Sequence[float] = AMPLIFICATIONS,
    current_usage: Optional[Mapping[str, Mapping[str, float]]] = None,
    excluded: Optional[Iterable[str]] = None,
    seed: Optional[int] = None,
    profile: str = "pink",
) -> RoutingReport:
    """Re-run Cortex 2 over ``paths`` pink-wave realizations per amplification; the report says how fragile the winner is."""
    from .router import CORTEX_ENDPOINTS, ProviderError, _endpoint_key, project_seth_routing_entropy, select_milp_endpoint

    started = time.perf_counter()
    names = list(CORTEX_ENDPOINTS)
    excluded_set = set(excluded or ())
    base = project_seth_routing_entropy(names)
    paths = max(1, int(paths))
    amps = tuple(float(a) for a in amplifications) or (1.0,)
    base_seed = int(seed) if seed is not None else zlib.crc32(task_type.encode("utf-8")) & 0xFFFFFFFF
    with pinkwave.suspended():
        deterministic = select_milp_endpoint(task_type, estimated_tokens, ledger=ledger, entropy_by_endpoint=base, current_usage=current_usage, excluded=excluded_set)
        win_rates: Dict[str, Dict[str, float]] = {}
        for amp in amps:
            wins = {name: 0 for name in names}
            for index in range(paths):
                jitter = _path_jitter(names, base_seed + index * 7919, amp, profile)
                penalties = {name: min(1.0, base.get(name, 0.0) + jitter[name]) for name in names}
                try:
                    decision = select_milp_endpoint(task_type, estimated_tokens, ledger=ledger, entropy_by_endpoint=penalties, current_usage=current_usage, excluded=excluded_set)
                except ProviderError:
                    break
                wins[decision.endpoint.name] += 1
            win_rates[f"x{amp:g}"] = {name: wins[name] / paths for name in names}
    first, last = win_rates[f"x{amps[0]:g}"], win_rates[f"x{max(amps):g}"]
    blocked = [name for name in names if name in excluded_set or not _endpoint_key(CORTEX_ENDPOINTS[name])]
    outliers = [name for name in names if first.get(name, 0.0) == 0.0 and last.get(name, 0.0) > 0.0 and name not in blocked]
    return RoutingReport(
        task_type=task_type, estimated_tokens=int(estimated_tokens), deterministic=deterministic.endpoint.name, paths=paths,
        amplifications=amps, win_rates=win_rates, fragility=round(1.0 - first.get(deterministic.endpoint.name, 0.0), 4),
        fragility_amplified=round(1.0 - last.get(deterministic.endpoint.name, 0.0), 4), outliers=outliers, blocked=blocked,
        ms=int((time.perf_counter() - started) * 1000),
    )


_FRAGILITY_CACHE: Dict[Tuple[str, Tuple[str, ...], int], Tuple[float, float]] = {}
_FRAGILITY_LOCK = threading.Lock()


def cached_fragility(task_type: str, estimated_tokens: int, ledger: Optional[QuotaLedger] = None, ttl: float = FRAGILITY_TTL_SECONDS) -> Optional[float]:
    """Production-strength fragility for the outcome log, recomputed at most once per minute per task type and key set."""
    from .router import CORTEX_ENDPOINTS, _endpoint_key

    keyed = tuple(sorted(name for name, endpoint in CORTEX_ENDPOINTS.items() if _endpoint_key(endpoint)))
    key = (task_type, keyed, int(estimated_tokens) // 1000)
    now = time.time()
    with _FRAGILITY_LOCK:
        hit = _FRAGILITY_CACHE.get(key)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
    try:
        value = simulate_routing(task_type, estimated_tokens, ledger=ledger, paths=FRAGILITY_PATHS, amplifications=(1.0,)).fragility
    except Exception:
        return None
    with _FRAGILITY_LOCK:
        _FRAGILITY_CACHE[key] = (now, value)
    return value


def reset_for_tests() -> None:
    with _FRAGILITY_LOCK:
        _FRAGILITY_CACHE.clear()


# ----------------------------------------------------------------------------- budget forecast

@dataclass
class BudgetReport:
    vendor: str
    cap: int
    used: int
    hour_now: float
    rate_per_hour: float
    paths: int
    p_cap_today: float = 0.0
    p10_cap_hour: Optional[float] = None   # the early tail: one path in ten caps by this hour
    p50_cap_hour: Optional[float] = None
    early_cappers: int = 0                  # paths capping more than EARLY_MARGIN_HOURS before the median

    @property
    def remaining(self) -> int:
        return max(0, int(self.cap) - int(self.used)) if self.cap > 0 else -1

    def row(self) -> Dict[str, Any]:
        return {
            "vendor": self.vendor, "cap": self.cap, "used": self.used, "remaining": self.remaining if self.cap > 0 else "uncapped",
            "rate/h": int(self.rate_per_hour), "p(cap today)": round(self.p_cap_today, 2),
            "p10 cap hour (UTC)": None if self.p10_cap_hour is None else round(self.p10_cap_hour, 1),
            "p50 cap hour (UTC)": None if self.p50_cap_hour is None else round(self.p50_cap_hour, 1),
            "early cappers": self.early_cappers,
        }

    def summary(self) -> str:
        if self.cap <= 0:
            return f"{self.vendor}: uncapped"
        if self.p50_cap_hour is None:
            return f"{self.vendor}: {self.remaining} tokens left, unlikely to cap today (p={self.p_cap_today:.2f})"
        early = f" (earliest tenth by {self.p10_cap_hour:.1f}h)" if self.p10_cap_hour is not None else ""
        return f"{self.vendor}: caps around {self.p50_cap_hour:.1f}h UTC in {self.p_cap_today:.0%} of paths{early}"


def _percentile(values: Sequence[float], fraction: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return float(ordered[index])


def forecast_budget(
    vendor: str,
    cap: int,
    used: int,
    hour_now: float,
    paths: int = FORECAST_PATHS,
    sigma: float = FORECAST_SIGMA,
    profile: str = "pink",
    seed: Optional[int] = None,
    rate_per_hour: Optional[float] = None,
) -> BudgetReport:
    """Monte Carlo over the rest of the UTC day: demand = observed rate x (1 + sigma x 1/f burst), per path; when does the cap fall?"""
    cap, used = max(0, int(cap)), max(0, int(used))
    hour_now = max(0.0, min(24.0, float(hour_now)))
    if rate_per_hour is None:
        rate_per_hour = used / hour_now if hour_now >= 0.5 else (cap / 24.0 if cap > 0 else 0.0)
    report = BudgetReport(vendor=vendor, cap=cap, used=used, hour_now=hour_now, rate_per_hour=float(rate_per_hour), paths=max(1, int(paths)))
    if cap <= 0 or rate_per_hour <= 0.0:
        return report
    if used >= cap:
        report.p_cap_today, report.p10_cap_hour, report.p50_cap_hour = 1.0, hour_now, hour_now
        return report
    steps = max(1, int(math.ceil((24.0 - hour_now) / FORECAST_STEP_HOURS)))
    base_seed = int(seed) if seed is not None else zlib.crc32(vendor.encode("utf-8")) & 0xFFFFFFFF
    per_step = float(rate_per_hour) * FORECAST_STEP_HOURS
    cap_hours: List[float] = []
    for index in range(report.paths):
        path_seed = base_seed + index * 104729
        total, hour = float(used), hour_now
        for step in range(steps):
            burst = pinkwave.signal("forecast", step, profile, seed=path_seed) if sigma > 0.0 else 0.0
            total += per_step * max(0.0, 1.0 + sigma * burst)
            hour += FORECAST_STEP_HOURS
            if total >= cap:
                cap_hours.append(min(24.0, hour))
                break
    report.p_cap_today = len(cap_hours) / report.paths
    report.p10_cap_hour = _percentile(cap_hours, 0.10)
    report.p50_cap_hour = _percentile(cap_hours, 0.50)
    if report.p50_cap_hour is not None:
        report.early_cappers = sum(1 for h in cap_hours if h < report.p50_cap_hour - EARLY_MARGIN_HOURS)
    return report


def utc_hour(now: Optional[float] = None) -> float:
    stamp = time.gmtime(now if now is not None else time.time())
    return stamp.tm_hour + stamp.tm_min / 60.0 + stamp.tm_sec / 3600.0


def forecast_vendors(ledger: Optional[QuotaLedger], vendors: Iterable[str], now: Optional[float] = None, paths: int = FORECAST_PATHS) -> List[BudgetReport]:
    """One report per keyed vendor from the shared daily counters (the same ones the router's caps use)."""
    hour = utc_hour(now)
    reports: List[BudgetReport] = []
    for vendor in dict.fromkeys(vendors):
        cap, used = daily_cap(vendor), 0
        if ledger is not None and ledger.known(vendor):
            usage = ledger.usage(vendor)
            cap = int(usage.get("daily_limit") or 0) or cap
            used = int(usage.get("daily_tokens", 0))
        reports.append(forecast_budget(vendor, cap, used, hour, paths=paths))
    return reports


def should_defer(
    reports: Sequence[BudgetReport], needed_tokens: int, horizon_hours: float = DEFER_HORIZON_HOURS, probability: float = DEFER_PROBABILITY
) -> Tuple[bool, str]:
    """Defer scheduled work when every capped vendor is out of headroom or likely to cap inside the horizon; never when one is clear."""
    if not reports:
        return False, "no keyed vendor to forecast"
    notes: List[str] = []
    for report in reports:
        if report.cap <= 0:
            return False, f"{report.vendor} is uncapped"
        if report.remaining < int(needed_tokens):
            notes.append(f"{report.vendor}: {report.remaining} tokens left, {int(needed_tokens)} needed")
            continue
        soon = report.p50_cap_hour is not None and report.p50_cap_hour <= report.hour_now + float(horizon_hours)
        if report.p_cap_today >= probability and soon:
            notes.append(f"{report.vendor}: {report.p_cap_today:.0%} of paths cap by {report.p50_cap_hour:.1f}h UTC")
            continue
        return False, f"{report.vendor} has headroom ({report.summary()})"
    return True, "deferred by the budget forecast: " + "; ".join(notes)

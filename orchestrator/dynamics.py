"""System dynamics for the learner: pairwise statistics between endpoints and constraint-law analogies.

Two layers, both bounded and both honest about what they are:

- **Pairwise statistics.** The endpoints' latency series (from the outcome log, binned) are compared
  pairwise: Pearson correlation, the lag of maximum cross-correlation, and a plug-in transfer entropy
  on median-symbolized series (lag 1) that says which endpoint's slowdowns lead which. When ``pyspi``
  (Python toolkit of Statistics for Pairwise Interactions) is installed it computes the same table
  with its own estimators and the report says so; without it the built-in numpy estimators run.
  ``pyspi`` is optional (``requirements-dynamics.txt``): it is heavy and never required on the free
  tier.
- **Constraint laws.** Analogies that map system state to a bounded utility modulation
  (``PHYSICS_MAX`` in total): *dissipation* (energy lost to failures grows with load), *friction*
  (latency drag grows with load), *momentum* (inertia favours the endpoint in use so routing does not
  churn), *coupling* (an endpoint statistically tied to a degraded one inherits part of its
  penalty). They shape near-ties; they never touch a capacity row, a key, or a timeout. They are
  routing analogies, not physical claims about the vendors.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

LAWS: Tuple[str, ...] = ("dissipation", "friction", "momentum", "coupling")
PHYSICS_MAX = 0.10          # total modulation per endpoint is clamped to +- this
MOMENTUM_BONUS = 0.05
COUPLING_THRESHOLD = 0.5    # |pearson| above this counts as coupled
BIN_MINUTES = 5
MIN_BINS = 8
MAX_LAG = 3
COUPLING_CACHE_SECONDS = 300.0


# ----------------------------------------------------------------------------- state and laws

@dataclass
class SystemState:
    telemetry: Mapping[str, Mapping[str, float]] = field(default_factory=dict)  # name -> {failure_rate, latency_ratio, observations}
    load: Mapping[str, float] = field(default_factory=dict)                     # name -> fraction of the RPM/TPM window in use
    last_used: Optional[str] = None
    coupling: Mapping[Tuple[str, str], float] = field(default_factory=dict)     # (a, b) -> |pearson| of their latency series


def load_fraction(endpoint: Any, usage: Any) -> float:
    """How much of the endpoint's window is in use: the larger of the RPM and TPM fractions, in [0, 1]."""
    if usage is None:
        return 0.0
    rpm_ceiling = float(usage.rpm_ceiling(endpoint) or 0.0)
    rpm = float(usage.rpm_used) / rpm_ceiling if rpm_ceiling > 0 else 0.0
    tpm_ceiling = usage.tpm_ceiling(endpoint)
    tpm = float(usage.tpm_used) / float(tpm_ceiling) if tpm_ceiling else 0.0
    return max(0.0, min(1.0, max(rpm, tpm)))


def _degradation(snapshot: Mapping[str, float]) -> float:
    failure = max(0.0, min(1.0, float(snapshot.get("failure_rate", 0.0))))
    latency = max(0.0, min(1.0, float(snapshot.get("latency_ratio", 0.0)) / 2.0))
    return max(0.0, min(1.0, 0.6 * failure + 0.4 * latency))


def law_dissipation(state: SystemState, name: str) -> float:
    snapshot = state.telemetry.get(name) or {}
    return -PHYSICS_MAX * max(0.0, min(1.0, float(snapshot.get("failure_rate", 0.0)))) * float(state.load.get(name, 0.0))


def law_friction(state: SystemState, name: str) -> float:
    snapshot = state.telemetry.get(name) or {}
    drag = max(0.0, min(1.0, float(snapshot.get("latency_ratio", 0.0)) / 2.0))
    return -PHYSICS_MAX * drag * float(state.load.get(name, 0.0))


def law_momentum(state: SystemState, name: str) -> float:
    return MOMENTUM_BONUS if state.last_used and name == state.last_used else 0.0


def law_coupling(state: SystemState, name: str) -> float:
    worst = 0.0
    for (a, b), strength in state.coupling.items():
        other = b if a == name else a if b == name else None
        if other is None or abs(float(strength)) < COUPLING_THRESHOLD:
            continue
        worst = max(worst, abs(float(strength)) * _degradation(state.telemetry.get(other) or {}))
    return -PHYSICS_MAX * worst


LAW_FUNCTIONS: Dict[str, Callable[[SystemState, str], float]] = {
    "dissipation": law_dissipation, "friction": law_friction, "momentum": law_momentum, "coupling": law_coupling,
}


def modulation(state: SystemState, names: Sequence[str], laws: Sequence[str] = LAWS) -> Dict[str, float]:
    """Per-endpoint utility modulation from the chosen laws, clamped to [-PHYSICS_MAX, +PHYSICS_MAX]."""
    active = [LAW_FUNCTIONS[law] for law in laws if law in LAW_FUNCTIONS]
    out: Dict[str, float] = {}
    for name in names:
        total = sum(fn(state, name) for fn in active)
        out[name] = round(max(-PHYSICS_MAX, min(PHYSICS_MAX, total)), 6)
    return out


# ----------------------------------------------------------------------------- pairwise statistics

@dataclass
class DependencyReport:
    names: List[str]
    backend: str
    bins: int
    pairs: Dict[Tuple[str, str], Dict[str, float]] = field(default_factory=dict)

    def coupling(self) -> Dict[Tuple[str, str], float]:
        return {pair: abs(float(stats.get("pearson", 0.0))) for pair, stats in self.pairs.items()}

    def rows(self) -> List[Dict[str, Any]]:
        out = []
        for (a, b), stats in self.pairs.items():
            flow = float(stats.get("te_ab", 0.0)) - float(stats.get("te_ba", 0.0))
            out.append({
                "pair": f"{a} ~ {b}", "pearson": round(float(stats.get("pearson", 0.0)), 3),
                "xcorr max": round(float(stats.get("xcorr_max", 0.0)), 3), "at lag": int(stats.get("xcorr_lag", 0)),
                "TE a->b": round(float(stats.get("te_ab", 0.0)), 4), "TE b->a": round(float(stats.get("te_ba", 0.0)), 4),
                "information flow": (f"{a} leads" if flow > 1e-6 else f"{b} leads" if flow < -1e-6 else "none"),
            })
        return out


def _standardize(values: Sequence[float]) -> List[float]:
    n = len(values)
    if n == 0:
        return []
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    std = math.sqrt(var) if var > 0 else 0.0
    return [((v - mean) / std) if std > 0 else 0.0 for v in values]


def pearson(a: Sequence[float], b: Sequence[float]) -> float:
    x, y = _standardize(a), _standardize(b)
    n = min(len(x), len(y))
    return (sum(x[i] * y[i] for i in range(n)) / n) if n else 0.0


def cross_correlation(a: Sequence[float], b: Sequence[float], max_lag: int = MAX_LAG) -> Tuple[float, int]:
    """(max |correlation|, lag): positive lag means ``a`` leads ``b`` by that many bins."""
    x, y = _standardize(a), _standardize(b)
    n = min(len(x), len(y))
    best, best_lag = 0.0, 0
    for lag in range(-max_lag, max_lag + 1):
        pairs = [(x[i], y[i + lag]) for i in range(n) if 0 <= i + lag < n]
        if len(pairs) < 3:
            continue
        value = sum(p * q for p, q in pairs) / len(pairs)
        if abs(value) > abs(best):
            best, best_lag = value, lag
    return best, best_lag


def transfer_entropy(source: Sequence[float], target: Sequence[float]) -> float:
    """Plug-in transfer entropy source -> target (bits) on median-symbolized series at lag 1; 0 when nothing is learned."""
    n = min(len(source), len(target))
    if n < 4:
        return 0.0

    def symbols(values: Sequence[float]) -> List[int]:
        ordered = sorted(values[:n])
        median = ordered[n // 2]
        return [1 if v > median else 0 for v in values[:n]]

    s, t = symbols(source), symbols(target)
    triples: Dict[Tuple[int, int, int], int] = {}
    for i in range(1, n):
        key = (t[i], t[i - 1], s[i - 1])
        triples[key] = triples.get(key, 0) + 1
    total = float(n - 1)
    pair_ts: Dict[Tuple[int, int], int] = {}
    pair_tt: Dict[Tuple[int, int], int] = {}
    single_t: Dict[int, int] = {}
    for (t_now, t_prev, s_prev), count in triples.items():
        pair_ts[(t_prev, s_prev)] = pair_ts.get((t_prev, s_prev), 0) + count
        pair_tt[(t_now, t_prev)] = pair_tt.get((t_now, t_prev), 0) + count
        single_t[t_prev] = single_t.get(t_prev, 0) + count
    te = 0.0
    for (t_now, t_prev, s_prev), count in triples.items():
        p_joint = count / total
        p_cond_full = count / pair_ts[(t_prev, s_prev)]
        p_cond_past = pair_tt[(t_now, t_prev)] / single_t[t_prev]
        te += p_joint * math.log2(p_cond_full / p_cond_past)
    return max(0.0, round(te, 6))


def pyspi_available() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("pyspi") is not None
    except Exception:
        return False


def _pyspi_pairs(series: Mapping[str, Sequence[float]]) -> Optional[Dict[Tuple[str, str], Dict[str, float]]]:
    """The same three statistics through pyspi's estimators; None when pyspi is missing or fails (the built-ins take over)."""
    try:
        import numpy as np
        from pyspi.calculator import Calculator  # type: ignore

        names = list(series)
        data = np.vstack([np.asarray(series[name], dtype=float) for name in names])
        calc = Calculator(dataset=data, subset="fast")
        calc.compute()
        table = calc.table
        out: Dict[Tuple[str, str], Dict[str, float]] = {}
        for i, a in enumerate(names):
            for j, b in enumerate(names):
                if j <= i:
                    continue
                stats: Dict[str, float] = {}
                for column, key in (("cov_EmpiricalCovariance", "pearson"), ("xcorr_max_sig-False", "xcorr_max"), ("te_kraskov_NN-4_k-1_kt-1_l-1_lt-1", "te_ab")):
                    if column in table.columns.get_level_values(0):
                        stats[key] = float(table[column].iloc[i, j])
                        if key == "te_ab":
                            stats["te_ba"] = float(table[column].iloc[j, i])
                stats.setdefault("pearson", pearson(series[a], series[b]))
                xc, lag = cross_correlation(series[a], series[b])
                stats.setdefault("xcorr_max", xc)
                stats["xcorr_lag"] = lag
                stats.setdefault("te_ab", transfer_entropy(series[a], series[b]))
                stats.setdefault("te_ba", transfer_entropy(series[b], series[a]))
                out[(a, b)] = stats
        return out
    except Exception:
        return None


def pairwise_statistics(series: Mapping[str, Sequence[float]], prefer_pyspi: bool = True) -> DependencyReport:
    """Pairwise dependency table for every pair of endpoint series with at least MIN_BINS aligned points."""
    usable = {name: list(values) for name, values in series.items() if len(values) >= MIN_BINS}
    names = sorted(usable)
    bins = min((len(v) for v in usable.values()), default=0)
    if len(names) < 2:
        return DependencyReport(names=names, backend="none", bins=bins)
    pairs = _pyspi_pairs(usable) if prefer_pyspi and pyspi_available() else None
    backend = "pyspi" if pairs is not None else "builtin"
    if pairs is None:
        pairs = {}
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                xc, lag = cross_correlation(usable[a], usable[b])
                pairs[(a, b)] = {
                    "pearson": pearson(usable[a], usable[b]), "xcorr_max": xc, "xcorr_lag": lag,
                    "te_ab": transfer_entropy(usable[a], usable[b]), "te_ba": transfer_entropy(usable[b], usable[a]),
                }
    return DependencyReport(names=names, backend=backend, bins=bins, pairs=pairs)


def endpoint_series(project_scope: str, hours: float = 24.0, bin_minutes: int = BIN_MINUTES) -> Dict[str, List[float]]:
    """Per endpoint, the mean latency per time bin over the window (bins with no send carry the last value forward)."""
    from . import vault

    rows = vault.recent_routes(project_scope, limit=5000, hours=hours)
    if not rows:
        return {}
    width = max(60.0, float(bin_minutes) * 60.0)
    now = time.time()
    start = now - float(hours) * 3600.0
    count = max(1, int(math.ceil((now - start) / width)))
    sums: Dict[str, List[float]] = {}
    counts: Dict[str, List[int]] = {}
    for row in rows:
        route = str(row["route"])
        if route == "failed":
            continue
        name = route.split("/")[0]
        index = min(count - 1, max(0, int((float(row["timestamp"]) - start) // width)))
        sums.setdefault(name, [0.0] * count)[index] += float(row["ms"])
        counts.setdefault(name, [0] * count)[index] += 1
    out: Dict[str, List[float]] = {}
    for name, totals in sums.items():
        values: List[float] = []
        last: Optional[float] = None
        for total, n in zip(totals, counts[name]):
            if n:
                last = total / n
            values.append(last if last is not None else float("nan"))
        first = next((v for v in values if not math.isnan(v)), None)
        if first is None:
            continue
        out[name] = [first if math.isnan(v) else v for v in values]
    return out


_COUPLING_CACHE: Dict[str, Tuple[float, Dict[Tuple[str, str], float]]] = {}
_COUPLING_LOCK = threading.Lock()


def cached_coupling(project_scope: str, now: Optional[float] = None) -> Dict[Tuple[str, str], float]:
    """|pearson| per endpoint pair from the last day of sends, recomputed at most every five minutes per scope."""
    scope = (project_scope or "").strip() or "default"
    stamp = float(now if now is not None else time.time())
    with _COUPLING_LOCK:
        hit = _COUPLING_CACHE.get(scope)
        if hit is not None and stamp - hit[0] < COUPLING_CACHE_SECONDS:
            return hit[1]
    try:
        coupling = pairwise_statistics(endpoint_series(scope), prefer_pyspi=False).coupling()
    except Exception:
        coupling = {}
    with _COUPLING_LOCK:
        _COUPLING_CACHE[scope] = (stamp, coupling)
    return coupling


def reset_for_tests() -> None:
    with _COUPLING_LOCK:
        _COUPLING_CACHE.clear()

"""Cortex 2 as an empirical learner: measured speed, Bayesian quality, pink-wave exploration.

The selector's utility used to be built from two hand-tuned constants per endpoint. This module
replaces them with measurements and closes the loop through the outcome log:

- **speed** comes from the recorded p50 latency of real sends (the vault's ``route_log``, with the
  in-memory telemetry as a second source), blended toward the table value while observations are
  few (``n / (n + k)``) so a handful of slow calls cannot swing routing;
- **quality** is a Beta prior per endpoint and task type, updated by the operator's verdicts (a
  thumbs up or a locked artifact is a success, a thumbs down a failure), so the posterior mean shifts
  utility by at most ``QUALITY_WEIGHT / 2`` either way and its width says how sure the estimate is;
- **exploration** is governed by the scope's pink wave: at gain ``g`` the selector routes a share
  ``explore_max * g`` of sends to a feasible runner-up (the one whose quality is least certain),
  never when the regret would exceed ``MAX_REGRET`` and never for context-heavy requests; the share
  is exact over one walk of the wave and the 1/f correlation makes the explorations cluster;
- the **dynamics** layer (``orchestrator/dynamics.py``) adds a bounded modulation from constraint-law
  analogies (dissipation, friction, momentum, coupling) computed from load, telemetry, and the
  pairwise statistics of the endpoints' latency series.

Every hard limit is untouched: capacity rows, keys, timeouts, and daily caps are evaluated before
any of this, and exploration can only pick an endpoint those rows already allowed.
"""
from __future__ import annotations

import contextvars
import json
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import pinkwave

SETTING_KEY = "learner"
QUALITY_WEIGHT = 0.30          # utility shift = QUALITY_WEIGHT * (posterior mean - 0.5): at most +-0.15
MAX_REGRET = 0.25              # never explore an endpoint whose utility trails the winner by more than this
EXPLORE_MAX_DEFAULT = 0.10     # share of sends explored at gain 1
SPEED_CONFIDENCE_N = 10        # observations at which the measured speed carries half the weight
SPEED_FAST_MS = 300.0          # p50 at or under this maps to speed 1.0
SPEED_SLOW_MS = 6_000.0        # p50 at or over this maps to speed 0.05
SPEED_FLOOR = 0.05
PRIOR_ALPHA = 2.0
PRIOR_BETA = 2.0
LATENCY_WINDOW_HOURS = 72.0
NO_EXPLORE_TASKS = ("context_load",)
CACHE_SECONDS = 30.0
OUTCOME_UPDATES = {
    "up": (1.0, 0.0), "locked": (1.0, 0.0), "down": (0.0, 1.0),
    # The app's own evidence closes the loop too: a cut answer and a page that threw are half a failure each; a send
    # the endpoint could not answer at all is a full one.
    "cut": (0.0, 0.5), "sandbox_error": (0.0, 0.5), "failed": (0.0, 1.0),
}
_WORKSPACE: contextvars.ContextVar = contextvars.ContextVar("learner_workspace", default="")


def set_workspace(workspace: str) -> None:
    """Momentum is scoped to the workspace sending: Normal Chat's last endpoint is no reason for a company cycle to follow it."""
    _WORKSPACE.set(str(workspace or ""))


def current_workspace() -> str:
    return str(_WORKSPACE.get() or "")


@dataclass(frozen=True)
class LearnerSettings:
    enabled: bool = True
    explore_max: float = EXPLORE_MAX_DEFAULT
    laws: Tuple[str, ...] = ("dissipation", "friction", "momentum", "coupling")

    def __post_init__(self) -> None:
        from .dynamics import LAWS

        object.__setattr__(self, "explore_max", max(0.0, min(0.5, float(self.explore_max))))
        object.__setattr__(self, "laws", tuple(law for law in self.laws if law in LAWS))

    def to_json(self) -> str:
        return json.dumps({"enabled": bool(self.enabled), "explore_max": self.explore_max, "laws": list(self.laws)}, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "LearnerSettings":
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        laws = data.get("laws")
        return cls(
            enabled=bool(data.get("enabled", True)), explore_max=float(data.get("explore_max", EXPLORE_MAX_DEFAULT)),
            laws=tuple(laws) if isinstance(laws, list) else ("dissipation", "friction", "momentum", "coupling"),
        )


def settings_for(project_scope: str) -> LearnerSettings:
    from . import vault

    return LearnerSettings.from_json(str(vault.setting_get(project_scope, SETTING_KEY, "") or ""))


def save_settings(project_scope: str, enabled: bool, explore_max: float, laws: Sequence[str]) -> LearnerSettings:
    from . import vault

    settings = LearnerSettings(enabled=bool(enabled), explore_max=float(explore_max), laws=tuple(laws))
    vault.setting_set(project_scope, SETTING_KEY, settings.to_json())
    invalidate(project_scope)
    return settings


def speed_from_p50(p50_ms: float) -> float:
    """Measured speed in [SPEED_FLOOR, 1]: linear from the fast bound to the slow bound."""
    if p50_ms <= SPEED_FAST_MS:
        return 1.0
    if p50_ms >= SPEED_SLOW_MS:
        return SPEED_FLOOR
    return max(SPEED_FLOOR, 1.0 - (p50_ms - SPEED_FAST_MS) / (SPEED_SLOW_MS - SPEED_FAST_MS) * (1.0 - SPEED_FLOOR))


@dataclass
class LearnedScore:
    endpoint: str
    speed: float
    speed_table: float
    p50_ms: Optional[float]
    speed_observations: int
    quality_mean: float
    quality_std: float
    quality_observations: int

    def row(self, task_type: str) -> Dict[str, Any]:
        return {
            "endpoint": self.endpoint, "task": task_type, "speed (learned)": round(self.speed, 3), "speed (table)": self.speed_table,
            "p50 ms": None if self.p50_ms is None else int(self.p50_ms), "latency obs": self.speed_observations,
            "quality mean": round(self.quality_mean, 3), "quality std": round(self.quality_std, 3), "verdicts": self.quality_observations,
        }


class Learner:
    """One scope's learned view of the endpoints; built from the vault, cached for CACHE_SECONDS."""

    def __init__(self, project_scope: str, settings: Optional[LearnerSettings] = None, now: Optional[float] = None) -> None:
        from . import vault

        self.project_scope = (project_scope or "").strip() or "default"
        self.settings = settings or settings_for(self.project_scope)
        self.created_at = float(now if now is not None else time.time())
        self.latency = vault.endpoint_latency_stats(self.project_scope, hours=LATENCY_WINDOW_HOURS)
        self.priors = vault.quality_priors_for(self.project_scope)
        recent = vault.recent_routes(self.project_scope, limit=60)
        answered = [row for row in recent if str(row["route"]) != "failed"]
        self.last_used = str(answered[0]["route"]).split("/")[0] if answered else None
        self.last_used_by_workspace: Dict[str, str] = {}
        for row in answered:
            workspace = str(row["workspace"] or "") if "workspace" in row.keys() else ""
            self.last_used_by_workspace.setdefault(workspace, str(row["route"]).split("/")[0])

    def momentum_endpoint(self) -> Optional[str]:
        """The endpoint the sending workspace used last (None when it has no history), else the scope's last."""
        workspace = current_workspace()
        if workspace:
            return self.last_used_by_workspace.get(workspace)
        return self.last_used

    @property
    def enabled(self) -> bool:
        return bool(self.settings.enabled)

    # ---- measured speed ----------------------------------------------------------------------

    def speed(self, endpoint: Any) -> Tuple[float, int, Optional[float]]:
        """(blended speed, observations, p50 ms): the vault's p50 first, the in-memory telemetry second, the table last."""
        from .router import LATENCY_BASELINE_SECONDS, telemetry_snapshot

        stats = self.latency.get(endpoint.name) or {}
        n, p50 = int(stats.get("n", 0)), stats.get("p50_ms")
        if n == 0:
            snapshot = telemetry_snapshot(endpoint.name)
            if snapshot.get("observations", 0.0) > 0:
                n = int(snapshot["observations"])
                p50 = (float(snapshot["latency_ratio"]) + 1.0) * LATENCY_BASELINE_SECONDS * 1000.0
        if n == 0 or p50 is None:
            return float(endpoint.speed_score), 0, None
        weight = n / (n + SPEED_CONFIDENCE_N)
        measured = speed_from_p50(float(p50))
        return weight * measured + (1.0 - weight) * float(endpoint.speed_score), n, float(p50)

    # ---- Bayesian quality --------------------------------------------------------------------

    def quality(self, endpoint_name: str, task_type: str) -> Tuple[float, float, int]:
        alpha, beta, n = self.priors.get((endpoint_name, task_type), (PRIOR_ALPHA, PRIOR_BETA, 0))
        total = alpha + beta
        mean = alpha / total
        variance = (alpha * beta) / (total * total * (total + 1.0))
        return mean, math.sqrt(max(0.0, variance)), int(n)

    def learned_scores(self, task_type: str, endpoints: Sequence[Any]) -> Dict[str, LearnedScore]:
        out: Dict[str, LearnedScore] = {}
        for endpoint in endpoints:
            speed, n_speed, p50 = self.speed(endpoint)
            mean, std, n_quality = self.quality(endpoint.name, task_type)
            out[endpoint.name] = LearnedScore(endpoint.name, speed, float(endpoint.speed_score), p50, n_speed, mean, std, n_quality)
        return out

    # ---- constraint-law modulation ----------------------------------------------------------

    def modulation(self, endpoints: Sequence[Any], usage: Mapping[str, Any]) -> Dict[str, float]:
        from . import dynamics
        from .router import telemetry_snapshot

        if not self.settings.laws:
            return {}
        state = dynamics.SystemState(
            telemetry={e.name: telemetry_snapshot(e.name) for e in endpoints},
            load={e.name: dynamics.load_fraction(e, usage.get(e.name)) for e in endpoints},
            last_used=self.momentum_endpoint(),
            coupling=dynamics.cached_coupling(self.project_scope),
        )
        return dynamics.modulation(state, [e.name for e in endpoints], self.settings.laws)

    # ---- exploration -------------------------------------------------------------------------

    def explore_rate(self, gain: float) -> float:
        return float(self.settings.explore_max) * max(0.0, min(1.0, float(gain)))

    def choose_exploration(self, feasible: Sequence[Any], utilities: Mapping[str, float], winner: str, task_type: str) -> Optional[Any]:
        """The feasible runner-up whose quality is least certain, inside the regret bound; None when exploring would cost too much."""
        if task_type in NO_EXPLORE_TASKS:
            return None
        best = float(utilities.get(winner, 0.0))
        candidates = [e for e in feasible if e.name != winner and float(utilities.get(e.name, -1.0)) >= best - MAX_REGRET]
        if not candidates:
            return None
        return max(candidates, key=lambda e: (self.quality(e.name, task_type)[1], float(utilities.get(e.name, 0.0))))


# ---- per-scope cache, outcome feedback, reports --------------------------------------------------

_CACHE: Dict[str, Learner] = {}
_LOCK = threading.Lock()


def for_scope(project_scope: str, now: Optional[float] = None) -> Learner:
    scope = (project_scope or "").strip() or "default"
    stamp = float(now if now is not None else time.time())
    with _LOCK:
        cached = _CACHE.get(scope)
        if cached is not None and stamp - cached.created_at < CACHE_SECONDS:
            return cached
    learner = Learner(scope, now=stamp)
    with _LOCK:
        _CACHE[scope] = learner
    return learner


def current() -> Optional[Learner]:
    """The learner of the active pink-wave scope, or None when no scope is active (library use, tests)."""
    chaos = pinkwave.current()
    if chaos is None:
        return None
    learner = for_scope(chaos.project_scope)
    return learner if learner.enabled else None


def invalidate(project_scope: Optional[str] = None) -> None:
    with _LOCK:
        if project_scope is None:
            _CACHE.clear()
        else:
            _CACHE.pop((project_scope or "").strip() or "default", None)


def observe_outcome(project_scope: str, endpoint: str, task_type: str, outcome: str) -> bool:
    """Fold one verdict into the endpoint's quality prior for that task type; False for outcomes that carry no verdict."""
    from . import vault

    delta = OUTCOME_UPDATES.get(outcome)
    if delta is None or not endpoint or endpoint == "failed":
        return False
    vault.quality_prior_update(project_scope, endpoint, task_type, delta[0], delta[1])
    invalidate(project_scope)
    return True


def rebuild_priors(project_scope: str) -> int:
    """Recompute every quality prior of a scope from the outcome log (idempotent); returns verdicts folded in."""
    from . import vault

    vault.quality_priors_reset(project_scope)
    count = 0
    for row in reversed(vault.recent_routes(project_scope, limit=5000)):
        keys = row.keys()
        outcome = str(row["outcome"]) if "outcome" in keys and row["outcome"] else ""
        endpoint = str(row["route"]).split("/")[0]
        if not outcome and "finish" in keys and str(row["finish"] or "") == "length":
            outcome = "cut"  # the app's own evidence: an answer the budget cut
        if observe_outcome(project_scope, endpoint, str(row["task_type"]), outcome):
            count += 1
    invalidate(project_scope)
    return count


def report(project_scope: str, task_types: Sequence[str]) -> Dict[str, Any]:
    """What the learner believes and how much it has explored, for the routing expander."""
    from . import vault
    from .router import CORTEX_ENDPOINTS, _endpoint_key

    learner = Learner(project_scope)
    endpoints = [e for e in CORTEX_ENDPOINTS.values() if _endpoint_key(e)]
    rows: List[Dict[str, Any]] = []
    for task_type in task_types:
        for name, score in learner.learned_scores(task_type, endpoints).items():
            rows.append(score.row(task_type))
    exploration = vault.exploration_stats(project_scope, hours=168.0)
    chaos = pinkwave.settings_for(project_scope)
    exploration["configured_rate"] = round(learner.explore_rate(chaos.gain), 4)
    return {"settings": learner.settings, "rows": rows, "exploration": exploration, "last_used": learner.last_used}


@dataclass
class ExplorationDecision:
    explored: bool = False
    rate: float = 0.0
    endpoint: str = ""
    reason: str = ""
    learned: Dict[str, Any] = field(default_factory=dict)

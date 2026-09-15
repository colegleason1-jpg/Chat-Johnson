"""Controlled chaos: the validated 1/f signal applied, bounded, across features.

The studio's math layer (``router.generate_one_over_f_noise``, the Project Seth stencil) is
reproducible and tested. This module walks along that signal, one step per use, at three
frequency profiles ("white" alpha 0.5 jitters fast, "pink" alpha 1.0, "brown" alpha 1.5
drifts slowly) and turns each sample into a small, bounded nudge:

- routing: a jitter added to the entropy penalty so near-ties between endpoints are broken
  differently over time; hard limits (RPM, TPM, daily caps, keys) never move;
- Heavy Mode: a temperature schedule (a warmer draft, a cold critique, the base synthesis);
- memory: how much of the prompt budget goes to long-distance recall from earlier chats;
- migration: how much recalled cross-chat memory a vision digest carries.

A gain of 0 switches every nudge off and the app behaves deterministically. Settings live per
project scope in the vault so the app and the worker walk the same wave; the step counter is
scoped too. Nothing here is a physical claim: it is a bounded routing and budgeting signal.
"""
from __future__ import annotations

import contextvars
import json
import math
import zlib
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, Iterable, Mapping, Optional, Tuple

PROFILES: Dict[str, float] = {"white": 0.5, "pink": 1.0, "brown": 1.5}  # alpha of the 1/f^alpha spectrum
FEATURES: Tuple[str, ...] = ("routing", "heavy", "memory", "migration")
DEFAULT_PROFILES: Dict[str, str] = {"routing": "pink", "heavy": "pink", "memory": "brown", "migration": "pink"}
DEFAULT_GAIN = 0.25
LENGTH = 1024                 # samples per series; the walk wraps around
PHASE = 97                    # endpoint i reads the wave at step + i * PHASE so endpoints are not nudged in lockstep
ROUTING_MAX_JITTER = 0.15     # of the [0, 1] entropy penalty -> at most 0.045 utility at gain 1
HEAVY_DRAFT_SPREAD = 0.4      # draft temperature = base + gain * spread * unit
HEAVY_CRITIQUE_TEMPERATURE = 0.0
RECALL_BASE_SHARE = 0.10      # of the prompt context budget, always available to long-distance memory
RECALL_SPREAD = 0.15          # + gain * spread * unit, so recall never exceeds a quarter of the budget
DIGEST_RECALL_BASE = 600      # characters of cross-chat memory a vision digest carries
DIGEST_RECALL_SPREAD = 600
SETTING_KEY = "chaos"

_ACTIVE: contextvars.ContextVar[Optional["Chaos"]] = contextvars.ContextVar("pinkwave_active", default=None)


@dataclass(frozen=True)
class ChaosSettings:
    gain: float = DEFAULT_GAIN
    profiles: Mapping[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "gain", max(0.0, min(1.0, float(self.gain))))
        merged = dict(DEFAULT_PROFILES)
        for feature, profile in dict(self.profiles or {}).items():
            if feature in FEATURES and profile in PROFILES:
                merged[feature] = profile
        object.__setattr__(self, "profiles", merged)

    def to_json(self) -> str:
        return json.dumps({"gain": self.gain, "profiles": dict(self.profiles)}, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "ChaosSettings":
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            data = {}
        return cls(gain=float(data.get("gain", DEFAULT_GAIN)), profiles=dict(data.get("profiles") or {}))


@lru_cache(maxsize=64)
def _series(profile: str, seed: int) -> Tuple[float, ...]:
    """One standardized 1/f^alpha realization per (profile, seed); zeros when numpy is missing (no chaos)."""
    from .router import generate_one_over_f_noise, np  # local import: router imports this module

    if np is None:
        return tuple(0.0 for _ in range(LENGTH))
    noise = generate_one_over_f_noise(LENGTH, alpha=PROFILES.get(profile, 1.0), seed=seed, sample_rate=1.0, low_frequency_hz=1.0 / LENGTH)
    return tuple(float(v) for v in noise.samples)


def signal(feature: str, step: int, profile: str = "pink", seed: Optional[int] = None) -> float:
    """The standardized wave sample for a feature at a step (reproducible; the seed defaults to the feature name)."""
    base_seed = int(seed) if seed is not None else zlib.crc32(feature.encode("utf-8")) & 0xFFFFFFFF
    series = _series(profile if profile in PROFILES else "pink", base_seed)
    return series[int(step) % len(series)]


def unit(feature: str, step: int, profile: str = "pink", seed: Optional[int] = None) -> float:
    """The same sample squashed into [0, 1] (0.5 at the mean, tails at the extremes)."""
    return 0.5 * (1.0 + math.tanh(signal(feature, step, profile, seed) / 2.0))


def settings_for(project_scope: str) -> ChaosSettings:
    from . import vault  # local import: vault is imported lazily so this module stays import-light

    return ChaosSettings.from_json(str(vault.setting_get(project_scope, SETTING_KEY, "") or ""))


def save_settings(project_scope: str, gain: float, profiles: Optional[Mapping[str, str]] = None) -> ChaosSettings:
    from . import vault

    settings = ChaosSettings(gain=gain, profiles=dict(profiles or {}))
    vault.setting_set(project_scope, SETTING_KEY, settings.to_json())
    return settings


class Chaos:
    """The active wave for one project scope: reads the scope's settings, advances one scoped step per use."""

    def __init__(self, project_scope: str, settings: Optional[ChaosSettings] = None, step_source=None) -> None:
        self.project_scope = (project_scope or "").strip() or "default"
        self.settings = settings or settings_for(self.project_scope)
        self._step_source = step_source

    @property
    def gain(self) -> float:
        return float(self.settings.gain)

    def profile(self, feature: str) -> str:
        return str(self.settings.profiles.get(feature, "pink"))

    def step(self, feature: str) -> int:
        """Advance and return the scoped counter for a feature: consecutive uses read consecutive samples."""
        if self._step_source is not None:
            return int(self._step_source(feature))
        from . import vault

        return int(vault.bump_counter(f"pinkwave:{self.project_scope}:{feature}"))

    def unit(self, feature: str, step: Optional[int] = None) -> float:
        return unit(feature, self.step(feature) if step is None else int(step), self.profile(feature))

    # ---- per-feature nudges, each bounded by the constants above ----

    def routing_jitter(self, endpoint_names: Iterable[str], step: Optional[int] = None) -> Dict[str, float]:
        """Per-endpoint additions to the entropy penalty in [0, gain * ROUTING_MAX_JITTER]; empty at gain 0."""
        names = list(endpoint_names)
        if self.gain <= 0.0 or not names:
            return {}
        base = self.step("routing") if step is None else int(step)
        profile = self.profile("routing")
        return {name: round(self.gain * ROUTING_MAX_JITTER * unit("routing", base + index * PHASE, profile), 6) for index, name in enumerate(names)}

    def heavy_schedule(self, base_temperature: float, step: Optional[int] = None) -> Optional[Tuple[float, float, float]]:
        """(draft, critique, synthesis) temperatures, or None at gain 0 so callers keep one temperature."""
        if self.gain <= 0.0:
            return None
        value = self.unit("heavy", step)
        draft = min(1.0, float(base_temperature) + self.gain * HEAVY_DRAFT_SPREAD * value)
        return (round(draft, 3), HEAVY_CRITIQUE_TEMPERATURE, float(base_temperature))

    def recall_share(self, step: Optional[int] = None) -> float:
        """Fraction of the prompt budget for long-distance recall: the base share plus a bounded wave term."""
        if self.gain <= 0.0:
            return RECALL_BASE_SHARE
        return round(RECALL_BASE_SHARE + self.gain * RECALL_SPREAD * self.unit("memory", step), 4)

    def digest_recall_chars(self, step: Optional[int] = None) -> int:
        if self.gain <= 0.0:
            return DIGEST_RECALL_BASE
        return int(DIGEST_RECALL_BASE + self.gain * DIGEST_RECALL_SPREAD * self.unit("migration", step))

    def preview(self) -> Dict[str, Dict[str, object]]:
        """Where each feature stands right now (does not advance the counters)."""
        from . import vault

        out: Dict[str, Dict[str, object]] = {}
        for feature in FEATURES:
            step = vault.peek_counter(f"pinkwave:{self.project_scope}:{feature}")
            out[feature] = {"profile": self.profile(feature), "step": step, "unit": round(unit(feature, step, self.profile(feature)), 3)}
        return out


def activate(project_scope: str, settings: Optional[ChaosSettings] = None) -> Chaos:
    """Make a scope's wave current for this thread of execution (the app before a send, the worker per job)."""
    chaos = Chaos(project_scope, settings)
    _ACTIVE.set(chaos)
    return chaos


def deactivate() -> None:
    _ACTIVE.set(None)


def current() -> Optional[Chaos]:
    """The active wave, or None (deterministic behaviour, as in tests and library use)."""
    return _ACTIVE.get()


def for_scope(project_scope: str) -> Chaos:
    """The active wave when it belongs to this scope, else a fresh one for the scope."""
    active = _ACTIVE.get()
    if active is not None and active.project_scope == ((project_scope or "").strip() or "default"):
        return active
    return Chaos(project_scope)

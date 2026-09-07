"""Task memory: rolling summary + step history persisted to disk.

Keeps long-running orchestration coherent without re-reading everything:
each completed step contributes a compact note, and the summary is refreshed
with the cheapest available provider.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import List


@dataclass
class StepRecord:
    step_id: int
    title: str
    task_type: str
    provider: str
    status: str  # pending | done | failed
    note: str = ""
    ts: float = field(default_factory=time.time)


class TaskMemory:
    """Append-only step log + rolling summary, stored as JSON."""

    def __init__(self, path: str, goal: str = ""):
        self.path = path
        self.goal = goal
        self.summary = ""
        self.steps: List[StepRecord] = []
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.goal = data.get("goal", self.goal)
                self.summary = data.get("summary", "")
                self.steps = [StepRecord(**s) for s in data.get("steps", [])]
            except (json.JSONDecodeError, TypeError):
                pass  # start fresh on corrupt memory

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "goal": self.goal,
                    "summary": self.summary,
                    "steps": [asdict(s) for s in self.steps],
                },
                f,
                indent=2,
            )
        os.replace(tmp, self.path)

    def add_step(self, step: StepRecord) -> None:
        with self._lock:
            self.steps = [s for s in self.steps if s.step_id != step.step_id]
            self.steps.append(step)
            self.steps.sort(key=lambda s: s.step_id)
        self.save()

    def context_block(self, last_n: int = 8) -> str:
        """Compact memory block injected into subsequent prompts."""
        lines = [f"GOAL: {self.goal}"]
        if self.summary:
            lines.append(f"SUMMARY SO FAR: {self.summary}")
        recent = self.steps[-last_n:]
        if recent:
            lines.append("RECENT STEPS:")
            lines += [
                f"  #{s.step_id} [{s.status}] ({s.provider}/{s.task_type}) {s.title}"
                + (f" — {s.note}" if s.note else "")
                for s in recent
            ]
        return "\n".join(lines)

    def refresh_summary(self, summarizer) -> None:
        """Re-summarize progress. `summarizer(text) -> str` is injected so the
        caller decides which (cheap, free) provider does it."""
        transcript = "\n".join(
            f"#{s.step_id} [{s.status}] {s.title}: {s.note}" for s in self.steps
        )
        try:
            self.summary = summarizer(
                f"Goal: {self.goal}\n\nStep log:\n{transcript}\n\n"
                "Write a 3-sentence status summary of progress and what remains."
            )[:1200]
        except Exception:
            pass  # memory refresh is best-effort; never break execution
        self.save()

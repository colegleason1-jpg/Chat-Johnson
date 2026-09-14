"""Deterministic spatial layout: the honest core of the "pink-wave / Higgs" concept.

A scene is a room and a set of boxes with size, mass, and an anchor. The solver snaps floor
objects to the ground, clamps everything inside the room, and pushes overlapping boxes apart
along their least-penetration axis, the lighter box moving more. No model call, no randomness:
the same spec always yields the same layout.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

MAX_OBJECTS = 60
ANCHORS = ("floor", "wall", "free")
_SCENE_BLOCK = re.compile(r"```scene\s*\n(?P<body>.*?)```", re.S | re.I)
EPS = 1e-6


class SceneError(ValueError):
    pass


@dataclass
class SceneObject:
    name: str
    size: Tuple[float, float, float]
    mass: float = 1.0
    anchor: str = "floor"
    position: Optional[Tuple[float, float, float]] = None


@dataclass
class SceneSpec:
    room: Tuple[float, float, float]
    objects: List[SceneObject] = field(default_factory=list)


@dataclass
class PlacedObject:
    name: str
    size: Tuple[float, float, float]
    mass: float
    anchor: str
    position: Tuple[float, float, float]
    moved: float


@dataclass
class PlacedScene:
    room: Tuple[float, float, float]
    objects: List[PlacedObject]
    report: Dict[str, Any]


def _triple(value: Any, what: str, positive: bool = True) -> Tuple[float, float, float]:
    if isinstance(value, Mapping):
        keys = ("width", "depth", "height") if what != "position" else ("x", "y", "z")
        try:
            value = [value[k] for k in keys]
        except KeyError as exc:
            raise SceneError(f"{what} needs {', '.join(keys)}") from exc
    try:
        floats = tuple(float(v) for v in value)
    except (TypeError, ValueError) as exc:
        raise SceneError(f"{what} must be three numbers") from exc
    if len(floats) != 3 or any(not np.isfinite(v) for v in floats):
        raise SceneError(f"{what} must be three finite numbers")
    if positive and any(v <= 0 for v in floats):
        raise SceneError(f"{what} must be positive")
    return floats  # type: ignore[return-value]


def parse_scene(data: Mapping[str, Any]) -> SceneSpec:
    if not isinstance(data, Mapping) or "room" not in data or "objects" not in data:
        raise SceneError("a scene needs 'room' and 'objects'")
    room = _triple(data["room"], "room")
    raw_objects = data["objects"]
    if not isinstance(raw_objects, Sequence) or isinstance(raw_objects, str):
        raise SceneError("'objects' must be a list")
    if len(raw_objects) > MAX_OBJECTS:
        raise SceneError(f"at most {MAX_OBJECTS} objects")
    objects: List[SceneObject] = []
    for index, raw in enumerate(raw_objects):
        if not isinstance(raw, Mapping) or "size" not in raw:
            raise SceneError(f"object {index + 1} needs a size")
        name = str(raw.get("name") or f"object {index + 1}").strip()[:60]
        size = _triple(raw["size"], f"size of {name}")
        if any(s > r for s, r in zip(size, room)):
            raise SceneError(f"{name} is larger than the room")
        try:
            mass = float(raw.get("mass", 1.0))
        except (TypeError, ValueError) as exc:
            raise SceneError(f"mass of {name} must be a number") from exc
        anchor = str(raw.get("anchor") or "floor").lower()
        if anchor not in ANCHORS:
            raise SceneError(f"anchor of {name} must be one of {', '.join(ANCHORS)}")
        position = _triple(raw["position"], "position", positive=False) if raw.get("position") is not None else None
        objects.append(SceneObject(name=name, size=size, mass=max(0.01, mass), anchor=anchor, position=position))
    return SceneSpec(room=room, objects=objects)


def parse_scene_block(text: str) -> Optional[SceneSpec]:
    """The first fenced ```scene JSON block in a text, or None when there is none."""
    match = _SCENE_BLOCK.search(text or "")
    if not match:
        return None
    try:
        data = json.loads(match.group("body"))
    except ValueError as exc:
        raise SceneError(f"the scene block is not valid JSON: {exc}") from exc
    return parse_scene(data)


def _initial_positions(spec: SceneSpec) -> np.ndarray:
    room = np.array(spec.room)
    count = len(spec.objects)
    columns = max(1, int(np.ceil(np.sqrt(count))))
    positions = np.zeros((count, 3))
    for index, obj in enumerate(spec.objects):
        if obj.position is not None:
            positions[index] = obj.position
        else:
            row, col = divmod(index, columns)
            positions[index] = (
                (col + 0.5) * room[0] / columns - obj.size[0] / 2,
                (row + 0.5) * room[1] / max(1, int(np.ceil(count / columns))) - obj.size[1] / 2,
                0.0,
            )
    return positions


def _overlap(pos: np.ndarray, size: np.ndarray, a: int, b: int) -> np.ndarray:
    """Per-axis penetration depth between two boxes (all > 0 means they intersect)."""
    lo = np.maximum(pos[a], pos[b])
    hi = np.minimum(pos[a] + size[a], pos[b] + size[b])
    return hi - lo


def count_overlaps(pos: np.ndarray, size: np.ndarray) -> int:
    total = 0
    for a in range(len(pos)):
        for b in range(a + 1, len(pos)):
            if bool(np.all(_overlap(pos, size, a, b) > EPS)):
                total += 1
    return total


def solve_layout(spec: SceneSpec, iterations: int = 200) -> PlacedScene:
    room = np.array(spec.room, dtype=float)
    size = np.array([o.size for o in spec.objects], dtype=float).reshape(-1, 3)
    mass = np.array([o.mass for o in spec.objects], dtype=float)
    start = _initial_positions(spec)
    pos = start.copy()
    anchors = [o.anchor for o in spec.objects]

    def constrain() -> None:
        for i, anchor in enumerate(anchors):
            if anchor == "floor":
                pos[i, 2] = 0.0
            elif anchor == "wall":
                # Snap to the nearest wall; keep the floor.
                pos[i, 2] = 0.0
                distances = {
                    "x0": pos[i, 0], "x1": room[0] - size[i, 0] - pos[i, 0],
                    "y0": pos[i, 1], "y1": room[1] - size[i, 1] - pos[i, 1],
                }
                nearest = min(distances, key=distances.get)
                if nearest == "x0":
                    pos[i, 0] = 0.0
                elif nearest == "x1":
                    pos[i, 0] = room[0] - size[i, 0]
                elif nearest == "y0":
                    pos[i, 1] = 0.0
                else:
                    pos[i, 1] = room[1] - size[i, 1]
            pos[i] = np.clip(pos[i], 0.0, room - size[i])

    constrain()
    overlaps_before = count_overlaps(pos, size)
    used = 0
    for used in range(1, int(iterations) + 1):
        moved_any = False
        for a in range(len(pos)):
            for b in range(a + 1, len(pos)):
                depth = _overlap(pos, size, a, b)
                if not bool(np.all(depth > EPS)):
                    continue
                axes = [0, 1] if anchors[a] != "free" or anchors[b] != "free" else [0, 1, 2]
                axis = min(axes, key=lambda k: depth[k])
                total = float(depth[axis]) + 1e-3
                share_a = mass[b] / (mass[a] + mass[b])  # the lighter box moves more
                center_a = pos[a, axis] + size[a, axis] / 2
                center_b = pos[b, axis] + size[b, axis] / 2
                direction = -1.0 if center_a <= center_b else 1.0
                pos[a, axis] += direction * total * share_a
                pos[b, axis] -= direction * total * (1 - share_a)
                moved_any = True
        constrain()
        if not moved_any:
            break
        if count_overlaps(pos, size) == 0:
            break
    overlaps_after = count_overlaps(pos, size)
    inside = bool(np.all(pos >= -EPS) and np.all(pos + size <= room + EPS))
    moved = np.linalg.norm(pos - start, axis=1) if len(pos) else np.zeros(0)
    placed = [
        PlacedObject(name=o.name, size=o.size, mass=o.mass, anchor=o.anchor, position=tuple(float(v) for v in pos[i]), moved=float(moved[i]))
        for i, o in enumerate(spec.objects)
    ]
    report = {
        "objects": len(placed), "overlaps_before": overlaps_before, "overlaps_after": overlaps_after, "iterations": used,
        "inside_room": inside, "moved": int(sum(1 for m in moved if m > 1e-3)), "resolved": overlaps_after == 0 and inside,
    }
    return PlacedScene(room=tuple(float(v) for v in room), objects=placed, report=report)


def scene_dict(placed: PlacedScene) -> Dict[str, Any]:
    """The JSON shape (positions and sizes rounded to a tenth of a millimetre for readability)."""
    objects = []
    for o in placed.objects:
        item = asdict(o)
        item["size"] = [round(float(v), 4) for v in o.size]
        item["position"] = [round(float(v), 4) for v in o.position]
        item["moved"] = round(float(o.moved), 4)
        objects.append(item)
    return {"room": list(placed.room), "objects": objects, "report": dict(placed.report)}


def scene_json(placed: PlacedScene) -> str:
    return json.dumps(scene_dict(placed), indent=2)


def scene_markdown(placed: PlacedScene) -> str:
    r = placed.report
    lines = [
        f"Resolved layout: {r['objects']} object(s), overlaps {r['overlaps_before']} → {r['overlaps_after']}, "
        f"{r['iterations']} iteration(s), {'all inside the room' if r['inside_room'] else 'some outside the room'}.",
        "",
        "| object | anchor | size (w×d×h) | position (x, y, z) | mass | moved |",
        "|---|---|---|---|---|---|",
    ]
    for o in placed.objects:
        lines.append(f"| {o.name} | {o.anchor} | {o.size[0]:g}×{o.size[1]:g}×{o.size[2]:g} | {o.position[0]:g}, {o.position[1]:g}, {o.position[2]:g} | {o.mass:g} | {o.moved:g} |")
    return "\n".join(lines)

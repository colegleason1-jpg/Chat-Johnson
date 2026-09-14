---
name: spatial-layout
description: The scene block a spatial mission needs so the solver can resolve the layout.
keywords: room layout floor plan furniture arrange scene 3d spatial warehouse stage set placement objects
---
- When asked for a room, floor plan, stage, warehouse, or any arrangement of objects, answer with exactly one fenced block tagged `scene` containing JSON: `{"room": {"width": 8, "depth": 6, "height": 3}, "objects": [{"name": "desk", "size": [1.6, 0.8, 0.75], "mass": 40, "anchor": "wall"}, ...]}`.
- Units are metres and kilograms; sizes are width × depth × height; every object must fit inside the room; at most 60 objects.
- `anchor` is `floor` (rests on the ground), `wall` (against the nearest wall), or `free` (may float, e.g. a lamp on a shelf). Add `"position": [x, y, z]` only when the brief fixes a spot; the solver resolves collisions and keeps everything inside.
- The deterministic solver, not the model, decides final positions; describe intent and constraints in prose before the block, never invent coordinates for everything.

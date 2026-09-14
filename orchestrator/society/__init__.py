"""Agent society (Plato's Republic) feeding companies run on Traction/EOS, on the job runner."""
from . import academy as _academy  # noqa: F401  (registers the academy cycle)
from . import cycles as _cycles  # noqa: F401  (registers the company cycle)
from . import tick as _tick  # noqa: F401  (registers the society tick)
from .academy import KIND_ACADEMY, run_now as run_academy_now
from .cycles import KIND_COMPANY, run_now
from .release import publish_work, record_board_feedback, request_final_edit
from .store import SOCIETY_SCHEMA_SQL
from .templates import DEFAULT_PRODUCTS, add_product, seed_academy, seed_company
from .tick import KIND_TICK, start_tick, stop_tick, tick_state

__all__ = [
    "KIND_ACADEMY", "KIND_COMPANY", "KIND_TICK", "run_academy_now", "run_now", "start_tick", "stop_tick", "tick_state",
    "publish_work", "record_board_feedback", "request_final_edit", "SOCIETY_SCHEMA_SQL", "DEFAULT_PRODUCTS", "add_product",
    "seed_academy", "seed_company",
]

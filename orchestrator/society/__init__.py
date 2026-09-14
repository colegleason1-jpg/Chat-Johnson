"""Agent society (Plato's Republic) feeding companies run on Traction/EOS, on the job runner."""
from . import academy as _academy  # noqa: F401  (registers the academy cycle)
from . import cycles as _cycles  # noqa: F401  (registers the company cycle)
from .academy import KIND_ACADEMY, run_now as run_academy_now
from .cycles import KIND_COMPANY, run_now
from .store import SOCIETY_SCHEMA_SQL
from .templates import DEFAULT_PRODUCTS, add_product, seed_academy, seed_company

__all__ = ["KIND_ACADEMY", "KIND_COMPANY", "run_academy_now", "run_now", "SOCIETY_SCHEMA_SQL", "DEFAULT_PRODUCTS", "add_product", "seed_academy", "seed_company"]

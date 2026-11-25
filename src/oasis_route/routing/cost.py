"""Cost estimation from model pricing, honest about what is known.

Prices are USD per million tokens. A result is one of:

- ``free``: the provider's own catalogue (or an operator) prices the model at zero, or it runs locally;
- ``priced``: a per-token price is known; ``verified`` says whether it came from the provider/operator
  (``True``) or from an imported research snapshot (``False``, an estimate);
- ``unknown``: no price is known. Unknown is never treated as zero.

Amounts are integer USD micros (1e-6 USD) so reservations and settlements add up exactly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

MICROS_PER_USD = 1_000_000


@dataclass(frozen=True)
class CostEstimate:
    status: str  # free | priced | unknown
    usd_micros: int | None
    verified: bool
    source: str | None
    prompt_usd_per_mtok: float | None = None
    completion_usd_per_mtok: float | None = None

    @property
    def usd(self) -> float | None:
        return None if self.usd_micros is None else self.usd_micros / MICROS_PER_USD

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "usd": self.usd,
            "usd_micros": self.usd_micros,
            "verified": self.verified,
            "source": self.source,
            "prompt_usd_per_mtok": self.prompt_usd_per_mtok,
            "completion_usd_per_mtok": self.completion_usd_per_mtok,
        }


def _price(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    return float(v) if v >= 0 and math.isfinite(v) else None


def unit_prices(pricing: dict[str, Any] | None) -> tuple[float | None, float | None]:
    p = pricing or {}
    return _price(p.get("prompt_usd_per_mtok")), _price(p.get("completion_usd_per_mtok"))


def estimate_cost(
    pricing: dict[str, Any] | None,
    input_tokens: int,
    output_tokens: int,
    *,
    local: bool = False,
) -> CostEstimate:
    """Cost of ``input_tokens`` + ``output_tokens``, rounded **up** to the next micro-dollar."""
    p = pricing or {}
    if local:
        return CostEstimate("free", 0, True, "local")
    if p.get("free") is True:
        return CostEstimate("free", 0, True, str(p.get("source") or "provider_model_list"))
    inp, out = unit_prices(p)
    if inp is None or out is None:
        return CostEstimate("unknown", None, False, p.get("source"), inp, out)
    micros = math.ceil(
        (max(0, input_tokens) * inp + max(0, output_tokens) * out) * MICROS_PER_USD / 1_000_000
    )
    verified = p.get("verified") is not False and p.get("source") != "omniroute_catalog"
    return CostEstimate("priced", micros, verified, p.get("source"), inp, out)

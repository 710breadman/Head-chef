from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any


@dataclass(slots=True)
class BudgetResult:
    estimated_input_tokens: int
    usable_input_tokens: int
    reserved_output_tokens: int
    safety_margin_tokens: int
    utilization: float
    status: str
    recommended_chunks: int
    explanation: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def estimate_tokens(text: str) -> int:
    """Conservative language-agnostic estimate for routing, not billing."""
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


def evaluate_budget(
    text: str,
    context_tokens: int,
    reserved_output_tokens: int = 2048,
    safety_margin_tokens: int = 1024,
) -> BudgetResult:
    estimated = estimate_tokens(text)
    usable = max(1, context_tokens - reserved_output_tokens - safety_margin_tokens)
    utilization = estimated / usable
    chunks = max(1, math.ceil(estimated / max(1, int(usable * 0.70))))

    if utilization <= 0.60:
        status = "safe"
        explanation = "The estimated input fits comfortably inside the usable context budget."
    elif utilization <= 0.85:
        status = "warning"
        explanation = "The task may fit, but leaves limited room for instructions, retrieved context, and correction."
    else:
        status = "split"
        explanation = "Split the work into smaller job cards before dispatching it to this model."

    return BudgetResult(
        estimated_input_tokens=estimated,
        usable_input_tokens=usable,
        reserved_output_tokens=reserved_output_tokens,
        safety_margin_tokens=safety_margin_tokens,
        utilization=round(utilization, 4),
        status=status,
        recommended_chunks=chunks,
        explanation=explanation,
    )

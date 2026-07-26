from __future__ import annotations


MANDATORY_REVIEW_CATEGORIES = {"coding", "planning"}
HIGH_RISK_SIGNALS = {
    "architecture", "migration", "security", "destructive", "delete", "public api",
    "public interface", "dependency", "license", "stored data",
}


def coordinator_review_required(task: str, category: str, *, requires_split: bool, confidence: float) -> bool:
    lowered = task.lower()
    return (
        category in MANDATORY_REVIEW_CATEGORIES
        or requires_split
        or confidence < 0.70
        or any(signal in lowered for signal in HIGH_RISK_SIGNALS)
    )

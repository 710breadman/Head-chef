from __future__ import annotations

from typing import Any

from .models import ModelProfile
from .router import RouteRequest, route
from .visual import GENERATION_STATION_OUTPUT_TYPES, select_approved_template


STATIONS = {
    "coding": "Fix a Python bug and propose tests",
    "vision": "Inspect an image for visual defects",
    "embedding": "Create embeddings for retrieval",
    "planning": "Decompose a bounded implementation into safe steps",
    "writing": "Rewrite prose clearly and concisely",
    "retrieval": "Retrieve an exact fact from supplied context",
    "analysis": "Analyze a bounded technical question",
}


def build_kitchen(
    profiles: list[ModelProfile],
    *,
    benchmark_path=None,
    outcome_path=None,
) -> dict[str, Any]:
    stations: dict[str, Any] = {}
    for category, task in STATIONS.items():
        decision = route(
            RouteRequest(task=task, required_capability=category),
            profiles,
            benchmark_path=benchmark_path,
            outcome_path=outcome_path,
        )
        stations[category] = {
            "model": decision.selected_model,
            "confidence": decision.confidence,
            "review_required": decision.coordinator_review_required,
            "reason": decision.explanation,
            "alternates": [
                {"model": item.model, "score": item.score, "reasons": item.reasons}
                for item in decision.candidates
                if not item.rejected
            ][:3],
            "backend": "ollama",
        }
    for station, output_type in GENERATION_STATION_OUTPUT_TYPES.items():
        template_id, _spec = select_approved_template(output_type)
        stations[station] = {
            "model": template_id,
            "confidence": 0.9 if template_id else 0.0,
            "review_required": True,
            "reason": (
                f"Approved ComfyUI template '{template_id}' available for {output_type} generation."
                if template_id else f"No approved {output_type} generation template installed."
            ),
            "alternates": [],
            "backend": "comfyui",
        }
    return {"mode": "local-only", "stations": stations}

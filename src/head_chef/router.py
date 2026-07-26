from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Any, Iterable

from .models import ModelProfile
from .token_governor import BudgetResult, evaluate_budget
from .policy import coordinator_review_required


TASK_KEYWORDS: dict[str, tuple[str, ...]] = {
    "vision": ("image", "screenshot", "photo", "ocr", "visual", "panel", "diagram"),
    "coding": ("code", "bug", "function", "class", "test", "refactor", "script", "python", "powershell"),
    "planning": ("roadmap", "sprint", "architecture", "plan", "design", "decompose", "requirements"),
    "embedding": ("embed", "embedding", "vectorize", "vector representation"),
    "retrieval": ("search", "retrieve", "rank", "match", "similarity", "index"),
    "writing": ("rewrite", "story", "draft", "prose", "chapter", "edit", "tone"),
}


@dataclass(slots=True)
class RouteRequest:
    task: str
    context_text: str = ""
    required_capability: str | None = None
    manual_model: str | None = None
    prefer_quality: bool = True
    modalities: list[str] = field(default_factory=lambda: ["text"])
    complexity: str = "medium"
    risk: str = "low"
    output_format: str = "json"
    allow_cloud: bool = False


@dataclass(slots=True)
class CandidateScore:
    model: str
    score: float
    reasons: list[str] = field(default_factory=list)
    rejected: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RouteDecision:
    category: str
    selected_model: str | None
    confidence: float
    candidates: list[CandidateScore]
    budget: BudgetResult | None
    requires_split: bool
    coordinator_review_required: bool
    explanation: str
    schema_version: str = "2.0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "selected_model": self.selected_model,
            "confidence": self.confidence,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "budget": self.budget.to_dict() if self.budget else None,
            "requires_split": self.requires_split,
            "coordinator_review_required": self.coordinator_review_required,
            "explanation": self.explanation,
        }


def classify_task(task: str, forced: str | None = None) -> str:
    if forced:
        return forced
    lowered = task.lower()
    scores = {
        category: sum(1 for keyword in keywords if keyword in lowered)
        for category, keywords in TASK_KEYWORDS.items()
    }
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "analysis"


def _load_benchmark_scores(path: Path | None, category: str) -> dict[tuple[str, str], tuple[float, float]]:
    if not path or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    scores: dict[tuple[str, str], tuple[float, float]] = {}
    current_suite = data.get("benchmark")
    for item in data.get("results", []):
        if not isinstance(item, dict):
            continue
        model = item.get("model")
        digest = item.get("model_digest")
        score = item.get("score")
        if (
            item.get("category") == category
            and item.get("ok") is True
            and item.get("suite_version") == current_suite
            and isinstance(model, str)
            and isinstance(digest, str)
            and digest
            and isinstance(score, (int, float))
        ):
            scores[(model, digest)] = (float(score), float(item.get("elapsed_seconds") or 0))
    return scores


def _load_benchmark_failures(path: Path | None, category: str) -> set[tuple[str, str]]:
    if not path or not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    current_suite = data.get("benchmark")
    return {
        (item["model"], item["model_digest"])
        for item in data.get("results", [])
        if isinstance(item, dict)
        and item.get("category") == category
        and item.get("suite_version") == current_suite
        and item.get("ok") is False
        and item.get("skipped") is not True
        and isinstance(item.get("model"), str)
        and isinstance(item.get("model_digest"), str)
        and item.get("model_digest")
    }


def _load_outcomes(path: Path | None, category: str) -> dict[tuple[str, str], tuple[float, float]]:
    if not path or not path.exists():
        return {}
    grouped: dict[tuple[str, str], list[tuple[bool, float]]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            item.get("category") != category
            or not isinstance(item.get("model"), str)
            or not isinstance(item.get("model_digest"), str)
            or not item.get("model_digest")
        ):
            continue
        grouped.setdefault((item["model"], item["model_digest"]), []).append(
            (bool(item.get("ok")), float(item.get("elapsed_seconds") or 0))
        )
    return {
        identity: (
            sum(1 for ok, _ in values if ok) / len(values),
            sum(seconds for _, seconds in values) / len(values),
        )
        for identity, values in grouped.items()
    }


def route(
    request: RouteRequest,
    profiles: Iterable[ModelProfile],
    *,
    reserved_output_tokens: int = 2048,
    safety_margin_tokens: int = 1024,
    benchmark_path: Path | None = None,
    outcome_path: Path | None = None,
) -> RouteDecision:
    profiles = list(profiles)
    category = classify_task(request.task, request.required_capability)
    benchmark_scores = _load_benchmark_scores(benchmark_path, category)
    benchmark_failures = _load_benchmark_failures(benchmark_path, category)
    outcomes = _load_outcomes(outcome_path, category)

    if request.manual_model:
        selected = next((p for p in profiles if p.name == request.manual_model), None)
        if selected is None:
            return RouteDecision(
                category=category,
                selected_model=None,
                confidence=0.0,
                candidates=[],
                budget=None,
                requires_split=False,
                coordinator_review_required=True,
                explanation=f"Manual model '{request.manual_model}' is not installed.",
            )
        if selected.is_cloud and not request.allow_cloud:
            return RouteDecision(
                category=category, selected_model=None, confidence=0.0, candidates=[], budget=None,
                requires_split=False, coordinator_review_required=True,
                explanation=f"Manual model '{selected.name}' is cloud-backed; local-only routing rejected it.",
            )
        if category not in selected.capabilities:
            return RouteDecision(
                category=category, selected_model=None, confidence=0.0, candidates=[],
                budget=None, requires_split=False, coordinator_review_required=True,
                explanation=f"Manual model '{selected.name}' does not advertise {category}; strict specialist routing rejected it.",
            )
        if selected.is_embedding_only and category != "embedding":
            return RouteDecision(
                category=category,
                selected_model=None,
                confidence=0.0,
                candidates=[],
                budget=None,
                requires_split=False,
                coordinator_review_required=True,
                explanation=f"Manual model '{selected.name}' is embedding-only and cannot perform this generative task.",
            )
        budget = evaluate_budget(
            request.context_text or request.task,
            selected.context_tokens,
            reserved_output_tokens,
            safety_margin_tokens,
        )
        return RouteDecision(
            category=category,
            selected_model=selected.name,
            confidence=1.0,
            candidates=[CandidateScore(selected.name, 100.0, ["manual override"])],
            budget=budget,
            requires_split=budget.status == "split",
            coordinator_review_required=coordinator_review_required(
                request.task, category, requires_split=budget.status == "split", confidence=1.0
            ),
            explanation="Manual override changed model choice only. Capability, budget, and review policy still apply.",
        )

    candidates: list[CandidateScore] = []
    profile_by_name = {profile.name: profile for profile in profiles}
    for profile in profiles:
        reasons: list[str] = []
        score = 0.0
        benchmark_evidence = benchmark_scores.get((profile.name, profile.digest))
        if (profile.name, profile.digest) in benchmark_failures:
            candidates.append(CandidateScore(
                profile.name, -100.0,
                [f"failed current {category} strength evaluation"],
                True,
            ))
            continue
        if profile.is_cloud and not request.allow_cloud:
            candidates.append(CandidateScore(profile.name, -100.0, ["cloud-backed model excluded"], True))
            continue

        if profile.is_embedding_only and category != "embedding":
            candidates.append(CandidateScore(profile.name, -100.0, ["embedding-only model"], True))
            continue
        if category == "embedding" and "embedding" not in profile.capabilities:
            candidates.append(CandidateScore(profile.name, -100.0, ["not an embedding model"], True))
            continue

        if category in profile.capabilities:
            score += 45
            reasons.append(f"supports {category}")
        elif category == "analysis" and "planning" in profile.capabilities:
            score += 22
            reasons.append("general reasoning fallback")
        else:
            score -= 22
            reasons.append(f"no strong {category} signal")

        if request.prefer_quality and benchmark_evidence is None:
            quality_bonus = min(8.0, profile.parameter_billions / 4)
            score += quality_bonus
            if quality_bonus:
                reasons.append(f"quality-size bonus +{quality_bonus:.1f}")
        else:
            speed_bonus = max(0.0, 18.0 - min(18.0, profile.parameter_billions))
            score += speed_bonus
            reasons.append(f"speed-size bonus +{speed_bonus:.1f}")

        if category == "coding" and "coding-specialist" in profile.notes:
            score += 25
            reasons.append("coding specialist")
        if category == "vision" and "vision" in profile.capabilities:
            score += 30
            reasons.append("vision model")
            if "vision-capable-name" in profile.notes:
                score += 25
                reasons.append("vision specialist")
        if category == "writing" and "writing-specialist" in profile.notes:
            score += 25
            reasons.append("writing specialist")
        if (
            benchmark_evidence is None
            and category in {"analysis", "planning", "writing"}
            and profile.parameter_billions >= 20
        ):
            score += 8
            reasons.append("large general model")
        if profile.context_tokens >= 32768:
            score += 6
            reasons.append("large context metadata")

        profile_benchmark = profile.benchmark_scores.get(category)
        if benchmark_evidence is not None:
            benchmark, elapsed_seconds = benchmark_evidence
            bonus = max(-20.0, min(20.0, benchmark - 80.0))
            latency_penalty = min(12.0, elapsed_seconds / 2)
            score += bonus - latency_penalty
            reasons.append(f"local strength score adjustment {bonus:+.1f}")
            reasons.append(f"strength latency penalty -{latency_penalty:.1f}")
        elif profile_benchmark is not None:
            bonus = max(-20.0, min(20.0, profile_benchmark - 80.0))
            score += bonus
            reasons.append(f"local strength score adjustment {bonus:+.1f}")
        reliability_bonus = (profile.reliability - 0.5) * 20
        score += reliability_bonus
        if reliability_bonus:
            reasons.append(f"reliability adjustment {reliability_bonus:+.1f}")
        identity = (profile.name, profile.digest)
        if identity in outcomes:
            success_rate, average_seconds = outcomes[identity]
            outcome_bonus = (success_rate - 0.5) * 30
            latency_penalty = min(20.0, average_seconds / 10)
            score += outcome_bonus - latency_penalty
            reasons.append(f"observed success {success_rate:.0%}")
            reasons.append(f"observed latency penalty -{latency_penalty:.1f}")

        candidates.append(CandidateScore(profile.name, round(score, 2), reasons, False))

    candidates.sort(key=lambda item: item.score, reverse=True)
    viable = [candidate for candidate in candidates if not candidate.rejected]
    if not viable:
        return RouteDecision(
            category=category,
            selected_model=None,
            confidence=0.0,
            candidates=candidates,
            budget=None,
            requires_split=False,
            coordinator_review_required=True,
            explanation="No installed local model is suitable. Keep this task with Codex/OpenAI or install an appropriate model manually.",
        )

    winner = viable[0]
    selected_profile = profile_by_name[winner.model]
    runner_up = viable[1].score if len(viable) > 1 else winner.score - 20
    margin = max(0.0, winner.score - runner_up)
    confidence = round(min(0.98, 0.55 + margin / 80.0), 2)
    budget = evaluate_budget(
        request.context_text or request.task,
        selected_profile.context_tokens,
        reserved_output_tokens,
        safety_margin_tokens,
    )
    requires_split = budget.status == "split"
    coordinator_review = coordinator_review_required(
        request.task, category, requires_split=requires_split, confidence=confidence
    )

    explanation = (
        f"Selected {winner.model} for {category}. "
        f"The decision is based on installed-model metadata, task signals, model size, context metadata, "
        f"and any saved local benchmark result."
    )
    return RouteDecision(
        category=category,
        selected_model=winner.model,
        confidence=confidence,
        candidates=candidates,
        budget=budget,
        requires_split=requires_split,
        coordinator_review_required=coordinator_review,
        explanation=explanation,
    )

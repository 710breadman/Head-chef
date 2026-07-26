from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any

from .models import ModelProfile
from .ollama import OllamaClient, OllamaError
from .storage import atomic_write_json, utc_now


SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "risks": {"type": "array", "items": {"type": "string"}},
        "needs_review": {"type": "boolean"},
    },
    "required": ["summary", "risks", "needs_review"],
}

SUITE_VERSION = "hc-bench-v2.0"
CASES = {
    "coding": "Find bug: def first(items): return items[1]. Explain risk.",
    "planning": "Plan three safe steps to add config validation. Name rollback.",
    "writing": "Rewrite concisely: Due to the fact that validation is absent, failures may occur.",
    "requirements": "Extract requirements: input must be UTF-8, max 1 MB, reject traversal.",
    "hallucination": "You cannot run tools. State whether tests were run and list risks.",
}

CASE_CAPABILITY = {
    "coding": "coding",
    "planning": "planning",
    "writing": "writing",
    "requirements": "analysis",
    "hallucination": "analysis",
}


def benchmark_model(client: OllamaClient, profile: ModelProfile, timeout_seconds: int, category: str = "coding") -> dict[str, Any]:
    started = time.perf_counter()
    try:
        response = client.chat(
            profile.name,
            [
                {"role": "system", "content": "Return only data matching the requested JSON schema."},
                {"role": "user", "content": CASES[category]},
            ],
            format_schema=SCHEMA,
            options={"temperature": 0, "num_predict": 256},
            timeout_seconds=timeout_seconds,
        )
        elapsed = time.perf_counter() - started
        try:
            parsed = json.loads(response.content)
            schema_ok = (
                isinstance(parsed, dict)
                and isinstance(parsed.get("summary"), str)
                and isinstance(parsed.get("risks"), list)
                and isinstance(parsed.get("needs_review"), bool)
            )
        except json.JSONDecodeError:
            schema_ok = False

        eval_count = int(response.raw.get("eval_count") or 0)
        eval_duration = int(response.raw.get("eval_duration") or 0)
        tokens_per_second = (eval_count / (eval_duration / 1_000_000_000)) if eval_duration > 0 else 0.0
        score = 70.0 if schema_ok else 25.0
        if elapsed <= 30:
            score += 15
        elif elapsed <= 90:
            score += 8
        if tokens_per_second >= 10:
            score += 10
        elif tokens_per_second >= 3:
            score += 5
        score = min(100.0, score)

        return {
            "model": profile.name,
            "model_digest": profile.digest,
            "category": category,
            "ok": True,
            "schema_ok": schema_ok,
            "elapsed_seconds": round(elapsed, 3),
            "tokens_per_second": round(tokens_per_second, 3),
            "prompt_eval_count": response.raw.get("prompt_eval_count"),
            "eval_count": response.raw.get("eval_count"),
            "score": score,
            "error": None,
        }
    except OllamaError as exc:
        return {
            "model": profile.name,
            "model_digest": profile.digest,
            "category": category,
            "ok": False,
            "schema_ok": False,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "tokens_per_second": 0.0,
            "prompt_eval_count": None,
            "eval_count": None,
            "score": 0.0,
            "error": str(exc),
        }


def run_benchmarks(
    client: OllamaClient,
    profiles: list[ModelProfile],
    output_path: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    results = [
        benchmark_model(client, profile, timeout_seconds, category)
        for profile in profiles
        for category in CASES
        if CASE_CAPABILITY[category] in profile.capabilities
    ]
    payload = {
        "created_at": utc_now(),
        "schema_version": "2.0",
        "benchmark": SUITE_VERSION,
        "warning": "Task-specific local evidence only; not a universal model-quality leaderboard.",
        "results": results,
    }
    atomic_write_json(output_path, payload)
    return payload

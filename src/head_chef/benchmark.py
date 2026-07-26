from __future__ import annotations

import json
import hashlib
from pathlib import Path
import secrets
import time
from typing import Any

from .models import ModelProfile
from .ollama import OllamaClient, OllamaError
from .storage import atomic_create_json, atomic_write_json, compact_timestamp, utc_now


SUITE_VERSION = "hc-strengths-v3.2"
SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "maxLength": 1200},
        "risks": {"type": "array", "items": {"type": "string"}},
        "needs_review": {"type": "boolean"},
    },
    "required": ["answer", "risks", "needs_review"],
}

STRENGTH_CASES: dict[str, dict[str, Any]] = {
    "coding": {
        "prompt": "Find the indexing bug in: def first(items): return items[1]. State the failing inputs.",
        "expected": (("items[1]", "second element"), ("fewer than two", "empty", "one element"), ("index", "indexerror")),
    },
    "planning": {
        "prompt": "Give exactly three numbered one-sentence steps to add JSON config validation. Include testing and rollback. Maximum 120 words.",
        "expected": (("validat",), ("test",), ("rollback", "revert")),
    },
    "writing": {
        "prompt": "Rewrite concisely: Due to the fact that validation is absent, failures may occur.",
        "expected": (("validation",), ("failure", "fail")),
    },
    "retrieval": {
        "prompt": "Retrieve the exact port from this supplied fact: Ollama uses port 11434.",
        "expected": (("11434",),),
    },
    "analysis": {
        "prompt": "Explain why immutable run records improve auditability.",
        "expected": (("audit",), ("history", "record", "trace"), ("evidence", "integrity")),
    },
    "vision": {
        "prompt": "Identify the shapes and colors. Expected fixture has a red circle, blue square, green triangle.",
        "expected": (("red",), ("circle",), ("blue",), ("square",), ("green",), ("triangle",)),
    },
    "embedding": {
        "prompt": "Head Chef local kitchen routing",
        "expected": (),
    },
}


def _chat_score(
    content: str,
    expected: tuple[tuple[str, ...], ...],
    elapsed: float,
    raw: dict[str, Any],
    category: str,
) -> tuple[float, bool, bool]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return 0.0, False, False
    schema_ok = (
        isinstance(parsed, dict)
        and isinstance(parsed.get("answer"), str)
        and isinstance(parsed.get("risks"), list)
        and isinstance(parsed.get("needs_review"), bool)
    )
    searchable = content.casefold()
    expected_hits = sum(1 for group in expected if any(token in searchable for token in group))
    task_ok = expected_hits >= max(1, (len(expected) + 1) // 2)
    if category == "planning" and schema_ok:
        answer = parsed["answer"]
        numbered_steps = sum(1 for number in ("1.", "2.", "3.", "4.") if number in answer)
        task_ok = task_ok and numbered_steps == 3 and len(answer.split()) <= 120
    score = (45 if schema_ok else 0) + (35 * expected_hits / max(1, len(expected)))
    score += 12 if elapsed <= 30 else 6 if elapsed <= 90 else 0
    eval_count = int(raw.get("eval_count") or 0)
    eval_duration = int(raw.get("eval_duration") or 0)
    tokens_per_second = eval_count / (eval_duration / 1_000_000_000) if eval_duration else 0
    score += 8 if tokens_per_second >= 10 else 4 if tokens_per_second >= 3 else 0
    return round(min(100.0, score), 2), schema_ok, task_ok


def benchmark_model(
    client: OllamaClient,
    profile: ModelProfile,
    timeout_seconds: int,
    category: str,
    *,
    vision_image: Path | None = None,
) -> dict[str, Any]:
    case = STRENGTH_CASES[category]
    started = time.perf_counter()
    base = {
        "model": profile.name,
        "model_digest": profile.digest,
        "category": category,
        "suite_version": SUITE_VERSION,
    }
    try:
        if category == "embedding":
            response = client.embed(profile.name, [case["prompt"]], timeout_seconds=timeout_seconds)
            elapsed = time.perf_counter() - started
            embeddings = response.raw.get("embeddings", [])
            valid = (
                isinstance(embeddings, list)
                and len(embeddings) == 1
                and isinstance(embeddings[0], list)
                and bool(embeddings[0])
                and all(isinstance(value, (int, float)) for value in embeddings[0])
            )
            return {
                **base, "ok": valid, "schema_ok": valid, "task_ok": valid,
                "elapsed_seconds": round(elapsed, 3),
                "dimensions": len(embeddings[0]) if valid else 0,
                "tokens_per_second": 0.0, "score": 100.0 if valid else 0.0, "error": None,
            }

        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": "Return only requested JSON. Put complete concrete task output in answer, never a preamble or promise.",
            },
            {"role": "user", "content": case["prompt"]},
        ]
        if category == "vision":
            if vision_image is None:
                return {**base, "ok": False, "skipped": True, "score": 0.0, "error": "vision image required"}
            messages[1]["images"] = [vision_image.read_bytes()]
        response = client.chat(
            profile.name,
            messages,
            format_schema=SCHEMA,
            options={"temperature": 0, "num_predict": 384},
            think=False,
            timeout_seconds=timeout_seconds,
        )
        elapsed = time.perf_counter() - started
        score, schema_ok, task_ok = _chat_score(response.content, case["expected"], elapsed, response.raw, category)
        eval_count = int(response.raw.get("eval_count") or 0)
        eval_duration = int(response.raw.get("eval_duration") or 0)
        tokens_per_second = eval_count / (eval_duration / 1_000_000_000) if eval_duration else 0.0
        return {
            **base, "ok": schema_ok and task_ok, "schema_ok": schema_ok, "task_ok": task_ok,
            "elapsed_seconds": round(elapsed, 3), "tokens_per_second": round(tokens_per_second, 3),
            "prompt_eval_count": response.raw.get("prompt_eval_count"), "eval_count": response.raw.get("eval_count"),
            "score": score, "error": None,
            "response_sha256": hashlib.sha256(response.content.encode("utf-8")).hexdigest(),
            "response": response.content,
        }
    except (OllamaError, OSError) as exc:
        return {
            **base, "ok": False, "schema_ok": False, "task_ok": False,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "tokens_per_second": 0.0, "score": 0.0, "error": str(exc),
        }


def run_benchmarks(
    client: OllamaClient,
    profiles: list[ModelProfile],
    output_path: Path,
    timeout_seconds: int,
    *,
    categories: set[str] | None = None,
    vision_image: Path | None = None,
    assignments: dict[str, set[str]] | None = None,
) -> dict[str, Any]:
    requested = categories or set(STRENGTH_CASES)
    prior_results: list[dict[str, Any]] = []
    if output_path.exists():
        try:
            prior = json.loads(output_path.read_text(encoding="utf-8"))
            if isinstance(prior.get("results"), list):
                prior_results = [item for item in prior["results"] if isinstance(item, dict)]
        except (OSError, json.JSONDecodeError):
            prior_results = []
    cases = [
        (profile, category)
        for profile in profiles
        for category in STRENGTH_CASES
        if category in requested and category in profile.capabilities
        and (assignments is None or category in assignments.get(profile.name, set()))
    ]
    results: list[dict[str, Any]] = []
    for profile, category in cases:
        result = benchmark_model(
            client, profile, timeout_seconds, category, vision_image=vision_image,
        )
        results.append(result)
        checkpoint_keys = {
            (item.get("model"), item.get("model_digest"), item.get("category"))
            for item in results
        }
        checkpoint_results = [
            item for item in prior_results
            if (item.get("model"), item.get("model_digest"), item.get("category")) not in checkpoint_keys
        ] + results
        atomic_write_json(output_path, {
            "created_at": utc_now(),
            "schema_version": "3.0",
            "benchmark": SUITE_VERSION,
            "warning": "Strength-specific local evidence only; never a universal leaderboard.",
            "results": checkpoint_results,
            "current_result_count": len(results),
            "in_progress": len(results) < len(cases),
            "completed_case_count": len(results),
            "total_case_count": len(cases),
        })
    current_keys = {
        (item.get("model"), item.get("model_digest"), item.get("category"))
        for item in results
    }
    merged_results = [
        item for item in prior_results
        if (item.get("model"), item.get("model_digest"), item.get("category")) not in current_keys
    ] + results
    payload = {
        "created_at": utc_now(),
        "schema_version": "3.0",
        "benchmark": SUITE_VERSION,
        "warning": "Strength-specific local evidence only; never a universal leaderboard.",
        "results": merged_results,
        "current_result_count": len(results),
        "in_progress": False,
        "completed_case_count": len(results),
        "total_case_count": len(cases),
    }
    snapshot = output_path.parent / "runs" / f"{compact_timestamp()}-{secrets.token_hex(4)}.json"
    payload["artifact_path"] = str(snapshot)
    atomic_create_json(snapshot, payload)
    atomic_write_json(output_path, payload)
    return payload

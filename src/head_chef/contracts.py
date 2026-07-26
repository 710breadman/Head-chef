from __future__ import annotations

from typing import Any


SCHEMA_VERSION = "2.0"
CONTRACT_VERSION = "head-chef.v2"

WORKER_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "result": {"type": "string"},
        "changed_files": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "tests_recommended": {"type": "array", "items": {"type": "string"}},
        "acceptance_check": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "criterion": {"type": "string"},
                    "met": {"type": "boolean"},
                    "evidence": {"type": "string"},
                },
                "required": ["criterion", "met", "evidence"],
            },
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "blockers": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "result",
        "changed_files",
        "assumptions",
        "risks",
        "tests_recommended",
        "acceptance_check",
        "confidence",
        "blockers",
    ],
}

REVIEW_STATUSES = {"accepted", "partially_useful", "rejected", "unsafe", "timed_out", "needs_revision"}


def validate_worker_output(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["output must be a JSON object"]
    errors: list[str] = []
    string_fields = ("result",)
    list_fields = ("changed_files", "assumptions", "risks", "tests_recommended", "acceptance_check", "blockers")
    for field in string_fields:
        if not isinstance(value.get(field), str):
            errors.append(f"{field} must be a string")
    for field in list_fields:
        if not isinstance(value.get(field), list):
            errors.append(f"{field} must be an array")
    confidence = value.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
        errors.append("confidence must be a number from 0 to 1")
    for index, check in enumerate(value.get("acceptance_check", []) if isinstance(value.get("acceptance_check"), list) else []):
        if not isinstance(check, dict):
            errors.append(f"acceptance_check[{index}] must be an object")
            continue
        if not isinstance(check.get("criterion"), str) or not isinstance(check.get("met"), bool) or not isinstance(check.get("evidence"), str):
            errors.append(f"acceptance_check[{index}] has invalid fields")
    return errors

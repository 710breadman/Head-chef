from __future__ import annotations

from dataclasses import dataclass, asdict, field
import json
from typing import Any

from .contracts import REVIEW_STATUSES, SCHEMA_VERSION, validate_worker_output


@dataclass(slots=True)
class VerificationResult:
    status: str
    valid_schema: bool
    acceptance_complete: bool
    requires_coordinator_review: bool
    notes: list[str] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_worker_output(content: str) -> tuple[dict[str, Any] | None, list[str]]:
    if len(content.encode("utf-8")) > 2_000_000:
        return None, ["worker output exceeds 2000000 bytes"]
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        return None, [f"worker output is not valid JSON: {exc.msg}"]
    errors = validate_worker_output(value)
    return (value if not errors else None), errors


def verify_output(
    value: dict[str, Any] | None,
    errors: list[str],
    acceptance_criteria: list[str],
    *,
    coordinator_review_required: bool,
) -> VerificationResult:
    if errors or value is None:
        return VerificationResult("needs_revision", False, False, coordinator_review_required, errors)
    reported = {
        check.get("criterion")
        for check in value["acceptance_check"]
        if isinstance(check, dict) and check.get("met") is True and check.get("evidence")
    }
    complete = all(item in reported for item in acceptance_criteria)
    notes: list[str] = []
    if not complete:
        notes.append("Worker did not provide evidence for every acceptance criterion.")
    if value["blockers"]:
        notes.append("Worker reported blockers.")
    status = "partially_useful" if not complete or value["blockers"] else "accepted"
    if coordinator_review_required:
        status = "pending"
        notes.append("Coordinator review required; automated verification cannot accept this run.")
    return VerificationResult(status, True, complete, coordinator_review_required, notes)


def validate_review_status(status: str) -> None:
    if status not in REVIEW_STATUSES:
        raise ValueError(f"Invalid review status: {status}")

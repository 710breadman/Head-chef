from __future__ import annotations

from dataclasses import dataclass, asdict, field
import json
from typing import Any

from .contracts import REVIEW_STATUSES, SCHEMA_VERSION, validate_worker_output


def _criterion_claim(value: str) -> str:
    words = value.casefold().rstrip(".").split()
    if words and words[0] in {"report", "state", "provide", "produce", "identify", "explain", "return"}:
        words = words[1:]
    if words and words[0] == "that":
        words = words[1:]
    return " ".join(words)


def _criterion_tokens(value: str) -> set[str]:
    stop = {"the", "a", "an", "that", "this", "one", "to", "and", "or", "of", "for", "statement"}
    tokens = []
    for raw in value.casefold().replace("-", " ").split():
        cleaned = "".join(character for character in raw if character.isalnum())
        if cleaned and cleaned not in stop and cleaned not in {"report", "state", "provide", "produce", "identify", "explain", "return"}:
            tokens.append(cleaned[:6])
    return set(tokens)


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
    normalizations: list[str] = []
    if (
        isinstance(value, dict)
        and isinstance(value.get("confidence"), (int, float))
        and not isinstance(value.get("confidence"), bool)
        and 1 < value["confidence"] <= 100
    ):
        original = value["confidence"]
        value["confidence"] = original / 100
        normalizations.append(f"confidence normalized from {original}/100 to {value['confidence']}/1")
    if isinstance(value, dict) and isinstance(value.get("blockers"), list):
        sentinels = {"none", "n/a", "no blockers", "not applicable"}
        blockers = [
            blocker for blocker in value["blockers"]
            if not isinstance(blocker, str) or blocker.strip().casefold() not in sentinels
        ]
        if len(blockers) != len(value["blockers"]):
            normalizations.append("removed sentinel non-blocker value")
            value["blockers"] = blockers
    if isinstance(value, dict) and normalizations:
        value["normalizations"] = normalizations
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
    reported_token_sets = [
        _criterion_tokens(check["criterion"])
        for check in value["acceptance_check"]
        if isinstance(check, dict) and check.get("met") is True and check.get("evidence")
    ]
    result_text = value["result"].casefold()
    complete = True
    for item in acceptance_criteria:
        criterion_tokens = _criterion_tokens(item)
        if not (
            item in reported
            or item.casefold().rstrip(".") in result_text
            or _criterion_claim(item) in result_text
            or (criterion_tokens and any(criterion_tokens <= tokens for tokens in reported_token_sets))
        ):
            complete = False
            break
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

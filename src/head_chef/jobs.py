from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import re
from typing import Any

from .router import RouteDecision
from .storage import atomic_write_json, compact_timestamp, utc_now
from .contracts import SCHEMA_VERSION
import secrets


@dataclass(slots=True)
class JobCard:
    id: str
    project: str
    task: str
    status: str = "ready"
    selected_model: str | None = None
    category: str = "analysis"
    created_at: str = field(default_factory=utc_now)
    allowed_files: list[str] = field(default_factory=list)
    forbidden_files: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    test_commands: list[str] = field(default_factory=list)
    context_notes: list[str] = field(default_factory=list)
    context_text: str = ""
    exclusions: list[str] = field(default_factory=list)
    coordinator_review_required: bool = True
    requires_split: bool = False
    routing_explanation: str = ""
    schema_version: str = SCHEMA_VERSION
    task_profile: dict[str, Any] = field(default_factory=dict)
    input_images: list[str] = field(default_factory=list)
    parent_job_id: str | None = None
    dependencies: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return cleaned[:48] or "task"


def create_job_card(
    project: str,
    task: str,
    decision: RouteDecision,
    *,
    allowed_files: list[str] | None = None,
    forbidden_files: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    test_commands: list[str] | None = None,
    context_notes: list[str] | None = None,
    context_text: str = "",
    exclusions: list[str] | None = None,
) -> JobCard:
    return JobCard(
        id=f"HC-{compact_timestamp()}-{secrets.token_hex(4)}-{_slug(task)}",
        project=project,
        task=task,
        selected_model=decision.selected_model,
        category=decision.category,
        allowed_files=allowed_files or [],
        forbidden_files=forbidden_files or [],
        acceptance_criteria=acceptance_criteria or [],
        test_commands=test_commands or [],
        context_notes=context_notes or [],
        context_text=context_text,
        exclusions=exclusions or [],
        coordinator_review_required=decision.coordinator_review_required,
        requires_split=decision.requires_split,
        routing_explanation=decision.explanation,
    )


def save_job_card(job: JobCard, jobs_dir: Path) -> Path:
    path = jobs_dir / f"{job.id}.json"
    from .storage import atomic_create_json
    atomic_create_json(path, job.to_dict())
    return path


def load_job_card(path: Path) -> JobCard:
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    version = data.get("schema_version", "1.0")
    if version == "1.0":
        data["schema_version"] = SCHEMA_VERSION
    elif version != SCHEMA_VERSION:
        raise ValueError(f"Unsupported job schema_version: {version}")
    if not isinstance(data.get("task"), str) or not data["task"].strip():
        raise ValueError("Job task must be a non-empty string")
    if data.get("category") not in {"analysis", "coding", "planning", "vision", "writing", "retrieval", "embedding"}:
        raise ValueError("Invalid job category")
    return JobCard(**data)


def render_worker_prompt(job: JobCard) -> str:
    allowed = "\n".join(f"- {item}" for item in job.allowed_files) or "- No files supplied"
    forbidden = "\n".join(f"- {item}" for item in job.forbidden_files) or "- Everything outside the allowed scope"
    acceptance = "\n".join(f"- {item}" for item in job.acceptance_criteria) or "- Produce a precise, reviewable result"
    tests = "\n".join(f"- {item}" for item in job.test_commands) or "- Do not claim tests were run"
    exclusions = "\n".join(f"- {item}" for item in job.exclusions) or "- No unrelated work"
    notes = "\n".join(f"- {item}" for item in job.context_notes) or "- None"
    supplied_context = job.context_text.strip() or "No additional text context supplied."

    return f"""You are a bounded local worker operating under Codex/OpenAI supervision.

JOB ID: {job.id}
CATEGORY: {job.category}
PROJECT: {job.project}

TASK
{job.task}

ALLOWED FILES OR INPUTS
{allowed}

FORBIDDEN SCOPE
{forbidden}

EXPLICIT EXCLUSIONS
{exclusions}

CONTEXT NOTES
{notes}

EXPLICITLY SUPPLIED CONTEXT
{supplied_context}

ACCEPTANCE CRITERIA
{acceptance}

TEST OR VERIFICATION COMMANDS
{tests}

OUTPUT
- Return only one JSON object matching supplied schema.
- Never wrap JSON in Markdown fences.

RULES
- Do not silently change architecture, public interfaces, stored data, security policy, or dependencies.
- Do not claim to have read files that were not supplied.
- Do not claim commands or tests were run.
- State assumptions and uncertainties.
- Stay inside the exact task.
- Return a concise proposed solution, patch guidance, analysis, or draft suitable for coordinator review.
- End with: RISKS, VERIFICATION NEEDED, and BLOCKERS.
"""

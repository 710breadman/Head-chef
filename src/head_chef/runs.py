from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
import secrets
from typing import Any

from .contracts import SCHEMA_VERSION
from .storage import atomic_create_json, utc_now


@dataclass(slots=True)
class RunRecord:
    job_id: str
    model: str
    executor: str
    attempt_number: int
    ok: bool
    run_id: str = field(default_factory=lambda: f"run-{secrets.token_hex(8)}")
    schema_version: str = SCHEMA_VERSION
    created_at: str = field(default_factory=utc_now)
    parent_run_id: str | None = None
    prompt: str | None = None
    response: Any = None
    validation_errors: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    review_status: str = "pending"
    review_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def next_attempt(runs_dir: Path, job_id: str) -> int:
    return 1 + sum(1 for path in runs_dir.glob("*.json") if _belongs_to_job(path, job_id))


def _belongs_to_job(path: Path, job_id: str) -> bool:
    import json
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("job_id") == job_id
    except (OSError, json.JSONDecodeError):
        return False


def save_run(run: RunRecord, runs_dir: Path) -> Path:
    path = runs_dir / f"{run.created_at[:10]}-{run.run_id}.json"
    atomic_create_json(path, run.to_dict())
    return path

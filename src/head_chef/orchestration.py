from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .models import ModelProfile
from .ollama import OllamaClient, OllamaError
from .router import RouteRequest, route


IGNORED_DIRECTORIES = {".git", ".head-chef", ".venv", "node_modules", "build", "dist"}
ROLE_NAMES = {
    "coding": "implementation station",
    "vision": "visual inspection station",
    "embedding": "indexing station",
    "planning": "planning station",
    "writing": "writing station",
    "retrieval": "research station",
    "analysis": "verification station",
}
PHASE_REVIEW_SCHEMA = {
    "type": "object",
    "required": ["phase_summary", "task_notes"],
    "properties": {
        "phase_summary": {"type": "string"},
        "task_notes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "needs", "risks"],
                "properties": {
                    "id": {"type": "string"},
                    "needs": {"type": "array", "items": {"type": "string"}},
                    "risks": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}


def _contained(root: Path, value: Path) -> Path:
    root = root.resolve()
    resolved = value.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Sprint file escapes project root: {value}") from exc
    return resolved


def discover_sprint_file(project: Path, explicit: str | None = None) -> Path:
    project = project.resolve()
    if explicit:
        path = _contained(project, project / explicit)
        if not path.is_file():
            raise ValueError(f"Sprint file not found: {explicit}")
        return path

    preferred = (
        project / "sprints" / "SPRINTS.json",
        project / "SPRINTS.json",
        project / "sprints.json",
        project / "sprint.json",
    )
    for path in preferred:
        if path.is_file():
            return path.resolve()

    candidates: list[Path] = []
    for path in project.rglob("*"):
        if any(part in IGNORED_DIRECTORIES for part in path.relative_to(project).parts):
            continue
        if path.is_file() and "sprint" in path.name.casefold() and path.suffix.casefold() == ".json":
            candidates.append(path)
        if len(candidates) > 100:
            break
    if not candidates:
        raise ValueError("No JSON sprint file found. Use --sprint-file to select one.")
    return sorted(candidates, key=lambda item: (len(item.relative_to(project).parts), str(item)))[0].resolve()


def _read_json(path: Path) -> dict[str, Any]:
    if path.stat().st_size > 2_000_000:
        raise ValueError(f"Sprint file exceeds 2000000 bytes: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid sprint JSON at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Sprint JSON must be an object: {path}")
    return value


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if isinstance(item, (str, int))]
    return []


def load_sprint_tasks(project: Path, manifest: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    project = project.resolve()
    manifest = _contained(project, manifest)
    documents: list[tuple[Path, dict[str, Any]]] = [(manifest, _read_json(manifest))]
    contract = documents[0][1].get("task_contracts")
    patterns = _string_list(contract)
    for pattern in patterns:
        for path in sorted(project.glob(pattern)):
            path = _contained(project, path)
            if path.is_file() and path != manifest and path.suffix.casefold() == ".json":
                documents.append((path, _read_json(path)))

    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for path, document in documents:
        for index, raw in enumerate(document.get("tasks", [])):
            if not isinstance(raw, dict):
                continue
            task_id = str(raw.get("id") or f"{path.stem}-{index + 1}")
            if task_id not in merged:
                merged[task_id] = {}
                order.append(task_id)
            merged[task_id].update(raw)
            merged[task_id]["id"] = task_id
            merged[task_id].setdefault("phase", document.get("phase") or "unassigned")
    if not merged:
        raise ValueError(f"No tasks found in sprint contract: {manifest}")
    if len(merged) > 2000:
        raise ValueError("Sprint contract exceeds 2000 tasks")

    tasks = [normalize_task(merged[task_id]) for task_id in order]
    sources = [
        {
            "path": path.relative_to(project).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path, _ in documents
    ]
    return tasks, sources


def normalize_task(raw: dict[str, Any]) -> dict[str, Any]:
    title = str(raw.get("title") or raw.get("name") or raw.get("objective") or raw["id"])
    objective = str(raw.get("objective") or raw.get("description") or title)
    return {
        "id": str(raw["id"]),
        "title": title,
        "objective": objective,
        "phase": str(raw.get("phase") or raw.get("sprint") or raw.get("milestone") or "unassigned"),
        "status": str(raw.get("status") or "unknown").casefold(),
        "dependencies": _string_list(raw.get("dependencies") or raw.get("depends_on")),
        "acceptance_criteria": _string_list(
            raw.get("acceptance_criteria") or raw.get("acceptance") or raw.get("definition_of_done")
        ),
        "expected_files": _string_list(raw.get("expected_files") or raw.get("files") or raw.get("allowed_files")),
        "evidence": _string_list(raw.get("evidence")),
    }


def infer_stations(task: dict[str, Any]) -> tuple[str, list[str]]:
    text = " ".join(
        [
            task["title"],
            task["objective"],
            *task["acceptance_criteria"],
            *task["expected_files"],
        ]
    ).casefold()
    has = lambda term: re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None
    expected = task["expected_files"]
    codeish = any(
        Path(value.rstrip("/\\")).suffix.casefold() in {".py", ".gd", ".ps1", ".sh", ".ts", ".js", ".cs"}
        or value.casefold().startswith(("game/", "src/", "tools/", "tests/"))
        for value in expected
    )
    visual = any(has(word) for word in ("visual", "sprite", "screenshot", "camera", "pixel", "render", "lighting", "art"))
    planning = any(has(word) for word in ("roadmap", "architecture", "decision", "choose", "lock or reopen", "adr"))
    writing = any(has(word) for word in ("dialogue", "narrative", "prose", "chapter", "story", "rewrite"))
    retrieval = any(has(word) for word in ("research", "inventory", "retrieve", "source review"))
    embedding = any(has(word) for word in ("embedding", "semantic index", "vector index"))

    if embedding:
        primary = "embedding"
    elif planning:
        primary = "planning"
    elif codeish:
        primary = "coding"
    elif visual:
        primary = "vision"
    elif writing:
        primary = "writing"
    elif retrieval:
        primary = "retrieval"
    else:
        primary = "analysis"

    support: list[str] = []
    for condition, station in (
        (visual, "vision"),
        (planning, "planning"),
        (writing, "writing"),
        (retrieval, "retrieval"),
        (codeish and primary != "coding", "coding"),
    ):
        if condition and station != primary and station not in support:
            support.append(station)
    if primary in {"coding", "vision", "planning"} and "analysis" not in support:
        support.append("analysis")
    return primary, support[:3]


def build_sprint_plan(
    tasks: list[dict[str, Any]],
    profiles: list[ModelProfile],
    *,
    benchmark_path: Path | None = None,
    outcome_path: Path | None = None,
) -> dict[str, Any]:
    completed = {task["id"] for task in tasks if task["status"] in {"done", "completed", "accepted"}}
    planned: list[dict[str, Any]] = []
    for task in tasks:
        primary, supporting = infer_stations(task)

        def assignment(station: str) -> dict[str, Any]:
            decision = route(
                RouteRequest(
                    task=f"{task['title']}. {task['objective']}",
                    required_capability=station,
                    prefer_quality=True,
                ),
                profiles,
                benchmark_path=benchmark_path,
                outcome_path=outcome_path,
            )
            return {
                "station": station,
                "role": ROLE_NAMES[station],
                "model": decision.selected_model,
                "confidence": decision.confidence,
                "review_required": decision.coordinator_review_required,
            }

        item = dict(task)
        item["actionable"] = task["status"] in {"ready", "active", "in_progress"} and all(
            dependency in completed for dependency in task["dependencies"]
        )
        item["primary_assignment"] = assignment(primary)
        item["supporting_assignments"] = [assignment(station) for station in supporting]
        planned.append(item)
    return {
        "task_count": len(planned),
        "actionable_task_ids": [task["id"] for task in planned if task["actionable"]],
        "tasks": planned,
    }


def add_local_phase_analysis(
    plan: dict[str, Any],
    client: OllamaClient,
    model: str,
    *,
    timeout_seconds: int,
) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for task in plan["tasks"]:
        groups[task["phase"]].append(
            {
                "id": task["id"],
                "title": task["title"],
                "objective": task["objective"],
                "status": task["status"],
                "dependencies": task["dependencies"],
                "acceptance_criteria": task["acceptance_criteria"],
                "expected_files": task["expected_files"],
            }
        )
    reviews: list[dict[str, Any]] = []
    errors: list[str] = []
    for phase, tasks in groups.items():
        prompt = (
            "Analyze only this supplied sprint phase. Explain concrete needs and risks for every task. "
            "Do not claim files were inspected or tests ran. Return the required JSON.\n\n"
            + json.dumps({"phase": phase, "tasks": tasks}, ensure_ascii=False)
        )
        try:
            response = client.chat(
                model,
                [{"role": "user", "content": prompt}],
                format_schema=PHASE_REVIEW_SCHEMA,
                options={"temperature": 0.1},
                timeout_seconds=timeout_seconds,
            )
            parsed = json.loads(response.content)
            if (
                not isinstance(parsed, dict)
                or not isinstance(parsed.get("phase_summary"), str)
                or not isinstance(parsed.get("task_notes"), list)
            ):
                raise ValueError("invalid phase analysis shape")
            known = {task["id"] for task in tasks}
            notes = parsed["task_notes"]
            if any(
                not isinstance(note, dict)
                or not isinstance(note.get("id"), str)
                or not isinstance(note.get("needs"), list)
                or not all(isinstance(value, str) for value in note["needs"])
                or not isinstance(note.get("risks"), list)
                or not all(isinstance(value, str) for value in note["risks"])
                for note in notes
            ):
                raise ValueError("invalid task note shape")
            received = [note["id"] for note in notes]
            if len(received) != len(set(received)) or set(received) != known:
                raise ValueError("phase analysis did not cover every task exactly once")
            reviews.append({"phase": phase, **parsed})
        except (ValueError, json.JSONDecodeError, OSError, OllamaError) as exc:
            errors.append(f"{phase}: {exc}")
    plan["local_phase_analysis"] = {
        "model": model,
        "advisory_only": True,
        "reviews": reviews,
        "errors": errors,
    }
    return plan

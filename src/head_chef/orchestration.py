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
from .visual import GENERATION_STATION_OUTPUT_TYPES, select_approved_template


IGNORED_DIRECTORIES = {".git", ".head-chef", ".venv", "node_modules", "build", "dist"}
ROLE_NAMES = {
    "coding": "implementation station",
    "vision": "visual inspection station",
    "embedding": "indexing station",
    "planning": "planning station",
    "writing": "writing station",
    "retrieval": "research station",
    "analysis": "verification station",
    "image_generation": "image generation station",
    "video_generation": "video generation station",
}
STATIONS = set(ROLE_NAMES)
PHASE_REVIEW_SCHEMA = {
    "type": "object",
    "required": ["phase_summary", "task_notes"],
    "properties": {
        "phase_summary": {"type": "string"},
        "task_notes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "id",
                    "needs",
                    "risks",
                    "recommended_primary_station",
                    "recommended_supporting_stations",
                    "local_scope",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "needs": {"type": "array", "items": {"type": "string"}},
                    "risks": {"type": "array", "items": {"type": "string"}},
                    "recommended_primary_station": {
                        "type": "string",
                        "enum": sorted(STATIONS | {"coordinator_only"}),
                    },
                    "recommended_supporting_stations": {
                        "type": "array",
                        "items": {"type": "string", "enum": sorted(STATIONS)},
                        "uniqueItems": True,
                    },
                    "local_scope": {"type": "string", "enum": ["full", "advisory", "none"]},
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
        name = path.name.casefold()
        if (
            path.is_file()
            and any(label in name for label in ("sprint", "roadmap"))
            and path.suffix.casefold() == ".json"
        ):
            candidates.append(path)
        if len(candidates) > 100:
            break
    if not candidates:
        raise ValueError("No JSON sprint or roadmap file found. Use --sprint-file to select one.")
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


def profile_sprint_task(task: dict[str, Any]) -> dict[str, Any]:
    text = " ".join(
        [
            task["title"],
            task["objective"],
            *task["acceptance_criteria"],
            *task["expected_files"],
        ]
    ).casefold()
    action_text = f"{task['title']} {task['objective']}".casefold()
    has = lambda term: re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None
    expected = task["expected_files"]
    codeish = any(
        Path(value.rstrip("/\\")).suffix.casefold() in {".py", ".gd", ".ps1", ".sh", ".ts", ".js", ".cs"}
        or value.casefold().startswith(("game/", "src/", "tools/", "tests/"))
        for value in expected
    )
    vision_inspect = any(
        has(word) for word in ("screenshot", "photo", "ocr", "diagram", "inspect", "visual defect", "visual qa")
    )
    image_gen = any(
        has(word) for word in (
            "sprite", "camera", "pixel", "render", "lighting", "art", "illustration",
            "concept art", "texture", "icon", "thumbnail", "cover art", "artwork", "logo",
        )
    )
    video_gen = any(
        has(word) for word in ("video", "animation", "cutscene", "trailer", "motion graphic", "clip")
    )
    planning = any(has(word) for word in ("roadmap", "architecture", "decision", "choose", "lock or reopen", "adr"))
    writing = any(has(word) for word in ("dialogue", "narrative", "prose", "chapter", "story", "rewrite"))
    retrieval = any(has(word) for word in ("research", "inventory", "retrieve", "source review"))
    embedding = any(has(word) for word in ("embedding", "semantic index", "vector index"))
    owner_only = any(
        phrase in action_text
        for phrase in (
            "select license",
            "choose license",
            "legal decision",
            "rotate secret",
            "rotate credential",
            "production deploy",
            "publish release",
            "delete production",
            "destructive migration",
            "purchase",
        )
    )
    tool_required = any(
        re.search(rf"(?<![a-z0-9]){verb}(?![a-z0-9])", action_text)
        for verb in ("run", "execute", "install", "launch", "compile", "deploy", "benchmark", "smoke test")
    )
    high_risk = owner_only or any(
        phrase in text
        for phrase in (
            "security policy",
            "public interface",
            "database migration",
            "schema migration",
            "production",
            "credential",
            "secret",
            "license",
        )
    )

    if owner_only:
        primary: str | None = None
    elif embedding:
        primary = "embedding"
    elif planning:
        primary = "planning"
    elif codeish:
        primary = "coding"
    elif video_gen:
        primary = "video_generation"
    elif image_gen:
        primary = "image_generation"
    elif vision_inspect:
        primary = "vision"
    elif writing:
        primary = "writing"
    elif retrieval:
        primary = "retrieval"
    else:
        primary = "analysis"

    support: list[str] = []
    for condition, station in (
        (video_gen, "video_generation"),
        (image_gen, "image_generation"),
        (vision_inspect, "vision"),
        (planning, "planning"),
        (writing, "writing"),
        (retrieval, "retrieval"),
        (codeish and primary != "coding", "coding"),
    ):
        if condition and station != primary and station not in support:
            support.append(station)
    if (
        primary in {"coding", "vision", "planning", "image_generation", "video_generation"}
        and "analysis" not in support
    ):
        support.append("analysis")
    if primary is None and "analysis" not in support:
        support.insert(0, "analysis")

    required_inputs = ["image"] if vision_inspect and primary == "vision" else []
    required_tools = ["shell"] if tool_required else []
    local_scope = "none" if owner_only else "advisory" if tool_required or high_risk else "full"
    coordinator_reasons: list[str] = []
    if owner_only:
        coordinator_reasons.append("Owner/legal/destructive decision cannot be delegated to a local model.")
    if tool_required:
        coordinator_reasons.append("Acceptance requires command execution unavailable to bounded local workers.")
    if high_risk and not owner_only:
        coordinator_reasons.append("High-risk change requires coordinator review and application.")
    if video_gen:
        modalities = ["text", "video"]
    elif image_gen or vision_inspect:
        modalities = ["text", "image"]
    else:
        modalities = ["text"]
    return {
        "schema_version": "1.0",
        "work_kind": primary or "coordinator_decision",
        "primary_station": primary,
        "supporting_stations": support[:3],
        "modalities": modalities,
        "risk": "high" if high_risk else "medium" if tool_required else "low",
        "required_inputs": required_inputs,
        "required_tools": required_tools,
        "local_scope": local_scope,
        "coordinator_action_required": local_scope != "full",
        "coordinator_reasons": coordinator_reasons,
        "signals": {
            "code": codeish,
            "vision_inspect": vision_inspect,
            "image_generation": image_gen,
            "video_generation": video_gen,
            "planning": planning,
            "writing": writing,
            "retrieval": retrieval,
            "embedding": embedding,
            "owner_only": owner_only,
            "tool_required": tool_required,
        },
    }


def infer_stations(task: dict[str, Any]) -> tuple[str | None, list[str]]:
    profile = profile_sprint_task(task)
    return profile["primary_station"], profile["supporting_stations"]


def _visual_station_assignment(
    station: str, output_type: str, task_profile: dict[str, Any], primary: str | None,
) -> dict[str, Any]:
    template_id, spec = select_approved_template(output_type)
    status = "assigned" if template_id else "unfilled"
    reason = (
        f"Approved ComfyUI template '{template_id}' available for {output_type} generation."
        if template_id else (
            f"No approved {output_type} generation template installed; add one under "
            "workflows/comfyui/ and register it in manifest.json with a matching output_type."
        )
    )
    return {
        "station": station,
        "role": ROLE_NAMES[station],
        "model": template_id,
        "model_digest": spec.get("sha256") if spec else None,
        "confidence": 0.9 if template_id else 0.0,
        # Generated media is always coordinator-reviewed before use, matching visual-run.
        "review_required": True,
        "status": status,
        "scope": task_profile["local_scope"] if station == primary else "support",
        "required_inputs": [],
        "reason": reason,
        "score": None,
        "score_evidence": [],
        "backend": "comfyui",
    }


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
        task_profile = profile_sprint_task(task)
        primary = task_profile["primary_station"]
        supporting = task_profile["supporting_stations"]

        def assignment(station: str) -> dict[str, Any]:
            output_type = GENERATION_STATION_OUTPUT_TYPES.get(station)
            if output_type:
                return _visual_station_assignment(station, output_type, task_profile, primary)
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
            selected = next(
                (candidate for candidate in decision.candidates if candidate.model == decision.selected_model),
                None,
            )
            selected_profile = next(
                (profile for profile in profiles if profile.name == decision.selected_model),
                None,
            )
            required_inputs = ["image"] if station == "vision" else []
            status = "unfilled" if not decision.selected_model else "conditional" if required_inputs else "assigned"
            return {
                "station": station,
                "role": ROLE_NAMES[station],
                "model": decision.selected_model,
                "model_digest": selected_profile.digest if selected_profile else None,
                "confidence": decision.confidence,
                "review_required": decision.coordinator_review_required,
                "status": status,
                "scope": task_profile["local_scope"] if station == primary else "support",
                "required_inputs": required_inputs,
                "reason": decision.explanation,
                "score": selected.score if selected else None,
                "score_evidence": selected.reasons if selected else [],
                "backend": "ollama",
            }

        item = dict(task)
        item["actionable"] = task["status"] in {"ready", "active", "in_progress"} and all(
            dependency in completed for dependency in task["dependencies"]
        )
        item["task_profile"] = task_profile
        item["primary_assignment"] = assignment(primary) if primary else {
            "station": None,
            "role": "coordinator-only decision",
            "model": None,
            "model_digest": None,
            "confidence": 1.0,
            "review_required": True,
            "status": "abstained",
            "scope": "none",
            "required_inputs": [],
            "reason": "; ".join(task_profile["coordinator_reasons"]),
            "score": None,
            "score_evidence": [],
        }
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
            "Analyze only this supplied sprint phase. For every task, explain concrete needs and risks, "
            "recommend exactly one primary station (or coordinator_only), up to three supporting stations, "
            "and local_scope full/advisory/none. Stations: coding=implementation, vision=actual image inspection, "
            "embedding=vector creation, planning=architecture/decomposition, writing=prose/dialogue, "
            "retrieval=supplied-source lookup, analysis=verification, image_generation=create new image/art/sprite "
            "assets via an approved ComfyUI template, video_generation=create new video/animation assets via an "
            "approved ComfyUI template. Command execution is advisory because "
            "workers have no shell. Legal, owner, destructive, secret, and production decisions are coordinator_only. "
            "Do not claim files were inspected or tests ran. Return the required JSON.\n\n"
            + json.dumps({"phase": phase, "tasks": tasks}, ensure_ascii=False)
        )
        try:
            response = client.chat(
                model,
                [{"role": "user", "content": prompt}],
                format_schema=PHASE_REVIEW_SCHEMA,
                options={"temperature": 0},
                think=False,
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
                or note.get("recommended_primary_station") not in STATIONS | {"coordinator_only"}
                or not isinstance(note.get("recommended_supporting_stations"), list)
                or not all(value in STATIONS for value in note["recommended_supporting_stations"])
                or len(note["recommended_supporting_stations"]) > 3
                or len(note["recommended_supporting_stations"])
                != len(set(note["recommended_supporting_stations"]))
                or note.get("recommended_primary_station") in note["recommended_supporting_stations"]
                or note.get("local_scope") not in {"full", "advisory", "none"}
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
    recommendations = {
        note["id"]: note
        for review in reviews
        for note in review["task_notes"]
    }
    disagreement_count = 0
    station_disagreement_count = 0
    scope_disagreement_count = 0
    for task in plan["tasks"]:
        note = recommendations.get(task["id"])
        if not note:
            continue
        recommended = note["recommended_primary_station"]
        recommended_station = None if recommended == "coordinator_only" else recommended
        deterministic_station = task["primary_assignment"]["station"]
        station_agrees = recommended_station == deterministic_station
        scope_agrees = note["local_scope"] == task["task_profile"]["local_scope"]
        agrees = station_agrees and scope_agrees
        if not agrees:
            disagreement_count += 1
        if not station_agrees:
            station_disagreement_count += 1
        if not scope_agrees:
            scope_disagreement_count += 1
        task["local_assignment_review"] = {
            "model": model,
            "recommended_primary_station": recommended_station,
            "recommended_supporting_stations": note["recommended_supporting_stations"],
            "recommended_local_scope": note["local_scope"],
            "station_agrees": station_agrees,
            "scope_agrees": scope_agrees,
            "agrees_with_deterministic_profile": agrees,
            "needs": note["needs"],
            "risks": note["risks"],
            "advisory_only": True,
        }
        task["assignment_review_required"] = not agrees
    plan["local_phase_analysis"]["assignment_disagreement_count"] = disagreement_count
    plan["local_phase_analysis"]["station_disagreement_count"] = station_disagreement_count
    plan["local_phase_analysis"]["scope_disagreement_count"] = scope_disagreement_count
    return plan

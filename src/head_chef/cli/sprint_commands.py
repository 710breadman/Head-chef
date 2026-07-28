"""Sprint discovery, planning, guided outline creation, and dependency-ready dispatch."""
from __future__ import annotations

import argparse
import concurrent.futures
from dataclasses import dataclass
import json
from pathlib import Path
import secrets
import sys
from typing import Any

from ..benchmark import STRENGTH_CASES
from ..comfyui import ComfyUIClient, ComfyUIError
from ..config import Settings, load_settings
from ..contracts import CONTRACT_VERSION
from ..jobs import create_job_card, save_job_card
from ..ollama import OllamaError
from ..orchestration import add_local_phase_analysis, build_sprint_plan, discover_sprint_file, load_sprint_tasks
from ..registry import apply_overrides, load_overrides
from ..router import RouteRequest, route
from ..storage import atomic_write_json, compact_timestamp, ensure_state, utc_now
from ..visual import GENERATION_STATION_OUTPUT_TYPES

from ._shared import _client, _evidence_path, _json, _profiles
from .job_commands import _cook_core, _dispatch_saved_job
from .visual_commands import _visual_job_core, _visual_run_core


def _orchestrate_core(args: argparse.Namespace) -> tuple[int, dict[str, Any] | None]:
    project = Path(args.project).resolve()
    settings, _ = load_settings(project)
    state = ensure_state(project, settings.state_dir)
    try:
        client = _client(settings)
        profiles = _profiles(client, settings)
        overrides = load_overrides(_evidence_path(project, settings, "model-overrides.json"))
        profiles = [apply_overrides(profile, overrides.get(profile.name, {})) for profile in profiles]
        requested_models = list(getattr(args, "model", []) or [])
        assignment_profiles = profiles
        if requested_models:
            installed = {profile.name for profile in profiles}
            missing = sorted(set(requested_models) - installed)
            if missing:
                raise ValueError(f"Requested roster models are not installed: {', '.join(missing)}")
            assignment_profiles = [profile for profile in profiles if profile.name in requested_models]
        manifest = discover_sprint_file(project, args.sprint_file)
        tasks, sources = load_sprint_tasks(project, manifest)
        plan = build_sprint_plan(
            tasks,
            assignment_profiles,
            benchmark_path=_evidence_path(project, settings, "benchmarks/latest.json"),
            outcome_path=_evidence_path(project, settings, "outcomes.jsonl"),
        )
        if not args.no_local_analysis:
            planning = route(
                RouteRequest(
                    task="Understand sprint requirements, dependencies, risks, and orchestration needs",
                    required_capability="planning",
                    prefer_quality=True,
                ),
                profiles,
                benchmark_path=_evidence_path(project, settings, "benchmarks/latest.json"),
                outcome_path=_evidence_path(project, settings, "outcomes.jsonl"),
            )
            if not planning.selected_model:
                raise ValueError("No local planning station is available for sprint analysis")
            add_local_phase_analysis(
                plan,
                client,
                planning.selected_model,
                timeout_seconds=args.timeout or settings.request_timeout_seconds,
            )
    except (OllamaError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1, None

    plan.update({
        "contract_version": CONTRACT_VERSION,
        "schema_version": "1.0",
        "created_at": utc_now(),
        "project": str(project),
        "manifest": manifest.relative_to(project).as_posix(),
        "sources": sources,
    })
    active_output = state / "orchestration" / "sprint-plan.json"
    previous = None
    if active_output.exists():
        try:
            previous = json.loads(active_output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = None
    assignment_changes = _assignment_changes(previous, plan)
    apply_plan = not requested_models or bool(getattr(args, "apply_roster", False))
    output = active_output if apply_plan else (
        state / "orchestration" / "candidate-plans"
        / f"{compact_timestamp()}-{secrets.token_hex(4)}.json"
    )
    atomic_write_json(output, plan)
    comparison_path = state / "orchestration" / "reassignments" / (
        f"{compact_timestamp()}-{secrets.token_hex(4)}.json"
    )
    atomic_write_json(comparison_path, {
        "contract_version": CONTRACT_VERSION,
        "schema_version": "1.0",
        "created_at": utc_now(),
        "roster": [profile.name for profile in assignment_profiles],
        "changes": assignment_changes,
    })
    role_counts: dict[str, int] = {}
    assignment_status_counts: dict[str, int] = {}
    for task in plan["tasks"]:
        primary_assignment = task["primary_assignment"]
        station = primary_assignment["station"] or "coordinator-only"
        role_counts[station] = role_counts.get(station, 0) + 1
        assignment_status = primary_assignment["status"]
        assignment_status_counts[assignment_status] = assignment_status_counts.get(assignment_status, 0) + 1
    local_analysis = plan.get("local_phase_analysis", {})
    payload = {
        "status": "planned",
        "applied": apply_plan,
        "active_plan_path": str(active_output),
        "plan_path": str(output),
        "manifest": plan["manifest"],
        "source_count": len(sources),
        "task_count": plan["task_count"],
        "actionable_task_ids": plan["actionable_task_ids"],
        "primary_role_counts": role_counts,
        "assignment_status_counts": assignment_status_counts,
        "roster": [profile.name for profile in assignment_profiles],
        "assignment_changes": assignment_changes,
        "assignment_change_count": len(assignment_changes),
        "comparison_path": str(comparison_path),
        "local_analysis_model": local_analysis.get("model"),
        "local_analysis_phases": len(local_analysis.get("reviews", [])),
        "local_analysis_errors": local_analysis.get("errors", []),
        "local_assignment_disagreements": local_analysis.get("assignment_disagreement_count", 0),
        "local_station_disagreements": local_analysis.get("station_disagreement_count", 0),
        "local_scope_disagreements": local_analysis.get("scope_disagreement_count", 0),
    }
    return 0, payload


def cmd_orchestrate(args: argparse.Namespace) -> int:
    code, payload = _orchestrate_core(args)
    if payload is not None:
        _json(payload)
    return code


def _create_sprint_outline(project: Path, explicit: str | None = None) -> Path:
    project = project.resolve()
    path = (project / (explicit or "sprints/SPRINTS.json")).resolve()
    try:
        path.relative_to(project)
    except ValueError as exc:
        raise ValueError(f"Sprint file escapes project root: {path}") from exc
    if path.suffix.casefold() != ".json":
        raise ValueError("Sprint outline must be a JSON file.")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ValueError(f"Sprint outline already exists: {path}")
    atomic_write_json(path, {
        "schema_version": "1.0",
        "title": f"{project.name} sprint outline",
        "phases": [],
        "tasks": [],
    })
    return path


def cmd_sprint_check(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    try:
        manifest = discover_sprint_file(project, args.sprint_file)
        outline_status = "found"
    except ValueError as exc:
        if args.sprint_file and "not found" not in str(exc):
            raise
        if not args.sprint_file and "No JSON sprint or roadmap file found" not in str(exc):
            raise
        create = bool(args.yes)
        if not create:
            try:
                print("No sprint outline found. Create one? [y/N] ", end="", file=sys.stderr)
                answer = input()
            except EOFError:
                answer = ""
            create = answer.strip().casefold() in {"y", "yes"}
        if not create:
            _json({"status": "outline_missing", "outline": None, "planned": False})
            return 1
        manifest = _create_sprint_outline(project, args.sprint_file)
        outline_status = "created"

    orchestrate_args = argparse.Namespace(
        project=str(project),
        sprint_file=manifest.relative_to(project).as_posix(),
        no_local_analysis=args.no_local_analysis,
        timeout=args.timeout,
        model=[],
        apply_roster=False,
    )
    plan_code, plan_payload = _orchestrate_core(orchestrate_args)
    _json({
        "status": "planned" if plan_code == 0 else "plan_failed",
        "outline_status": outline_status,
        "outline": orchestrate_args.sprint_file,
        "planned": plan_code == 0,
        "plan": plan_payload,
    })
    return plan_code


def _assignment_changes(previous: object, current: dict[str, Any]) -> list[dict[str, Any]]:
    previous_tasks = previous.get("tasks", []) if isinstance(previous, dict) else []
    old = {
        str(task.get("id")): task.get("primary_assignment", {})
        for task in previous_tasks
        if isinstance(task, dict) and task.get("id")
    }
    changes: list[dict[str, Any]] = []
    for task in current.get("tasks", []):
        if not isinstance(task, dict) or not task.get("id"):
            continue
        task_id = str(task["id"])
        before = old.get(task_id)
        after = task.get("primary_assignment", {})
        if before is None:
            changes.append({
                "task_id": task_id, "change": "new_task",
                "before": None, "after": _assignment_identity(after),
            })
            continue
        before_identity = _assignment_identity(before)
        after_identity = _assignment_identity(after)
        if before_identity != after_identity:
            changes.append({
                "task_id": task_id, "change": "reassigned",
                "before": before_identity, "after": after_identity,
            })
    return changes


def _assignment_identity(value: object) -> dict[str, Any]:
    assignment = value if isinstance(value, dict) else {}
    return {
        "station": assignment.get("station"),
        "model": assignment.get("model"),
        "status": assignment.get("status"),
        "model_digest": assignment.get("model_digest"),
    }


def _existing_plan_files(project: Path, values: list[Any]) -> list[str]:
    files: list[str] = []
    root = project.resolve()
    for value in values:
        if not isinstance(value, str):
            continue
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts or ":" in value:
            continue
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        if candidate.is_file():
            files.append(candidate.relative_to(root).as_posix())
    return files


def _load_work_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": "1.0", "tasks": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("tasks"), dict):
        raise ValueError("work ledger must contain a tasks object")
    return value


def _completed_sprint_tasks(tasks: list[dict[str, Any]], ledger: dict[str, Any]) -> set[str]:
    completed = {
        str(task.get("id"))
        for task in tasks
        if task.get("status") in {"done", "completed", "accepted"}
    }
    completed.update(
        task_id
        for task_id, record in ledger.get("tasks", {}).items()
        if isinstance(record, dict) and record.get("status") == "accepted"
    )
    return completed


def _dependency_handoff(task: dict[str, Any], ledger: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    records = ledger.get("tasks", {})
    for dependency in task.get("dependencies") or []:
        record = records.get(dependency)
        if not isinstance(record, dict) or record.get("status") != "accepted":
            continue
        summary = record.get("result_summary")
        run_id = record.get("run_id")
        notes.append(
            f"Accepted dependency {dependency}"
            + (f" run {run_id}" if run_id else "")
            + (f": {summary}" if summary else "")
        )
    return notes


def _primary_run_from_payload(payload: object) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    run = payload.get("run")
    if isinstance(run, dict):
        return run
    synthesis = payload.get("synthesis")
    if isinstance(synthesis, dict) and isinstance(synthesis.get("run"), dict):
        return synthesis["run"]
    return None


def _dispatch_visual_task(
    project: Path,
    settings: Settings,
    task: dict[str, Any],
    assignment: dict[str, Any],
    args: argparse.Namespace,
) -> tuple[int, dict[str, Any]]:
    """Dispatch a sprint task assigned to a ComfyUI-backed generation station."""
    template_id = assignment.get("model")
    if not template_id:
        return 2, {"status": "no_eligible_model", "reason": assignment.get("reason")}
    try:
        inventory = ComfyUIClient(
            settings.comfyui_url, min(settings.request_timeout_seconds, 10),
        ).model_inventory()
    except (ComfyUIError, OSError) as exc:
        return 2, {"status": "backend_unavailable", "reason": str(exc)}
    checkpoints = inventory.get("checkpoints", [])
    if not checkpoints:
        return 2, {
            "status": "no_eligible_model",
            "reason": "No ComfyUI checkpoint installed for image/video generation.",
        }
    prompt_text = f"{task.get('title', '')}. {task.get('objective', '')}".strip()[:4000]
    acceptance = list(task.get("acceptance_criteria") or []) or [
        "Generated asset matches the sprint objective",
    ]
    job_args = argparse.Namespace(
        project=str(project),
        template=template_id,
        param=[f"checkpoint={checkpoints[0]}", f"prompt={prompt_text}"],
        acceptance=acceptance,
        timeout=args.timeout,
        max_attempts=args.max_attempts,
        release_ollama=False,
        verifier_model="qwen3-vl:8b-instruct",
    )
    job_code, job_payload = _visual_job_core(job_args)
    if job_code != 0 or not isinstance(job_payload, dict) or not job_payload.get("job_path"):
        return job_code or 2, {"status": "visual_job_failed", "result": job_payload}
    run_args = argparse.Namespace(
        project=str(project), job=job_payload["job_path"], verify_timeout=args.timeout,
    )
    run_code, run_payload = _visual_run_core(run_args)
    return run_code, {"status": "coordinator_review_required", "job": job_payload, "run": run_payload}


@dataclass(slots=True)
class _TaskOutcome:
    item_result: dict[str, Any]
    ledger_entry: dict[str, Any] | None
    code: int


def _dispatch_one_task(
    task: dict[str, Any],
    project: Path,
    settings: Settings,
    state: Path,
    ledger: dict[str, Any],
    artifact: Path,
    args: argparse.Namespace,
) -> _TaskOutcome:
    """Dispatch one dependency-ready sprint task. Only reads `ledger` (for dependency handoff
    notes) — never mutates it — so this is safe to run concurrently across tasks from a thread
    pool; the caller applies the returned ledger_entry after every task in the batch completes.
    """
    assignment = task.get("primary_assignment", {})
    category = assignment.get("station")
    model = assignment.get("model")
    assignment_status = assignment.get("status", "assigned")
    local_review = task.get("local_assignment_review", {})
    if (
        isinstance(local_review, dict)
        and local_review.get("station_agrees") is False
        and not args.accept_assignment_review
    ):
        return _TaskOutcome(
            {
                "task_id": task.get("id"),
                "status": "assignment_review_required",
                "deterministic_station": category,
                "local_recommended_station": local_review.get("recommended_primary_station"),
                "reason": "Deterministic profile and local planning reviewer disagree. Review before dispatch.",
            },
            None,
            2,
        )
    if assignment_status == "abstained":
        return _TaskOutcome(
            {
                "task_id": task.get("id"),
                "status": "coordinator_required",
                "reason": assignment.get("reason"),
            },
            None,
            2,
        )
    if assignment_status == "conditional" and not args.image:
        return _TaskOutcome(
            {
                "task_id": task.get("id"),
                "status": "input_required",
                "required_inputs": assignment.get("required_inputs", []),
                "reason": "Supply --image for the assigned vision station.",
            },
            None,
            2,
        )
    if category in GENERATION_STATION_OUTPUT_TYPES:
        code, visual_payload = _dispatch_visual_task(project, settings, task, assignment, args)
        return _TaskOutcome(
            {
                "task_id": task.get("id"),
                "model": model,
                "category": category,
                "exit_code": code,
                "result": visual_payload,
            },
            {
                "status": "coordinator_review_required" if code == 0 else "attention_required",
                "updated_at": utc_now(),
                "model": model,
                "category": category,
                "run_id": None,
                "work_artifact": str(artifact),
                "result_summary": None,
                "exit_code": code,
                "support_review_exit_code": None,
            },
            code,
        )
    if category not in STRENGTH_CASES or not isinstance(model, str) or not model:
        return _TaskOutcome(
            {
                "task_id": task.get("id"),
                "status": "no_eligible_model",
                "reason": assignment.get("reason"),
            },
            None,
            2,
        )
    task_text = (
        f"Sprint {task.get('id')}: {task.get('title')}\n"
        f"Objective: {task.get('objective')}\n"
        "Produce the maximum useful bounded local-worker result. Do not claim commands ran."
    )
    cook_args = argparse.Namespace(
        project=str(project),
        task=task_text,
        context_file=None,
        category=category,
        model=model,
        prefer_speed=False,
        allowed_file=_existing_plan_files(project, list(task.get("expected_files") or [])),
        forbidden_file=[],
        acceptance=(
            [
                "Provide concrete bounded guidance for coordinator action",
                "Map guidance to every sprint acceptance criterion",
            ]
            if task.get("task_profile", {}).get("local_scope") == "advisory"
            else list(task.get("acceptance_criteria") or [])
        ),
        test=[],
        context_note=[
            f"Dependencies: {', '.join(task.get('dependencies') or []) or 'none'}",
            f"Prior evidence: {'; '.join(task.get('evidence') or []) or 'none'}",
            *_dependency_handoff(task, ledger),
        ],
        exclude=["No unrelated sprint work", "Do not execute commands or modify files"],
        image=list(args.image),
        complexity=args.complexity,
        risk=args.risk,
        context_tokens=args.context_tokens,
        output_tokens=args.output_tokens,
        max_attempts=args.max_attempts,
        timeout=args.timeout,
        temperature=args.temperature,
        no_auto_split=False,
    )
    code, payload = _cook_core(cook_args)
    item_result = {
        "task_id": task.get("id"),
        "model": model,
        "category": category,
        "allowed_files": cook_args.allowed_file,
        "exit_code": code,
        "result": payload,
    }
    reviewer = next(
        (
            assignment for assignment in task.get("supporting_assignments", [])
            if isinstance(assignment, dict)
            and assignment.get("station") == "analysis"
            and assignment.get("model")
        ),
        None,
    )
    combined_code = code
    if code == 0 and payload and reviewer:
        review_code, review_payload = _local_support_review(
            project, settings, state, task, payload, str(reviewer["model"]), args,
        )
        item_result["support_review"] = review_payload
        item_result["support_review_exit_code"] = review_code
        combined_code = max(combined_code, review_code)
    run = _primary_run_from_payload(payload)
    result_summary = None
    if isinstance(run, dict) and isinstance(run.get("response"), dict):
        raw_summary = run["response"].get("result")
        if isinstance(raw_summary, str):
            result_summary = raw_summary[:4000]
    successful = item_result["exit_code"] == 0 and item_result.get("support_review_exit_code", 0) == 0
    needs_review = bool(assignment.get("review_required", True))
    ledger_entry = {
        "status": (
            "accepted"
            if successful and not needs_review
            else "coordinator_review_required"
            if successful
            else "attention_required"
        ),
        "updated_at": utc_now(),
        "model": model,
        "category": category,
        "run_id": run.get("run_id") if isinstance(run, dict) else None,
        "work_artifact": str(artifact),
        "result_summary": result_summary,
        "exit_code": item_result["exit_code"],
        "support_review_exit_code": item_result.get("support_review_exit_code"),
    }
    return _TaskOutcome(item_result, ledger_entry, combined_code)


def cmd_work(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    settings, _ = load_settings(project)
    state = ensure_state(project, settings.state_dir)
    plan_path = state / "orchestration" / "sprint-plan.json"
    if not plan_path.exists():
        print("No sprint plan. Run head-chef orchestrate first.", file=sys.stderr)
        return 2
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        tasks = plan.get("tasks", [])
        if not isinstance(tasks, list):
            raise ValueError("sprint plan tasks must be an array")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Invalid sprint plan: {exc}", file=sys.stderr)
        return 2

    ledger_path = state / "orchestration" / "work-ledger.json"
    try:
        ledger = _load_work_ledger(ledger_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Invalid work ledger: {exc}", file=sys.stderr)
        return 2
    task_map = {
        str(task.get("id")): task
        for task in tasks
        if isinstance(task, dict) and task.get("id")
    }
    for task_id in args.accept_task:
        task = task_map.get(task_id)
        record = ledger["tasks"].get(task_id)
        if task is None or not isinstance(record, dict) or record.get("status") != "coordinator_review_required":
            print(f"Cannot accept {task_id}: no coordinator-pending successful result.", file=sys.stderr)
            return 2
        record["status"] = "accepted"
        record["accepted_at"] = utc_now()
        record["accepted_by"] = "coordinator"
    if args.accept_task:
        ledger["updated_at"] = utc_now()
        atomic_write_json(ledger_path, ledger)

    completed = _completed_sprint_tasks(tasks, ledger)
    requested = set(args.task_id or [])
    selected = [
        task for task in tasks
        if isinstance(task, dict)
        and (
            task.get("status") in {"ready", "active", "in_progress"}
            or (task.get("status") is None and task.get("actionable") is True)
        )
        and str(task.get("id")) not in completed
        and all(str(dependency) in completed for dependency in task.get("dependencies") or [])
        and (not requested or task.get("id") in requested)
    ]
    if requested:
        missing = requested - {str(task.get("id")) for task in selected}
        if missing:
            print(f"Tasks are missing or not dependency-ready: {', '.join(sorted(missing))}", file=sys.stderr)
            return 2
    if not selected:
        if args.accept_task:
            _json({
                "status": "completed",
                "accepted_task_ids": list(args.accept_task),
                "ledger": str(ledger_path),
                "next_ready_task_ids": [],
                "task_count": 0,
                "results": [],
            })
            return 0
        print("No dependency-ready sprint tasks.", file=sys.stderr)
        return 2
    if not args.all_ready and not requested:
        selected = selected[:1]

    artifact = state / "orchestration" / "work-runs" / f"{compact_timestamp()}-{secrets.token_hex(4)}.json"
    max_workers = max(1, int(getattr(args, "parallel", 1) or 1))
    if max_workers > 1 and len(selected) > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [
                pool.submit(_dispatch_one_task, task, project, settings, state, ledger, artifact, args)
                for task in selected
            ]
            outcomes = [future.result() for future in futures]
    else:
        outcomes = [
            _dispatch_one_task(task, project, settings, state, ledger, artifact, args)
            for task in selected
        ]

    results: list[dict[str, Any]] = []
    final_code = 0
    for task, outcome in zip(selected, outcomes):
        results.append(outcome.item_result)
        final_code = max(final_code, outcome.code)
        if outcome.ledger_entry is not None:
            ledger["tasks"][str(task.get("id"))] = outcome.ledger_entry
    atomic_write_json(artifact, {
        "contract_version": CONTRACT_VERSION,
        "schema_version": "1.0",
        "created_at": utc_now(),
        "plan_path": str(plan_path),
        "results": results,
    })
    ledger["updated_at"] = utc_now()
    atomic_write_json(ledger_path, ledger)
    completed_after = _completed_sprint_tasks(tasks, ledger)
    next_ready = [
        str(task.get("id"))
        for task in tasks
        if isinstance(task, dict)
        and (
            task.get("status") in {"ready", "active", "in_progress"}
            or (task.get("status") is None and task.get("actionable") is True)
        )
        and str(task.get("id")) not in completed_after
        and all(str(dependency) in completed_after for dependency in task.get("dependencies") or [])
    ]
    _json({
        "status": "completed" if final_code == 0 else "attention_required",
        "artifact": str(artifact),
        "task_count": len(results),
        "results": results,
        "ledger": str(ledger_path),
        "next_ready_task_ids": next_ready,
    })
    return final_code


def _local_support_review(
    project: Path,
    settings: Settings,
    state: Path,
    task: dict[str, Any],
    primary_payload: dict[str, Any],
    reviewer_model: str,
    args: argparse.Namespace,
) -> tuple[int, dict[str, Any] | None]:
    primary_run = primary_payload.get("run")
    if not isinstance(primary_run, dict):
        synthesis = primary_payload.get("synthesis")
        primary_run = synthesis.get("run") if isinstance(synthesis, dict) else None
    if not isinstance(primary_run, dict):
        return 2, {"status": "skipped", "reason": "primary run evidence unavailable"}
    context = json.dumps({
        "task_id": task.get("id"),
        "objective": task.get("objective"),
        "acceptance_criteria": task.get("acceptance_criteria", []),
        "primary_run": primary_run,
    }, ensure_ascii=True)
    try:
        client = _client(settings)
        profiles = _profiles(client, settings)
        decision = route(
            RouteRequest(
                task=f"Independently review local result for sprint {task.get('id')}",
                context_text=context,
                required_capability="analysis",
                manual_model=reviewer_model,
                prefer_quality=True,
            ),
            profiles,
            reserved_output_tokens=args.output_tokens or settings.reserved_output_tokens,
            safety_margin_tokens=settings.safety_margin_tokens,
            benchmark_path=_evidence_path(project, settings, "benchmarks/latest.json"),
            outcome_path=_evidence_path(project, settings, "outcomes.jsonl"),
        )
        if not decision.selected_model:
            return 2, {"status": "skipped", "reason": decision.explanation}
        review_job = create_job_card(
            project=str(project),
            task=(
                f"Independently review the supplied primary local-worker result for {task.get('id')}. "
                "Re-check every original acceptance criterion. Find unsupported claims, missed requirements, "
                "unsafe suggestions, and concrete improvements. Explicitly state when no unsupported claims exist."
            ),
            decision=decision,
            acceptance_criteria=list(task.get("acceptance_criteria") or []),
            context_text=context,
            exclusions=["Do not invent project evidence", "Do not approve your own output"],
        )
        profile = next(profile for profile in profiles if profile.name == decision.selected_model)
        review_job.model_digest = profile.digest
        review_job.context_limit_tokens = min(
            args.context_tokens or profile.context_tokens,
            profile.context_tokens,
        )
        review_job.output_limit_tokens = args.output_tokens or settings.reserved_output_tokens
        review_job.max_attempts = args.max_attempts
        review_job.fallback_models = [
            candidate.model
            for candidate in decision.candidates
            if not candidate.rejected and candidate.model != decision.selected_model
        ][:2]
        review_path = save_job_card(review_job, state / "jobs")
        code, payload, attempts = _dispatch_saved_job(str(review_path), args)
        return code, {
            "status": "completed" if code == 0 else "failed",
            "job_path": str(review_path),
            "attempts": attempts,
            "result": payload,
        }
    except (OllamaError, OSError, ValueError) as exc:
        return 1, {"status": "failed", "error": str(exc)}

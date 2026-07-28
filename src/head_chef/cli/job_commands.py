"""Job card lifecycle: create, dispatch, review, and the composite cook/split-synthesis flow.

Every command that other commands compose with (job/dispatch/cook) is split into a `_*_core`
function that does the real work and returns `(exit_code, payload | None)` without printing,
and a thin `cmd_*` wrapper that calls the core and prints its payload. Composing code (cook,
work, visual-run's verification step, ...) calls the core functions directly instead of
capturing another command's stdout — required for thread-safe concurrent dispatch, since
`contextlib.redirect_stdout` swaps a single process-global `sys.stdout` and is not safe to use
from multiple threads at once.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import secrets
import sys
import time
from typing import Any

from ..config import load_settings
from ..context_packager import package_context, render_package, split_context
from ..executors import execute_job
from ..jobs import create_job_card, load_job_card, render_worker_prompt, save_job_card
from ..ollama import OllamaError
from ..router import RouteRequest, route
from ..runs import RunRecord, next_attempt, save_run
from ..storage import append_jsonl, atomic_create_json, ensure_state, utc_now
from ..token_governor import evaluate_budget
from ..verification import parse_worker_output, verify_output, validate_review_status

from ._shared import (
    _append_outcome,
    _client,
    _decision,
    _evidence_path,
    _json,
    _profiles,
    _public_job,
    _public_run,
    _safe_project_text,
    _worker_run_succeeded,
)


def _job_core(args: argparse.Namespace) -> tuple[int, dict[str, Any] | None]:
    project = Path(args.project).resolve()
    settings, root = load_settings(project)
    state = ensure_state(root, settings.state_dir)
    try:
        profiles = _profiles(_client(settings), settings)
        decision = _decision(args, profiles, settings, root)
    except (OllamaError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1, None

    context_text = ""
    if args.context_file:
        try:
            context_text = _safe_project_text(project, args.context_file)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1, None
    if args.allowed_file:
        try:
            packaged = package_context(Path(args.project), args.allowed_file, args.forbidden_file)
            context_text = render_package(packaged)
        except (OSError, ValueError) as exc:
            print(f"Cannot package context: {exc}", file=sys.stderr)
            return 2, None
        decision = route(
            RouteRequest(
                task=args.task,
                context_text=context_text,
                required_capability=args.category,
                manual_model=args.model,
                prefer_quality=not args.prefer_speed,
            ),
            profiles,
            reserved_output_tokens=settings.reserved_output_tokens,
            safety_margin_tokens=settings.safety_margin_tokens,
            benchmark_path=_evidence_path(root, settings, "benchmarks/latest.json"),
            outcome_path=_evidence_path(root, settings, "outcomes.jsonl"),
        )

    job = create_job_card(
        project=str(project),
        task=args.task,
        decision=decision,
        allowed_files=args.allowed_file,
        forbidden_files=args.forbidden_file,
        acceptance_criteria=args.acceptance,
        test_commands=args.test,
        context_notes=args.context_note,
        context_text=context_text,
        exclusions=args.exclude,
    )
    job.input_images = args.image
    selected_profile = next((profile for profile in profiles if profile.name == job.selected_model), None)
    job.model_digest = selected_profile.digest if selected_profile else ""
    job.task_profile = {
        "task_type": decision.category,
        "modalities": ["image", "text"] if args.image else ["text"],
        "complexity": args.complexity,
        "risk": args.risk,
        "output": "json",
    }
    requested_context = getattr(args, "context_tokens", None)
    if requested_context is not None and requested_context < 512:
        print("--context-tokens must be at least 512.", file=sys.stderr)
        return 2, None
    requested_output = getattr(args, "output_tokens", None)
    if requested_output is not None and requested_output < 1:
        print("--output-tokens must be positive.", file=sys.stderr)
        return 2, None
    if selected_profile:
        if requested_context and requested_context > selected_profile.context_tokens:
            print(
                f"Requested context {requested_context} exceeds model maximum {selected_profile.context_tokens}.",
                file=sys.stderr,
            )
            return 2, None
        estimated = decision.budget.estimated_input_tokens if decision.budget else 0
        minimum = estimated + (requested_output or settings.reserved_output_tokens) + settings.safety_margin_tokens
        if requested_context and requested_context < minimum:
            override_budget = evaluate_budget(
                context_text or args.task,
                requested_context,
                requested_output or settings.reserved_output_tokens,
                settings.safety_margin_tokens,
            )
            if args.allowed_file and override_budget.status == "split":
                decision.budget = override_budget
                decision.requires_split = True
                job.requires_split = True
            else:
                print(
                    f"Requested context {requested_context} is below safe task need {minimum}; package files to allow splitting.",
                    file=sys.stderr,
                )
                return 2, None
        job.context_limit_tokens = requested_context or min(
            selected_profile.context_tokens,
            max(settings.default_context_tokens, minimum),
        )
    job.output_limit_tokens = requested_output or settings.reserved_output_tokens
    job.max_attempts = max(1, getattr(args, "max_attempts", 2))
    job.fallback_models = [
        candidate.model
        for candidate in decision.candidates
        if not candidate.rejected and candidate.model != job.selected_model
    ][:2]
    path = save_job_card(job, state / "jobs")
    child_paths: list[str] = []
    if job.requires_split and args.allowed_file and decision.budget:
        max_chars = max(1, int(decision.budget.usable_input_tokens * 4 * 0.70))
        try:
            chunks = split_context(packaged, max_chars)
        except ValueError as exc:
            print(f"Cannot split context safely: {exc}", file=sys.stderr)
            return 2, None
        child_ids: list[str] = []
        for index, chunk in enumerate(chunks, 1):
            child = create_job_card(
                project=str(project),
                task=f"{args.task} [context part {index}/{len(chunks)}]",
                decision=decision,
                allowed_files=[item.path for item in chunk],
                forbidden_files=args.forbidden_file,
                acceptance_criteria=args.acceptance,
                test_commands=args.test,
                context_notes=args.context_note,
                context_text=render_package(chunk),
                exclusions=args.exclude,
            )
            child.parent_job_id = job.id
            child.model_digest = job.model_digest
            child.requires_split = False
            child.task_profile = dict(job.task_profile)
            child.context_limit_tokens = job.context_limit_tokens
            child.output_limit_tokens = job.output_limit_tokens
            child.max_attempts = job.max_attempts
            child.fallback_models = list(job.fallback_models)
            child_ids.append(child.id)
            child_paths.append(str(save_job_card(child, state / "jobs")))
        synthesis = create_job_card(
            project=str(project),
            task=f"Synthesize child results for: {args.task}",
            decision=decision,
            acceptance_criteria=args.acceptance,
            exclusions=["Do not add claims absent from child results"],
        )
        synthesis.parent_job_id = job.id
        synthesis.model_digest = job.model_digest
        synthesis.dependencies = child_ids
        synthesis.requires_split = False
        synthesis.task_profile = dict(job.task_profile)
        synthesis.context_limit_tokens = job.context_limit_tokens
        synthesis.output_limit_tokens = job.output_limit_tokens
        synthesis.max_attempts = job.max_attempts
        synthesis.fallback_models = list(job.fallback_models)
        child_paths.append(str(save_job_card(synthesis, state / "jobs")))
    payload = {"job": _public_job(job), "path": str(path), "child_job_paths": child_paths}
    return (0 if job.selected_model else 2), payload


def cmd_job(args: argparse.Namespace) -> int:
    code, payload = _job_core(args)
    if payload is not None:
        _json(payload)
    return code


def _dispatch_core(args: argparse.Namespace) -> tuple[int, dict[str, Any] | None]:
    path = Path(args.job).resolve()
    try:
        raw_job = json.loads(path.read_text(encoding="utf-8"))
        project_value = raw_job.get("project")
        if not isinstance(project_value, str) or not project_value:
            raise ValueError("Job card has no project root")
        settings, root = load_settings(Path(project_value))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"Cannot resolve job project: {exc}", file=sys.stderr)
        return 1, None
    state = ensure_state(root, settings.state_dir)
    jobs_root = (state / "jobs").resolve()
    try:
        path.relative_to(jobs_root)
    except ValueError:
        print("Job card must be inside project .head-chef/jobs.", file=sys.stderr)
        return 2, None
    try:
        job = load_job_card(path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"Cannot load job card: {exc}", file=sys.stderr)
        return 1, None

    if not job.selected_model:
        print("Job has no selected local model.", file=sys.stderr)
        return 2, None
    if job.requires_split:
        print("Job is marked oversized. Dispatch generated child jobs instead.", file=sys.stderr)
        return 2, None

    prompt = render_worker_prompt(job) if job.category != "embedding" else None
    client = _client(settings)
    attempt = next_attempt(state / "runs", job.id)
    started = time.perf_counter()
    try:
        current_profile = next(
            (profile for profile in _profiles(client, settings) if profile.name == job.selected_model),
            None,
        )
        if current_profile is None:
            raise ValueError(f"Selected model is no longer installed: {job.selected_model}")
        if job.model_digest and current_profile.digest != job.model_digest:
            raise ValueError(
                f"Model digest changed for {job.selected_model}; reroute job before dispatch."
            )
        execution = execute_job(
            client, job,
            temperature=args.temperature,
            timeout_seconds=args.timeout or settings.request_timeout_seconds,
        )
        response = execution.response
    except (OllamaError, ValueError) as exc:
        failed = RunRecord(
            job.id, job.selected_model, job.category, attempt, False,
            model_digest=job.model_digest, prompt=prompt, error=str(exc),
        )
        save_run(failed, state / "runs")
        append_jsonl(
            state / "outcomes.jsonl",
            {
                "created_at": utc_now(), "job_id": job.id, "model": job.selected_model,
                "model_digest": job.model_digest,
                "category": job.category, "ok": False,
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "reason": type(exc).__name__,
            },
        )
        print(str(exc), file=sys.stderr)
        return 1, None

    parsed = None
    errors: list[str] = []
    if job.category != "embedding":
        parsed, errors = parse_worker_output(response.content)
    else:
        parsed = {"embedding_count": len(response.raw.get("embeddings", []))}
    verification = verify_output(
        parsed if job.category != "embedding" else {
            "result": "embedding generated", "changed_files": [], "assumptions": [], "risks": [],
            "tests_recommended": [],
            "acceptance_check": [
                {"criterion": criterion, "met": True, "evidence": f"{parsed['embedding_count']} embedding vector(s) returned"}
                for criterion in job.acceptance_criteria
            ],
            "confidence": 1.0, "blockers": [],
        },
        errors,
        job.acceptance_criteria,
        coordinator_review_required=job.coordinator_review_required,
    )
    run_ok = _worker_run_succeeded(parsed, errors, verification.acceptance_complete)
    run = RunRecord(
        job.id, job.selected_model, execution.executor, attempt, run_ok,
        model_digest=job.model_digest, prompt=prompt,
        response=parsed if parsed is not None else response.content,
        validation_errors=errors,
        metrics={
            "total_duration": response.raw.get("total_duration"),
            "load_duration": response.raw.get("load_duration"),
            "prompt_eval_count": response.raw.get("prompt_eval_count"),
            "eval_count": response.raw.get("eval_count"),
            "done_reason": response.raw.get("done_reason"),
        },
        review_status=verification.status,
        review_notes=verification.notes,
    )
    run_path = save_run(run, state / "runs")
    _append_outcome(state / "outcomes.jsonl", {
        "created_at": utc_now(), "job_id": job.id, "run_id": run.run_id,
        "model": job.selected_model, "category": job.category, "ok": run_ok,
        "model_digest": job.model_digest,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "review_status": verification.status,
    })
    payload = {"run_path": str(run_path), "run": _public_run(run), "verification": verification.to_dict()}
    return (0 if run_ok else 4 if errors else 5), payload


def cmd_dispatch(args: argparse.Namespace) -> int:
    code, payload = _dispatch_core(args)
    if payload is not None:
        _json(payload)
    return code


def cmd_review(args: argparse.Namespace) -> int:
    validate_review_status(args.status)
    path = Path(args.run).resolve()
    settings, root = load_settings()
    runs_root = (root / settings.state_dir / "runs").resolve()
    try:
        path.relative_to(runs_root)
    except ValueError:
        print("Run must be inside project .head-chef/runs.", file=sys.stderr)
        return 2
    data = json.loads(path.read_text(encoding="utf-8"))
    review = {
        "schema_version": "2.0",
        "created_at": utc_now(),
        "run_id": data.get("run_id"),
        "job_id": data.get("job_id"),
        "status": args.status,
        "note": args.note or "",
    }
    run_id = str(data.get("run_id"))
    review_number = len(list(path.parent.glob(f"{run_id}-review-*.json"))) + 1
    review_path = path.parent / f"{run_id}-review-{review_number}.json"
    atomic_create_json(review_path, review)
    _json({"review_path": str(review_path), "review": review})
    return 0


def _cook_core(args: argparse.Namespace) -> tuple[int, dict[str, Any] | None]:
    job_code, job_result = _job_core(args)
    if job_code != 0 or not job_result:
        return job_code, ({"stage": "job", **job_result} if job_result else None)
    child_paths = job_result.get("child_job_paths", [])
    if child_paths:
        if not args.no_auto_split:
            return _cook_split_jobs_core(args, job_result, child_paths)
        payload = {
            "status": "split",
            "stage": "job",
            "job": job_result["job"],
            "job_path": job_result["path"],
            "child_job_paths": child_paths,
            "next_action": "Dispatch independent child jobs, then synthesis job after dependencies complete.",
        }
        return 3, payload
    dispatch_args = argparse.Namespace(
        job=job_result["path"],
        timeout=args.timeout,
        temperature=args.temperature,
    )
    dispatch_code = 1
    dispatch_result = None
    recovery: list[dict[str, Any]] = []
    fallback_models = list(job_result["job"].get("fallback_models", []))
    for attempt_number in range(1, max(1, args.max_attempts) + 1):
        dispatch_code, dispatch_result = _dispatch_core(dispatch_args)
        recovery.append({
            "attempt": attempt_number,
            "job_id": job_result["job"]["id"],
            "model": job_result["job"].get("selected_model"),
            "status": "ok" if dispatch_code == 0 else "retry" if attempt_number < args.max_attempts else "failed",
            "exit_code": dispatch_code,
        })
        if dispatch_code == 0:
            break
        if (dispatch_result is not None or attempt_number > 1) and fallback_models:
            fallback = fallback_models.pop(0)
            recovery_args = argparse.Namespace(**{**vars(args), "model": fallback})
            recovery_job_code, recovery_job = _job_core(recovery_args)
            if recovery_job_code == 0 and recovery_job and not recovery_job.get("child_job_paths"):
                job_result = recovery_job
                dispatch_args.job = recovery_job["path"]
                recovery[-1]["next_model"] = fallback
            else:
                recovery[-1]["fallback_rejected"] = fallback
    if len(recovery) > 1:
        settings, root = load_settings(Path(args.project))
        append_jsonl(root / settings.state_dir / "recovery.jsonl", {
            "created_at": utc_now(),
            "job_id": job_result["job"]["id"],
            "attempts": recovery,
        })
    if dispatch_result:
        payload = {
            "status": "completed" if dispatch_code == 0 else "verification_failed",
            "stage": "dispatch",
            "job": job_result["job"],
            "recovery": recovery,
            **dispatch_result,
        }
        return dispatch_code, payload
    if recovery:
        payload = {
            "status": "failed",
            "stage": "dispatch",
            "job": job_result["job"],
            "recovery": recovery,
        }
        return dispatch_code, payload
    return dispatch_code, None


def cmd_cook(args: argparse.Namespace) -> int:
    code, payload = _cook_core(args)
    if payload is not None:
        _json(payload)
    return code


def _dispatch_saved_job(path: str, args: argparse.Namespace) -> tuple[int, dict[str, Any] | None, list[dict[str, Any]]]:
    result: dict[str, Any] | None = None
    code = 1
    attempts: list[dict[str, Any]] = []
    current_path = Path(path)
    current_job = load_job_card(current_path)
    fallback_models = list(current_job.fallback_models)
    dispatch_args = argparse.Namespace(job=str(current_path), timeout=args.timeout, temperature=args.temperature)
    for number in range(1, max(1, args.max_attempts) + 1):
        code, result = _dispatch_core(dispatch_args)
        attempts.append({
            "attempt": number,
            "job_id": current_job.id,
            "model": current_job.selected_model,
            "exit_code": code,
        })
        if code == 0:
            break
        if result is not None and fallback_models and number < args.max_attempts:
            settings, _ = load_settings(Path(current_job.project))
            profiles = _profiles(_client(settings), settings)
            fallback = next(
                (
                    profile for name in fallback_models
                    for profile in profiles
                    if profile.name == name
                    and not profile.is_cloud
                    and current_job.category in profile.capabilities
                    and not (profile.is_embedding_only and current_job.category != "embedding")
                ),
                None,
            )
            if fallback:
                recovered = load_job_card(current_path)
                recovered.id = f"{recovered.id}-fallback-{secrets.token_hex(4)}"
                recovered.selected_model = fallback.name
                recovered.model_digest = fallback.digest
                recovered.context_limit_tokens = min(
                    recovered.context_limit_tokens or fallback.context_tokens,
                    fallback.context_tokens,
                )
                recovered.fallback_models = [name for name in fallback_models if name != fallback.name]
                current_path = save_job_card(recovered, current_path.parent)
                current_job = recovered
                dispatch_args.job = str(current_path)
                fallback_models = list(recovered.fallback_models)
                attempts[-1]["next_model"] = fallback.name
    return code, result, attempts


def _cook_split_jobs_core(
    args: argparse.Namespace, parent: dict[str, Any], child_paths: list[str],
) -> tuple[int, dict[str, Any] | None]:
    settings, root = load_settings(Path(parent["job"]["project"]))
    state = ensure_state(root, settings.state_dir)
    child_evidence: list[dict[str, Any]] = []
    child_runs: list[dict[str, Any]] = []
    for child_path in child_paths[:-1]:
        child = load_job_card(Path(child_path))
        code, result, attempts = _dispatch_saved_job(child_path, args)
        child_runs.append({
            "job_id": child.id,
            "model": child.selected_model,
            "exit_code": code,
            "attempts": attempts,
            "run_path": result.get("run_path") if result else None,
        })
        if code != 0 or not result:
            payload = {
                "status": "child_failed",
                "stage": "split_dispatch",
                "job": parent["job"],
                "child_runs": child_runs,
                "next_action": "Inspect immutable child attempts; rerun cook after correcting local runtime/model issue.",
            }
            return code or 1, payload
        child_evidence.append({
            "job_id": child.id,
            "model": child.selected_model,
            "response": result["run"]["response"],
            "verification": result["verification"],
        })

    placeholder = load_job_card(Path(child_paths[-1]))
    synthesis_context = json.dumps(
        {"source": "immutable schema-validated local-worker attempts", "children": child_evidence},
        ensure_ascii=True,
    )
    context_limit = placeholder.context_limit_tokens or settings.default_context_tokens
    synthesis_budget = evaluate_budget(
        synthesis_context,
        context_limit,
        placeholder.output_limit_tokens or settings.reserved_output_tokens,
        settings.safety_margin_tokens,
    )
    if synthesis_budget.status == "split":
        payload = {
            "status": "synthesis_oversized",
            "stage": "split_synthesis",
            "job": parent["job"],
            "child_runs": child_runs,
            "budget": synthesis_budget.to_dict(),
            "next_action": "Reduce child scope or raise task context within selected model metadata.",
        }
        return 3, payload
    placeholder.id = f"{placeholder.id}-ready-{secrets.token_hex(4)}"
    placeholder.context_text = synthesis_context
    placeholder.context_notes.append("Synthesize only the supplied immutable child run evidence.")
    synthesis_path = save_job_card(placeholder, state / "jobs")
    code, result, attempts = _dispatch_saved_job(str(synthesis_path), args)
    payload = {
        "status": "completed" if code == 0 else "synthesis_failed",
        "stage": "split_synthesis",
        "job": parent["job"],
        "child_runs": child_runs,
        "synthesis_job_path": str(synthesis_path),
        "synthesis_attempts": attempts,
        "synthesis": result,
    }
    return code, payload

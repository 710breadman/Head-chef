from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import platform
import secrets
import sys
import time
from typing import Any

from .benchmark import STRENGTH_CASES, run_benchmarks
from .config import Settings, load_settings, write_default_config
from .jobs import create_job_card, load_job_card, render_worker_prompt, save_job_card
from .models import ModelProfile, profile_from_ollama
from .ollama import OllamaClient, OllamaError
from .router import RouteRequest, route
from .storage import append_jsonl, atomic_write_json, compact_timestamp, ensure_state, utc_now
from .contracts import CONTRACT_VERSION
from .executors import execute_job
from .verification import parse_worker_output, verify_output, validate_review_status
from .runs import RunRecord, next_attempt, save_run
from .registry import apply_overrides, load_overrides, reconcile_registry, save_registry
from .context_packager import package_context, render_package, split_context
from .kitchen import build_kitchen
from .orchestration import add_local_phase_analysis, build_sprint_plan, discover_sprint_file, load_sprint_tasks
from .token_governor import evaluate_budget
from .comfyui import (
    ComfyUIClient,
    ComfyUIError,
    start_portable_comfyui,
    stop_portable_comfyui,
)
from .visual import (
    create_visual_job,
    free_vram_mb,
    load_approved_workflow,
    load_visual_job,
    model_usability,
    run_visual_job,
    save_visual_job,
    validate_model_parameters,
    workflow_root,
)


def _json(data: Any) -> None:
    if not isinstance(data, dict):
        data = {"contract_version": CONTRACT_VERSION, "data": data}
    elif "contract_version" not in data:
        data = {"contract_version": CONTRACT_VERSION, **data}
    print(json.dumps(data, indent=2, ensure_ascii=True))


def _public_run(run: RunRecord) -> dict[str, Any]:
    value = run.to_dict()
    value.pop("prompt", None)
    return value


def _public_job(job) -> dict[str, Any]:
    value = job.to_dict()
    if value.get("context_text"):
        value["context_text"] = "[stored in private job artifact]"
    return value


def _client(settings: Settings) -> OllamaClient:
    return OllamaClient(settings.ollama_url, settings.request_timeout_seconds, settings.request_retries)


def _profiles(client: OllamaClient, settings: Settings) -> list[ModelProfile]:
    profiles: list[ModelProfile] = []
    for raw in client.list_models():
        name = str(raw.get("name") or raw.get("model") or "")
        show: dict[str, Any] = {}
        if name:
            try:
                show = client.show_model(name)
            except OllamaError:
                pass
        profiles.append(profile_from_ollama(raw, show, settings.default_context_tokens))
    return profiles


def _global_evidence_path(relative: str) -> Path | None:
    global_state = os.getenv("HEAD_CHEF_GLOBAL_STATE")
    if global_state:
        global_root = Path(global_state).resolve()
        global_path = (global_root / relative).resolve()
        try:
            global_path.relative_to(global_root)
        except ValueError:
            return None
        return global_path
    return None


def _evidence_path(root: Path, settings: Settings, relative: str) -> Path:
    local = root / settings.state_dir / relative
    if local.exists():
        return local
    global_path = _global_evidence_path(relative)
    if global_path and global_path.exists():
        return global_path
    return local


def _append_outcome(local_path: Path, outcome: dict[str, Any]) -> None:
    append_jsonl(local_path, outcome)
    global_path = _global_evidence_path("outcomes.jsonl")
    if global_path and global_path != local_path.resolve():
        try:
            append_jsonl(global_path, outcome)
        except OSError as exc:
            print(f"Warning: could not update global routing evidence: {exc}", file=sys.stderr)


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    root.mkdir(parents=True, exist_ok=True)
    config_path = write_default_config(root)
    state = ensure_state(root)
    print(f"Initialized Head Chef state at {state}")
    print(f"Config: {config_path}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    try:
        settings, root = load_settings()
    except ValueError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2

    report: dict[str, Any] = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "project_root": str(root),
        "ollama_url": settings.ollama_url,
        "ollama_reachable": False,
        "ollama_version": None,
        "installed_models": 0,
        "warnings": [],
    }
    try:
        client = _client(settings)
        report["ollama_version"] = client.version()
        models = client.list_models()
        report["ollama_reachable"] = True
        report["installed_models"] = len(models)
        if not models:
            report["warnings"].append("Ollama is reachable, but no local models are installed.")
    except OllamaError as exc:
        report["warnings"].append(str(exc))

    if sys.version_info < (3, 11):
        report["warnings"].append("Python 3.11 or newer is required.")
    _json(report)
    return 0 if report["ollama_reachable"] else 1


def cmd_models(args: argparse.Namespace) -> int:
    settings, root = load_settings()
    try:
        profiles = _profiles(_client(settings), settings)
        overrides = load_overrides(root / settings.state_dir / "model-overrides.json")
        profiles = [apply_overrides(profile, overrides.get(profile.name, {})) for profile in profiles]
        save_registry(root / settings.state_dir / "registry.json", profiles)
    except OllamaError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    data = [profile.to_dict() for profile in profiles]
    if args.json:
        _json(data)
    else:
        if not data:
            print("No installed Ollama models found.")
            return 0
        for profile in profiles:
            caps = ", ".join(sorted(profile.capabilities))
            print(
                f"{profile.name} | {profile.parameter_size} | {profile.quantization} | "
                f"ctx={profile.context_tokens} | {caps}"
            )
    return 0


def _safe_project_text(project: Path, value: str) -> str:
    raw = Path(value)
    if raw.is_absolute() or ".." in raw.parts or ":" in value:
        raise ValueError("Context file must be project-relative")
    path = (project.resolve() / raw).resolve()
    try:
        path.relative_to(project.resolve())
    except ValueError as exc:
        raise ValueError("Context file escapes project root") from exc
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Cannot read context file {path}: {exc}") from exc


def _context_text(args: argparse.Namespace, project: Path | None = None) -> str:
    pieces = [getattr(args, "task", "")]
    path_value = getattr(args, "context_file", None)
    if path_value:
        pieces.append(_safe_project_text(project or Path.cwd(), path_value))
    return "\n\n".join(piece for piece in pieces if piece)


def _decision(args: argparse.Namespace, profiles: list[ModelProfile], settings: Settings, root: Path):
    context = _context_text(args, root)
    benchmark_path = _evidence_path(root, settings, "benchmarks/latest.json")
    return route(
        RouteRequest(
            task=args.task,
            context_text=context,
            required_capability=getattr(args, "category", None) or ("vision" if getattr(args, "image", None) else None),
            manual_model=getattr(args, "model", None),
            prefer_quality=not getattr(args, "prefer_speed", False),
        ),
        profiles,
        reserved_output_tokens=settings.reserved_output_tokens,
        safety_margin_tokens=settings.safety_margin_tokens,
        benchmark_path=benchmark_path,
        outcome_path=_evidence_path(root, settings, "outcomes.jsonl"),
    )


def cmd_route(args: argparse.Namespace) -> int:
    settings, root = load_settings()
    try:
        profiles = _profiles(_client(settings), settings)
        decision = _decision(args, profiles, settings, root)
    except (OllamaError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _json(decision.to_dict())
    return 0 if decision.selected_model else 2


def cmd_budget(args: argparse.Namespace) -> int:
    settings, _ = load_settings()
    try:
        text = _context_text(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    result = evaluate_budget(
        text,
        args.context_tokens or settings.default_context_tokens,
        settings.reserved_output_tokens,
        settings.safety_margin_tokens,
    )
    _json(result.to_dict())
    return 0


def cmd_job(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    settings, root = load_settings(project)
    state = ensure_state(root, settings.state_dir)
    try:
        profiles = _profiles(_client(settings), settings)
        decision = _decision(args, profiles, settings, root)
    except (OllamaError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    context_text = ""
    if args.context_file:
        try:
            context_text = _safe_project_text(project, args.context_file)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    if args.allowed_file:
        try:
            packaged = package_context(Path(args.project), args.allowed_file, args.forbidden_file)
            context_text = render_package(packaged)
        except (OSError, ValueError) as exc:
            print(f"Cannot package context: {exc}", file=sys.stderr)
            return 2
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
        return 2
    requested_output = getattr(args, "output_tokens", None)
    if requested_output is not None and requested_output < 1:
        print("--output-tokens must be positive.", file=sys.stderr)
        return 2
    if selected_profile:
        if requested_context and requested_context > selected_profile.context_tokens:
            print(
                f"Requested context {requested_context} exceeds model maximum {selected_profile.context_tokens}.",
                file=sys.stderr,
            )
            return 2
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
                return 2
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
            return 2
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
    _json({"job": _public_job(job), "path": str(path), "child_job_paths": child_paths})
    return 0 if job.selected_model else 2


def cmd_dispatch(args: argparse.Namespace) -> int:
    path = Path(args.job).resolve()
    try:
        raw_job = json.loads(path.read_text(encoding="utf-8"))
        project_value = raw_job.get("project")
        if not isinstance(project_value, str) or not project_value:
            raise ValueError("Job card has no project root")
        settings, root = load_settings(Path(project_value))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"Cannot resolve job project: {exc}", file=sys.stderr)
        return 1
    state = ensure_state(root, settings.state_dir)
    jobs_root = (state / "jobs").resolve()
    try:
        path.relative_to(jobs_root)
    except ValueError:
        print("Job card must be inside project .head-chef/jobs.", file=sys.stderr)
        return 2
    try:
        job = load_job_card(path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"Cannot load job card: {exc}", file=sys.stderr)
        return 1

    if not job.selected_model:
        print("Job has no selected local model.", file=sys.stderr)
        return 2
    if job.requires_split:
        print("Job is marked oversized. Dispatch generated child jobs instead.", file=sys.stderr)
        return 2

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
        failed_path = save_run(failed, state / "runs")
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
        return 1

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
    _json({"run_path": str(run_path), "run": _public_run(run), "verification": verification.to_dict()})
    return 0 if run_ok else 4 if errors else 5


def _worker_run_succeeded(
    parsed: object,
    validation_errors: list[str],
    acceptance_complete: bool,
) -> bool:
    blockers = parsed.get("blockers", []) if isinstance(parsed, dict) else []
    return not validation_errors and acceptance_complete and not blockers


def cmd_review(args: argparse.Namespace) -> int:
    import json
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
    from .storage import atomic_create_json
    atomic_create_json(review_path, review)
    _json({"review_path": str(review_path), "review": review})
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve() if getattr(args, "project", None) else None
    settings, root = load_settings(project)
    state = ensure_state(root, settings.state_dir)
    lock_root = _global_evidence_path("") or state
    lock_path = lock_root / "refresh.lock"
    try:
        lock_fd = _acquire_refresh_lock(lock_path)
    except FileExistsError:
        print(f"Another Head Chef refresh is running: {lock_path}", file=sys.stderr)
        return 2
    try:
        return _cmd_refresh_unlocked(args, settings, root, state)
    finally:
        os.close(lock_fd)
        lock_path.unlink(missing_ok=True)


def _acquire_refresh_lock(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, f"{os.getpid()}\n{utc_now()}\n".encode("utf-8"))
            return descriptor
        except FileExistsError:
            if attempt or _refresh_lock_is_live(path):
                raise
            path.unlink(missing_ok=True)
    raise FileExistsError(path)


def _refresh_lock_is_live(path: Path) -> bool:
    try:
        pid = int(path.read_text(encoding="utf-8").splitlines()[0])
        if os.name == "nt":
            import ctypes

            process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not process:
                return False
            ctypes.windll.kernel32.CloseHandle(process)
            return True
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, IndexError):
        return False


def _cmd_refresh_unlocked(
    args: argparse.Namespace,
    settings: Settings,
    root: Path,
    state: Path,
) -> int:
    try:
        client = _client(settings)
        profiles = _profiles(client, settings)
        overrides = load_overrides(_evidence_path(root, settings, "model-overrides.json"))
        profiles = [apply_overrides(profile, overrides.get(profile.name, {})) for profile in profiles]
        global_registry = _global_evidence_path("registry.json")
        registry_path = global_registry or (state / "registry.json")
        changes = reconcile_registry(registry_path, profiles)
        if registry_path != state / "registry.json":
            save_registry(state / "registry.json", profiles)

        changed_names = set(changes["new"] + changes["updated"])
        evaluation: dict[str, Any] | None = None
        skipped_strengths: list[dict[str, str]] = []
        benchmark_path = _global_evidence_path("benchmarks/latest.json") or state / "benchmarks" / "latest.json"
        evaluated = _evaluated_strengths(benchmark_path)
        requested_evaluation_models = set(getattr(args, "model", []) or [])
        pending_assignments: dict[str, set[str]] = {}
        for profile in profiles:
            if profile.is_cloud or (
                requested_evaluation_models and profile.name not in requested_evaluation_models
            ):
                continue
            strengths = set(profile.capabilities) & set(STRENGTH_CASES)
            missing_strengths = {
                category
                for category in strengths
                if (profile.name, profile.digest, category) not in evaluated
            }
            if profile.name in changed_names:
                missing_strengths |= strengths
            if "vision" in missing_strengths and not args.image:
                missing_strengths.remove("vision")
                skipped_strengths.append({
                    "model": profile.name, "category": "vision", "reason": "--image required",
                })
            if missing_strengths:
                pending_assignments[profile.name] = missing_strengths
        pending_profiles = [
            profile for profile in profiles
            if profile.name in pending_assignments
        ]
        if pending_profiles and not args.no_evaluate:
            benchmark_client = OllamaClient(
                settings.ollama_url,
                settings.request_timeout_seconds,
                request_retries=0,
            )
            evaluation = run_benchmarks(
                benchmark_client,
                pending_profiles,
                benchmark_path,
                args.timeout or settings.request_timeout_seconds,
                categories=set(STRENGTH_CASES),
                vision_image=Path(args.image).resolve() if args.image else None,
                assignments=pending_assignments,
            )
        kitchen = build_kitchen(
            [profile for profile in profiles if not profile.is_cloud],
            benchmark_path=benchmark_path,
            outcome_path=_evidence_path(root, settings, "outcomes.jsonl"),
        )
        atomic_write_json(state / "kitchen.json", kitchen)
    except (OllamaError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    comfyui_inventory: dict[str, Any]
    try:
        comfyui_models = ComfyUIClient(
            settings.comfyui_url,
            args.timeout or min(settings.request_timeout_seconds, 10),
        ).model_inventory()
        comfyui_inventory = {
            "schema_version": "1.0",
            "updated_at": utc_now(),
            "url": settings.comfyui_url,
            "categories": comfyui_models,
            "category_count": len(comfyui_models),
            "model_count": sum(len(models) for models in comfyui_models.values()),
        }
        atomic_write_json(state / "visual" / "comfyui-models.json", comfyui_inventory)
        comfyui_status = "available"
        comfyui_error = None
    except (ComfyUIError, ValueError) as exc:
        comfyui_inventory = {}
        comfyui_status = "unreachable"
        comfyui_error = str(exc)
    _json({
        "status": "refreshed",
        "model_count": len(profiles),
        "changes": changes,
        "evaluated_result_count": evaluation.get("current_result_count", 0) if evaluation else 0,
        "evaluation_artifact": evaluation.get("artifact_path") if evaluation else None,
        "skipped_strengths": skipped_strengths,
        "pending_evaluations": [
            {"model": model, "categories": sorted(categories)}
            for model, categories in sorted(pending_assignments.items())
        ] if args.no_evaluate else [],
        "kitchen": kitchen,
        "comfyui": {
            "status": comfyui_status,
            "model_count": comfyui_inventory.get("model_count", 0),
            "category_count": comfyui_inventory.get("category_count", 0),
            "inventory_path": str(state / "visual" / "comfyui-models.json")
            if comfyui_status == "available" else None,
            "error": comfyui_error,
        },
    })
    return 0


def _evaluated_strengths(path: Path) -> set[tuple[str, str, str]]:
    if not path.exists():
        return set()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    results = value.get("results", []) if isinstance(value, dict) else []
    return {
        (str(item["model"]), str(item["model_digest"]), str(item["category"]))
        for item in results
        if isinstance(item, dict)
        and item.get("model")
        and item.get("model_digest")
        and item.get("category")
    }


def cmd_checkpoint(args: argparse.Namespace) -> int:
    settings, root = load_settings()
    state = ensure_state(root, settings.state_dir)
    jobs = sorted((state / "jobs").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    runs = sorted((state / "runs").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    checkpoint = {
        "schema_version": "2.0",
        "created_at": utc_now(),
        "latest_job": str(jobs[0]) if jobs else None,
        "latest_run": str(runs[0]) if runs else None,
        "next_action": args.next_action or "Review latest run or create next bounded job.",
    }
    output = state / "checkpoint.json"
    atomic_write_json(output, checkpoint)
    _json({"checkpoint_path": str(output), "checkpoint": checkpoint})
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    settings, root = load_settings()
    path = root / settings.state_dir / "checkpoint.json"
    if not path.exists():
        print("No checkpoint exists.", file=sys.stderr)
        return 2
    _json({"checkpoint_path": str(path), "checkpoint": json.loads(path.read_text(encoding="utf-8"))})
    return 0


def cmd_kitchen(args: argparse.Namespace) -> int:
    settings, root = load_settings()
    try:
        profiles = _profiles(_client(settings), settings)
        overrides = load_overrides(_evidence_path(root, settings, "model-overrides.json"))
        profiles = [apply_overrides(profile, overrides.get(profile.name, {})) for profile in profiles]
    except (OllamaError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    result = build_kitchen(
        profiles,
        benchmark_path=_evidence_path(root, settings, "benchmarks/latest.json"),
        outcome_path=_evidence_path(root, settings, "outcomes.jsonl"),
    )
    _json(result)
    return 0


def cmd_orchestrate(args: argparse.Namespace) -> int:
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
        return 1

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
    _json({
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
    })
    return 0


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


def cmd_new_cooks(args: argparse.Namespace) -> int:
    refresh_args = argparse.Namespace(
        project=args.project,
        no_evaluate=args.no_evaluate,
        image=args.image,
        timeout=args.timeout,
        model=args.model,
    )
    refresh_code, refresh_payload = _captured_command(cmd_refresh, refresh_args)
    if refresh_code != 0:
        _json({"status": "refresh_failed", "refresh": refresh_payload})
        return refresh_code
    orchestrate_args = argparse.Namespace(
        project=args.project,
        sprint_file=args.sprint_file,
        no_local_analysis=args.no_local_analysis,
        timeout=args.timeout,
        model=[],
        apply_roster=False,
    )
    orchestrate_code, orchestrate_payload = _captured_command(cmd_orchestrate, orchestrate_args)
    _json({
        "status": "completed" if orchestrate_code == 0 else "reassignment_failed",
        "refresh": refresh_payload,
        "reassignment": orchestrate_payload,
    })
    return orchestrate_code


def _visual_parameters(values: list[str]) -> dict[str, Any]:
    parameters: dict[str, Any] = {}
    for item in values:
        if "=" not in item:
            raise ValueError(f"Visual parameter must be NAME=VALUE: {item}")
        name, raw = item.split("=", 1)
        if not name or name in parameters:
            raise ValueError(f"Duplicate or empty visual parameter: {name}")
        try:
            parameters[name] = json.loads(raw)
        except json.JSONDecodeError:
            parameters[name] = raw
    return parameters


def cmd_visual_templates(args: argparse.Namespace) -> int:
    manifest = json.loads((workflow_root() / "manifest.json").read_text(encoding="utf-8"))
    _json({"status": "available", "templates": manifest.get("templates", {})})
    return 0


def cmd_comfyui_doctor(args: argparse.Namespace) -> int:
    settings, _ = load_settings(Path(args.project).resolve())
    try:
        client = ComfyUIClient(settings.comfyui_url, args.timeout or settings.request_timeout_seconds)
        stats = client.system_stats()
        models = client.model_inventory()
    except (ComfyUIError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _json({
        "status": "ready",
        "url": settings.comfyui_url,
        "free_vram_mb": free_vram_mb(stats),
        "model_count": sum(len(items) for items in models.values()),
        "model_categories": {name: len(items) for name, items in models.items()},
        "usable_model_count": sum(1 for item in model_usability(models) if item["usable"]),
        "templates": sorted(
            json.loads((workflow_root() / "manifest.json").read_text(encoding="utf-8"))
            .get("templates", {})
        ),
    })
    return 0


def cmd_comfyui_models(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    settings, root = load_settings(project)
    state = ensure_state(root, settings.state_dir)
    try:
        models = ComfyUIClient(
            settings.comfyui_url,
            args.timeout or settings.request_timeout_seconds,
        ).model_inventory()
    except (ComfyUIError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    inventory = {
        "schema_version": "1.0",
        "updated_at": utc_now(),
        "url": settings.comfyui_url,
        "categories": models,
        "category_count": len(models),
        "model_count": sum(len(items) for items in models.values()),
        "usability": model_usability(models),
    }
    path = state / "visual" / "comfyui-models.json"
    atomic_write_json(path, inventory)
    _json({"status": "available", "inventory_path": str(path), **inventory})
    return 0


def cmd_comfyui_start(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    settings, _ = load_settings(project)
    state = ensure_state(project, settings.state_dir)
    root_value = args.portable_root or settings.comfyui_portable_root
    if not root_value:
        print("Provide --portable-root or configure comfyui_portable_root.", file=sys.stderr)
        return 2
    runtime_path = state / "visual" / "comfyui-runtime.json"
    log_path = state / "visual" / "comfyui.log"
    try:
        runtime = start_portable_comfyui(Path(root_value), runtime_path, log_path)
        client = ComfyUIClient(settings.comfyui_url, 2)
        deadline = time.monotonic() + args.wait
        last_error = ""
        while time.monotonic() < deadline:
            try:
                client.system_stats()
                _json({"status": "ready", "runtime": runtime})
                return 0
            except ComfyUIError as exc:
                last_error = str(exc)
                time.sleep(1)
    except (ComfyUIError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _json({"status": "starting", "runtime": runtime, "last_error": last_error})
    return 1


def cmd_comfyui_stop(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    settings, _ = load_settings(project)
    state = ensure_state(project, settings.state_dir)
    try:
        runtime = stop_portable_comfyui(state / "visual" / "comfyui-runtime.json")
    except (ComfyUIError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _json({"status": "stopped", "runtime": runtime})
    return 0


def cmd_visual_job(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    settings, _ = load_settings(project)
    state = ensure_state(project, settings.state_dir)
    try:
        parameters = _visual_parameters(args.param)
        inventory = ComfyUIClient(
            settings.comfyui_url,
            min(settings.request_timeout_seconds, 10),
        ).model_inventory()
        validate_model_parameters(args.template, parameters, inventory)
        job = create_visual_job(
            project,
            args.template,
            parameters,
            list(args.acceptance),
            timeout_seconds=args.timeout,
            max_attempts=args.max_attempts,
            release_ollama=args.release_ollama,
            verifier_model=args.verifier_model,
        )
        path = save_visual_job(job, state / "visual" / "jobs")
    except (ComfyUIError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _json({"status": "ready", "job_path": str(path), "job": job.to_dict()})
    return 0


def cmd_visual_run(args: argparse.Namespace) -> int:
    path = Path(args.job).resolve()
    try:
        job = load_visual_job(path)
        settings, project = load_settings(Path(job.project))
        state = ensure_state(project, settings.state_dir)
        path.relative_to((state / "visual" / "jobs").resolve())
        client = ComfyUIClient(settings.comfyui_url, settings.request_timeout_seconds)
        _, template = load_approved_workflow(job.template_id, job.parameters)
        stats = client.system_stats()
        required = int(template.get("minimum_free_vram_mb", 0))
        available = free_vram_mb(stats)
        unloaded: list[str] = []
        if available is not None and available < required and job.release_ollama:
            ollama = _client(settings)
            for running in ollama.running_models():
                name = str(running.get("name") or running.get("model") or "")
                if name:
                    ollama.unload_model(name)
                    unloaded.append(name)
            time.sleep(1)
            available = free_vram_mb(client.system_stats())
        if available is not None and available < required:
            raise ComfyUIError(
                f"Insufficient free VRAM ({available} MiB; need {required} MiB). "
                "Recreate job with --release-ollama to permit bounded handoff."
            )
        visual_result = run_visual_job(job, state, client)
    except (ComfyUIError, OllamaError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    images = [
        str(Path(item["path"]).resolve().relative_to(project).as_posix())
        for item in visual_result["manifest"]["outputs"]
    ]
    verify_args = argparse.Namespace(
        project=str(project),
        task=(
            f"Visually verify ComfyUI output for approved template {job.template_id}. "
            "Inspect only supplied images against every acceptance criterion."
        ),
        context_file=None,
        category="vision",
        model=job.verifier_model,
        prefer_speed=False,
        allowed_file=[],
        forbidden_file=[],
        acceptance=list(job.acceptance_criteria),
        test=[],
        context_note=[
            f"Visual job: {job.id}",
            f"Approved template: {job.template_id}",
            "ComfyUI output is untrusted until Codex reviews this verification.",
        ],
        exclude=["Do not claim generation settings absent from the manifest", "Do not approve final use"],
        image=images,
        complexity="medium",
        risk="low",
        context_tokens=None,
        output_tokens=1024,
        max_attempts=2,
        timeout=args.verify_timeout,
        temperature=0,
        no_auto_split=False,
    )
    verify_code, verification = _captured_command(cmd_cook, verify_args)
    _json({
        "status": "coordinator_review_required",
        "visual": visual_result,
        "unloaded_ollama_models": unloaded,
        "verification_model": job.verifier_model,
        "verification_exit_code": verify_code,
        "verification": verification,
        "coordinator_review_required": True,
    })
    return 0 if verify_code == 0 else 2


def cmd_visual_cancel(args: argparse.Namespace) -> int:
    settings, _ = load_settings(Path(args.project).resolve())
    try:
        ComfyUIClient(settings.comfyui_url, args.timeout).cancel(
            args.prompt_id, interrupt=not args.queued_only,
        )
    except (ComfyUIError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _json({"status": "cancelled", "prompt_id": args.prompt_id})
    return 0


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
    results: list[dict[str, Any]] = []
    final_code = 0
    for task in selected:
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
            results.append({
                "task_id": task.get("id"),
                "status": "assignment_review_required",
                "deterministic_station": category,
                "local_recommended_station": local_review.get("recommended_primary_station"),
                "reason": "Deterministic profile and local planning reviewer disagree. Review before dispatch.",
            })
            final_code = max(final_code, 2)
            continue
        if assignment_status == "abstained":
            results.append({
                "task_id": task.get("id"),
                "status": "coordinator_required",
                "reason": assignment.get("reason"),
            })
            final_code = max(final_code, 2)
            continue
        if assignment_status == "conditional" and not args.image:
            results.append({
                "task_id": task.get("id"),
                "status": "input_required",
                "required_inputs": assignment.get("required_inputs", []),
                "reason": "Supply --image for the assigned vision station.",
            })
            final_code = max(final_code, 2)
            continue
        if category not in STRENGTH_CASES or not isinstance(model, str) or not model:
            results.append({
                "task_id": task.get("id"),
                "status": "no_eligible_model",
                "reason": assignment.get("reason"),
            })
            final_code = max(final_code, 2)
            continue
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
        code, payload = _captured_command(cmd_cook, cook_args)
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
        if code == 0 and payload and reviewer:
            review_code, review_payload = _local_support_review(
                project, settings, state, task, payload, str(reviewer["model"]), args,
            )
            item_result["support_review"] = review_payload
            item_result["support_review_exit_code"] = review_code
            final_code = max(final_code, review_code)
        run = _primary_run_from_payload(payload)
        result_summary = None
        if isinstance(run, dict) and isinstance(run.get("response"), dict):
            raw_summary = run["response"].get("result")
            if isinstance(raw_summary, str):
                result_summary = raw_summary[:4000]
        successful = code == 0 and item_result.get("support_review_exit_code", 0) == 0
        needs_review = bool(assignment.get("review_required", True))
        ledger["tasks"][str(task.get("id"))] = {
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
            "exit_code": code,
            "support_review_exit_code": item_result.get("support_review_exit_code"),
        }
        results.append(item_result)
        final_code = max(final_code, code)
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


def _captured_command(function, args: argparse.Namespace) -> tuple[int, dict[str, Any] | None]:
    output = io.StringIO()
    with redirect_stdout(output):
        code = int(function(args))
    text = output.getvalue().strip()
    return code, json.loads(text) if text else None


def cmd_cook(args: argparse.Namespace) -> int:
    job_code, job_result = _captured_command(cmd_job, args)
    if job_code != 0 or not job_result:
        if job_result:
            _json({"stage": "job", **job_result})
        return job_code
    child_paths = job_result.get("child_job_paths", [])
    if child_paths:
        if not args.no_auto_split:
            return _cook_split_jobs(args, job_result, child_paths)
        _json({
            "status": "split",
            "stage": "job",
            "job": job_result["job"],
            "job_path": job_result["path"],
            "child_job_paths": child_paths,
            "next_action": "Dispatch independent child jobs, then synthesis job after dependencies complete.",
        })
        return 3
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
        dispatch_code, dispatch_result = _captured_command(cmd_dispatch, dispatch_args)
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
            recovery_job_code, recovery_job = _captured_command(cmd_job, recovery_args)
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
        _json({
            "status": "completed" if dispatch_code == 0 else "verification_failed",
            "stage": "dispatch",
            "job": job_result["job"],
            "recovery": recovery,
            **dispatch_result,
        })
    elif recovery:
        _json({
            "status": "failed",
            "stage": "dispatch",
            "job": job_result["job"],
            "recovery": recovery,
        })
    return dispatch_code


def _dispatch_saved_job(path: str, args: argparse.Namespace) -> tuple[int, dict[str, Any] | None, list[dict[str, Any]]]:
    result: dict[str, Any] | None = None
    code = 1
    attempts: list[dict[str, Any]] = []
    current_path = Path(path)
    current_job = load_job_card(current_path)
    fallback_models = list(current_job.fallback_models)
    dispatch_args = argparse.Namespace(job=str(current_path), timeout=args.timeout, temperature=args.temperature)
    for number in range(1, max(1, args.max_attempts) + 1):
        code, result = _captured_command(cmd_dispatch, dispatch_args)
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


def _cook_split_jobs(args: argparse.Namespace, parent: dict[str, Any], child_paths: list[str]) -> int:
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
            _json({
                "status": "child_failed",
                "stage": "split_dispatch",
                "job": parent["job"],
                "child_runs": child_runs,
                "next_action": "Inspect immutable child attempts; rerun cook after correcting local runtime/model issue.",
            })
            return code or 1
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
        _json({
            "status": "synthesis_oversized",
            "stage": "split_synthesis",
            "job": parent["job"],
            "child_runs": child_runs,
            "budget": synthesis_budget.to_dict(),
            "next_action": "Reduce child scope or raise task context within selected model metadata.",
        })
        return 3
    placeholder.id = f"{placeholder.id}-ready-{secrets.token_hex(4)}"
    placeholder.context_text = synthesis_context
    placeholder.context_notes.append("Synthesize only the supplied immutable child run evidence.")
    synthesis_path = save_job_card(placeholder, state / "jobs")
    code, result, attempts = _dispatch_saved_job(str(synthesis_path), args)
    _json({
        "status": "completed" if code == 0 else "synthesis_failed",
        "stage": "split_synthesis",
        "job": parent["job"],
        "child_runs": child_runs,
        "synthesis_job_path": str(synthesis_path),
        "synthesis_attempts": attempts,
        "synthesis": result,
    })
    return code


def cmd_benchmark(args: argparse.Namespace) -> int:
    settings, root = load_settings()
    state = ensure_state(root, settings.state_dir)
    try:
        client = _client(settings)
        profiles = _profiles(client, settings)
    except OllamaError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    profiles = [profile for profile in profiles if not profile.is_cloud]
    requested = set(args.model or [])
    categories = set(args.station or STRENGTH_CASES)
    if requested:
        selected = [profile for profile in profiles if profile.name in requested]
        assignments = None
    else:
        kitchen = build_kitchen(
            profiles,
            benchmark_path=state / "benchmarks" / "latest.json",
            outcome_path=state / "outcomes.jsonl",
        )
        assigned_names = {
            station["model"]
            for name, station in kitchen["stations"].items()
            if name in categories and station["model"]
        }
        selected = [profile for profile in profiles if profile.name in assigned_names]
        assignments: dict[str, set[str]] = {}
        for category, station in kitchen["stations"].items():
            if category in categories and station["model"]:
                assignments.setdefault(station["model"], set()).add(category)
    missing = requested - {profile.name for profile in selected}
    if missing:
        print(f"Requested local models not installed: {', '.join(sorted(missing))}", file=sys.stderr)
        return 2
    if not selected:
        print("No local station models available to benchmark.", file=sys.stderr)
        return 2

    vision_image = Path(args.image).resolve() if args.image else None
    if "vision" in categories and vision_image is None:
        print("Vision benchmark requires --image; other requested stations will still run.", file=sys.stderr)
    output = state / "benchmarks" / "latest.json"
    result = run_benchmarks(
        client,
        selected,
        output,
        args.timeout or settings.request_timeout_seconds,
        categories=categories,
        vision_image=vision_image,
        assignments=assignments,
    )
    _json({"path": str(output), **result})
    return 0


def _add_job_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--context-file", help="Explicit text file to store in job and send to local worker.")
    parser.add_argument("--category", choices=["analysis", "coding", "planning", "vision", "writing", "retrieval", "embedding"])
    parser.add_argument("--model")
    parser.add_argument("--prefer-speed", action="store_true")
    parser.add_argument("--allowed-file", action="append", default=[])
    parser.add_argument("--forbidden-file", action="append", default=[])
    parser.add_argument("--acceptance", action="append", default=[])
    parser.add_argument("--test", action="append", default=[])
    parser.add_argument("--context-note", action="append", default=[])
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--image", action="append", default=[])
    parser.add_argument("--complexity", choices=["low", "medium", "high"], default="medium")
    parser.add_argument("--risk", choices=["low", "medium", "high"], default="low")
    parser.add_argument("--context-tokens", type=int, help="Per-task context limit; cannot exceed model metadata.")
    parser.add_argument("--output-tokens", type=int, help="Per-task maximum generated tokens.")
    parser.add_argument("--max-attempts", type=int, default=2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="head-chef",
        description="Transparent local-model routing and bounded job dispatch for Codex-led projects.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Create project-local Head Chef state files.")
    init.add_argument("path", nargs="?", default=".")
    init.set_defaults(func=cmd_init)

    doctor = sub.add_parser("doctor", help="Check Ollama and Head Chef configuration.")
    doctor.set_defaults(func=cmd_doctor)

    models = sub.add_parser("models", help="List installed Ollama models.")
    models.add_argument("--json", action="store_true")
    models.set_defaults(func=cmd_models)

    refresh = sub.add_parser("refresh", help="Reconcile installed Ollama models and rebuild kitchen.")
    refresh.add_argument("--project", help="Project whose settings and evidence should be refreshed.")
    refresh.add_argument("--model", action="append", default=[], help="Evaluate only these installed models.")
    refresh.add_argument("--no-evaluate", action="store_true", help="Inventory only; do not strength-test new digests.")
    refresh.add_argument("--image", help="Local fixture used when evaluating a new vision model.")
    refresh.add_argument("--timeout", type=int)
    refresh.set_defaults(func=cmd_refresh)

    route_cmd = sub.add_parser("route", aliases=["plan"], help="Recommend an installed local model.")
    route_cmd.add_argument("--task", required=True)
    route_cmd.add_argument("--context-file")
    route_cmd.add_argument("--category", choices=["analysis", "coding", "planning", "vision", "writing", "retrieval", "embedding"])
    route_cmd.add_argument("--model", help="Manual installed-model override.")
    route_cmd.add_argument("--prefer-speed", action="store_true")
    route_cmd.set_defaults(func=cmd_route)

    budget = sub.add_parser("budget", help="Estimate context pressure.")
    budget.add_argument("--task", required=True)
    budget.add_argument("--context-file")
    budget.add_argument("--context-tokens", type=int)
    budget.set_defaults(func=cmd_budget)

    job = sub.add_parser("job", aliases=["delegate"], help="Create a bounded local-worker job card.")
    _add_job_arguments(job)
    job.set_defaults(func=cmd_job)

    cook = sub.add_parser("cook", help="Route, package, dispatch, validate, and record one local task.")
    _add_job_arguments(cook)
    cook.add_argument("--timeout", type=int)
    cook.add_argument("--temperature", type=float, default=0.1)
    cook.add_argument("--no-auto-split", action="store_true", help="Create child jobs without dispatching them.")
    cook.set_defaults(func=cmd_cook)

    dispatch = sub.add_parser("dispatch", help="Send one saved job card to its selected Ollama model.")
    dispatch.add_argument("--job", required=True)
    dispatch.add_argument("--timeout", type=int)
    dispatch.add_argument("--temperature", type=float, default=0.1)
    dispatch.set_defaults(func=cmd_dispatch)

    benchmark = sub.add_parser("benchmark", help="Run strength-specific evals on assigned local stations.")
    benchmark.add_argument("--model", action="append", default=[])
    benchmark.add_argument("--station", action="append", choices=sorted(STRENGTH_CASES), default=[])
    benchmark.add_argument("--image", help="Project-local fixture for vision station.")
    benchmark.add_argument("--timeout", type=int)
    benchmark.set_defaults(func=cmd_benchmark)

    review = sub.add_parser("review", help="Append a coordinator verdict to an immutable run.")
    review.add_argument("--run", required=True)
    review.add_argument("--status", required=True)
    review.add_argument("--note")
    review.set_defaults(func=cmd_review)

    checkpoint = sub.add_parser("checkpoint", help="Save compact resumable state.")
    checkpoint.add_argument("--next-action")
    checkpoint.set_defaults(func=cmd_checkpoint)

    resume = sub.add_parser("resume", help="Read compact resumable state.")
    resume.set_defaults(func=cmd_resume)

    kitchen = sub.add_parser("kitchen", help="Show local specialist stations Codex can route through.")
    kitchen.set_defaults(func=cmd_kitchen)

    orchestrate = sub.add_parser(
        "orchestrate",
        aliases=["sprint-plan", "reassign"],
        help="Discover sprint contracts, understand tasks, and pre-assign local stations.",
    )
    orchestrate.add_argument("--project", default=".")
    orchestrate.add_argument("--sprint-file", help="Project-relative sprint manifest override.")
    orchestrate.add_argument(
        "--model",
        action="append",
        default=[],
        help="Limit assignment candidates to this installed model; repeat for a custom roster.",
    )
    orchestrate.add_argument(
        "--apply-roster",
        action="store_true",
        help="Replace active sprint plan with the custom --model roster; default is comparison-only.",
    )
    orchestrate.add_argument("--no-local-analysis", action="store_true")
    orchestrate.add_argument("--timeout", type=int)
    orchestrate.set_defaults(func=cmd_orchestrate)

    new_cooks = sub.add_parser(
        "new-cooks",
        aliases=["onboard"],
        help="Detect/evaluate new local models, rebuild kitchen, and reassign project sprints.",
    )
    new_cooks.add_argument("--project", default=".")
    new_cooks.add_argument("--sprint-file", help="Project-relative sprint manifest override.")
    new_cooks.add_argument(
        "--model", action="append", default=[],
        help="Evaluate only these new/changed cooks; sprint seating still considers the full kitchen.",
    )
    new_cooks.add_argument("--image", help="Safe local fixture for evaluating new vision models.")
    new_cooks.add_argument("--no-evaluate", action="store_true")
    new_cooks.add_argument("--no-local-analysis", action="store_true")
    new_cooks.add_argument("--timeout", type=int)
    new_cooks.set_defaults(func=cmd_new_cooks)

    visual_templates = sub.add_parser(
        "visual-templates",
        help="List hash-approved fixed ComfyUI API workflow templates.",
    )
    visual_templates.set_defaults(func=cmd_visual_templates)

    comfyui_doctor = sub.add_parser(
        "comfyui-doctor",
        help="Check loopback ComfyUI and report available VRAM/templates.",
    )
    comfyui_doctor.add_argument("--project", default=".")
    comfyui_doctor.add_argument("--timeout", type=int, default=10)
    comfyui_doctor.set_defaults(func=cmd_comfyui_doctor)

    comfyui_models = sub.add_parser(
        "comfyui-models",
        help="List every model exposed by all ComfyUI model categories.",
    )
    comfyui_models.add_argument("--project", default=".")
    comfyui_models.add_argument("--timeout", type=int, default=10)
    comfyui_models.set_defaults(func=cmd_comfyui_models)

    comfyui_start = sub.add_parser(
        "comfyui-start",
        help="Start a verified portable ComfyUI on loopback and record its PID/log.",
    )
    comfyui_start.add_argument("--project", default=".")
    comfyui_start.add_argument("--portable-root")
    comfyui_start.add_argument("--wait", type=int, default=60)
    comfyui_start.set_defaults(func=cmd_comfyui_start)

    comfyui_stop = sub.add_parser(
        "comfyui-stop",
        help="Stop only the portable ComfyUI process previously started by Head Chef.",
    )
    comfyui_stop.add_argument("--project", default=".")
    comfyui_stop.set_defaults(func=cmd_comfyui_stop)

    visual_job = sub.add_parser(
        "visual-job",
        help="Create an immutable bounded job from an approved ComfyUI template.",
    )
    visual_job.add_argument("--project", required=True)
    visual_job.add_argument("--template", required=True)
    visual_job.add_argument("--param", action="append", default=[], help="Approved NAME=VALUE parameter.")
    visual_job.add_argument("--acceptance", action="append", default=[], required=True)
    visual_job.add_argument("--timeout", type=int, default=600)
    visual_job.add_argument("--max-attempts", type=int, default=2)
    visual_job.add_argument("--release-ollama", action="store_true")
    visual_job.add_argument("--verifier-model", default="qwen3-vl:8b-instruct")
    visual_job.set_defaults(func=cmd_visual_job)

    visual_run = sub.add_parser(
        "visual-run",
        help="Run a saved visual job through ComfyUI, then Qwen3-VL verification.",
    )
    visual_run.add_argument("--job", required=True)
    visual_run.add_argument("--verify-timeout", type=int, default=180)
    visual_run.set_defaults(func=cmd_visual_run)

    visual_cancel = sub.add_parser(
        "visual-cancel",
        help="Cancel a queued/running ComfyUI prompt.",
    )
    visual_cancel.add_argument("--project", default=".")
    visual_cancel.add_argument("--prompt-id", required=True)
    visual_cancel.add_argument("--queued-only", action="store_true")
    visual_cancel.add_argument("--timeout", type=int, default=10)
    visual_cancel.set_defaults(func=cmd_visual_cancel)

    work = sub.add_parser(
        "work",
        aliases=["run-plan"],
        help="Dispatch dependency-ready sprint tasks to their assigned local stations.",
    )
    work.add_argument("--project", default=".")
    work.add_argument("--task-id", action="append", default=[])
    work.add_argument(
        "--accept-task",
        action="append",
        default=[],
        help="Accept a coordinator-pending successful task result and unlock dependents.",
    )
    work.add_argument("--all-ready", action="store_true")
    work.add_argument("--image", action="append", default=[], help="Project-local input for a ready vision task.")
    work.add_argument(
        "--accept-assignment-review",
        action="store_true",
        help="Dispatch deterministic station after reviewing a local station disagreement.",
    )
    work.add_argument("--context-tokens", type=int)
    work.add_argument("--output-tokens", type=int)
    work.add_argument("--max-attempts", type=int, default=3)
    work.add_argument("--timeout", type=int)
    work.add_argument("--temperature", type=float, default=0.1)
    work.add_argument("--complexity", choices=["low", "medium", "high"], default="high")
    work.add_argument("--risk", choices=["low", "medium", "high"], default="medium")
    work.set_defaults(func=cmd_work)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

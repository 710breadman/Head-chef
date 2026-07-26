from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from dataclasses import asdict
import io
import json
from pathlib import Path
import platform
import sys
import time
from typing import Any

from .benchmark import STRENGTH_CASES, run_benchmarks
from .config import Settings, load_settings, write_default_config
from .jobs import create_job_card, load_job_card, render_worker_prompt, save_job_card
from .models import ModelProfile, profile_from_ollama
from .ollama import OllamaClient, OllamaError
from .router import RouteRequest, route
from .storage import append_jsonl, atomic_write_json, ensure_state, utc_now
from .contracts import CONTRACT_VERSION
from .executors import execute_job
from .verification import parse_worker_output, verify_output, validate_review_status
from .runs import RunRecord, next_attempt, save_run
from .registry import apply_overrides, load_overrides, save_registry
from .context_packager import package_context, render_package, split_context
from .kitchen import build_kitchen
from .token_governor import evaluate_budget


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


def _client(settings: Settings) -> OllamaClient:
    return OllamaClient(settings.ollama_url, settings.request_timeout_seconds)


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


def _context_text(args: argparse.Namespace) -> str:
    pieces = [getattr(args, "task", "")]
    path_value = getattr(args, "context_file", None)
    if path_value:
        path = Path(path_value)
        try:
            pieces.append(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ValueError(f"Cannot read context file {path}: {exc}") from exc
    return "\n\n".join(piece for piece in pieces if piece)


def _decision(args: argparse.Namespace, profiles: list[ModelProfile], settings: Settings, root: Path):
    context = _context_text(args)
    benchmark_path = root / settings.state_dir / "benchmarks" / "latest.json"
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
        outcome_path=root / settings.state_dir / "outcomes.jsonl",
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
    settings, root = load_settings()
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
            context_text = Path(args.context_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Cannot read context file {args.context_file}: {exc}", file=sys.stderr)
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
            benchmark_path=root / settings.state_dir / "benchmarks" / "latest.json",
            outcome_path=root / settings.state_dir / "outcomes.jsonl",
        )

    job = create_job_card(
        project=args.project,
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
                project=args.project,
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
            child_ids.append(child.id)
            child_paths.append(str(save_job_card(child, state / "jobs")))
        synthesis = create_job_card(
            project=args.project,
            task=f"Synthesize child results for: {args.task}",
            decision=decision,
            acceptance_criteria=args.acceptance,
            exclusions=["Do not add claims absent from child results"],
        )
        synthesis.parent_job_id = job.id
        synthesis.model_digest = job.model_digest
        synthesis.dependencies = child_ids
        synthesis.requires_split = False
        child_paths.append(str(save_job_card(synthesis, state / "jobs")))
    _json({"job": job.to_dict(), "path": str(path), "child_job_paths": child_paths})
    return 0 if job.selected_model else 2


def cmd_dispatch(args: argparse.Namespace) -> int:
    settings, root = load_settings()
    state = ensure_state(root, settings.state_dir)
    path = Path(args.job).resolve()
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
    run = RunRecord(
        job.id, job.selected_model, execution.executor, attempt, not errors,
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
    append_jsonl(
        state / "outcomes.jsonl",
        {
            "created_at": utc_now(), "job_id": job.id, "run_id": run.run_id,
            "model": job.selected_model, "category": job.category, "ok": not errors,
            "model_digest": job.model_digest,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "review_status": verification.status,
        },
    )
    _json({"run_path": str(run_path), "run": _public_run(run), "verification": verification.to_dict()})
    return 0 if not errors else 4


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
        overrides = load_overrides(root / settings.state_dir / "model-overrides.json")
        profiles = [apply_overrides(profile, overrides.get(profile.name, {})) for profile in profiles]
    except (OllamaError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    result = build_kitchen(
        profiles,
        benchmark_path=root / settings.state_dir / "benchmarks" / "latest.json",
        outcome_path=root / settings.state_dir / "outcomes.jsonl",
    )
    _json(result)
    return 0


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
    dispatch_code, dispatch_result = _captured_command(cmd_dispatch, dispatch_args)
    if dispatch_result:
        _json({
            "status": "completed" if dispatch_code == 0 else "verification_failed",
            "stage": "dispatch",
            "job": job_result["job"],
            **dispatch_result,
        })
    return dispatch_code


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

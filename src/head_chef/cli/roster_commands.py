"""Project init, environment doctor, model roster, ad-hoc routing, and refresh/onboarding."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
from typing import Any

from ..benchmark import STRENGTH_CASES, run_benchmarks
from ..comfyui import ComfyUIClient, ComfyUIError
from ..config import Settings, load_settings, write_default_config
from ..kitchen import build_kitchen
from ..ollama import OllamaClient, OllamaError
from ..registry import apply_overrides, load_overrides, reconcile_registry, save_registry
from ..storage import atomic_write_json, ensure_state, utc_now
from ..token_governor import evaluate_budget

from ._shared import (
    _client,
    _context_text,
    _decision,
    _evidence_path,
    _global_evidence_path,
    _json,
    _profiles,
)
from .sprint_commands import _orchestrate_core


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    root.mkdir(parents=True, exist_ok=True)
    config_path = write_default_config(root)
    state = ensure_state(root)
    print(f"Initialized Head Chef state at {state}")
    print(f"Config: {config_path}")
    return 0


def _detected_gpu_count() -> int | None:
    """Best-effort GPU count via nvidia-smi, purely to suggest a --parallel value. Returns None
    (not 0/1) when undetectable, e.g. no NVIDIA GPU, driver not installed, or nvidia-smi missing
    from PATH — Ollama itself remains the authority on what it can actually schedule."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "-L"], capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    count = sum(1 for line in result.stdout.splitlines() if line.strip().startswith("GPU "))
    return count or None


def cmd_doctor(args: argparse.Namespace) -> int:
    try:
        settings, root = load_settings()
    except ValueError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2

    gpu_count = _detected_gpu_count()
    report: dict[str, Any] = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "project_root": str(root),
        "ollama_url": settings.ollama_url,
        "ollama_reachable": False,
        "ollama_version": None,
        "installed_models": 0,
        "detected_gpu_count": gpu_count,
        "suggested_work_parallel": gpu_count if gpu_count else 1,
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
    if gpu_count and gpu_count > 1:
        report["warnings"].append(
            f"{gpu_count} GPUs detected. For concurrent local dispatch, use "
            f"'work --all-ready --parallel {gpu_count}' and configure Ollama's own scheduler "
            "(OLLAMA_NUM_PARALLEL, OLLAMA_SCHED_SPREAD) to spread models across them — "
            "Head Chef only avoids serializing its own requests, Ollama decides GPU placement."
        )
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


def _refresh_core(args: argparse.Namespace) -> tuple[int, dict[str, Any] | None]:
    project = Path(args.project).resolve() if getattr(args, "project", None) else None
    settings, root = load_settings(project)
    state = ensure_state(root, settings.state_dir)
    lock_root = _global_evidence_path("") or state
    lock_path = lock_root / "refresh.lock"
    try:
        lock_fd = _acquire_refresh_lock(lock_path)
    except FileExistsError:
        print(f"Another Head Chef refresh is running: {lock_path}", file=sys.stderr)
        return 2, None
    try:
        return _cmd_refresh_unlocked(args, settings, root, state)
    finally:
        os.close(lock_fd)
        lock_path.unlink(missing_ok=True)


def cmd_refresh(args: argparse.Namespace) -> int:
    code, payload = _refresh_core(args)
    if payload is not None:
        _json(payload)
    return code


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
) -> tuple[int, dict[str, Any] | None]:
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
        return 1, None
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
    payload = {
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
    }
    return 0, payload


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


def cmd_new_cooks(args: argparse.Namespace) -> int:
    refresh_args = argparse.Namespace(
        project=args.project,
        no_evaluate=args.no_evaluate,
        image=args.image,
        timeout=args.timeout,
        model=args.model,
    )
    refresh_code, refresh_payload = _refresh_core(refresh_args)
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
    orchestrate_code, orchestrate_payload = _orchestrate_core(orchestrate_args)
    _json({
        "status": "completed" if orchestrate_code == 0 else "reassignment_failed",
        "refresh": refresh_payload,
        "reassignment": orchestrate_payload,
    })
    return orchestrate_code

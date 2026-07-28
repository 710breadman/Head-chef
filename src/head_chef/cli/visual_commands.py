"""ComfyUI-backed visual generation: templates, portable runtime, and visual jobs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any

from ..comfyui import (
    ComfyUIClient,
    ComfyUIError,
    start_portable_comfyui,
    stop_portable_comfyui,
)
from ..config import load_settings
from ..ollama import OllamaError
from ..storage import atomic_write_json, ensure_state, utc_now
from ..visual import (
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

from ._shared import _client, _json
from .job_commands import _cook_core


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


def _visual_job_core(args: argparse.Namespace) -> tuple[int, dict[str, Any] | None]:
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
        return 2, None
    return 0, {"status": "ready", "job_path": str(path), "job": job.to_dict()}


def cmd_visual_job(args: argparse.Namespace) -> int:
    code, payload = _visual_job_core(args)
    if payload is not None:
        _json(payload)
    return code


def _visual_run_core(args: argparse.Namespace) -> tuple[int, dict[str, Any] | None]:
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
        return 1, None

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
    verify_code, verification = _cook_core(verify_args)
    payload = {
        "status": "coordinator_review_required",
        "visual": visual_result,
        "unloaded_ollama_models": unloaded,
        "verification_model": job.verifier_model,
        "verification_exit_code": verify_code,
        "verification": verification,
        "coordinator_review_required": True,
    }
    return (0 if verify_code == 0 else 2), payload


def cmd_visual_run(args: argparse.Namespace) -> int:
    code, payload = _visual_run_core(args)
    if payload is not None:
        _json(payload)
    return code


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

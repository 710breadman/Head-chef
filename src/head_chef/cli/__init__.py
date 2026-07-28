"""Head Chef CLI: argparse wiring only. Command logic lives in the sibling *_commands modules."""
from __future__ import annotations

import argparse
import sys

from ..benchmark import STRENGTH_CASES

# Re-exported for backward compatibility: this package replaces the former flat cli.py module,
# and both external callers (the `head-chef` console script) and the test suite import command
# functions and shared helpers directly from `head_chef.cli`.
from ._shared import (
    _append_outcome,
    _client,
    _context_text,
    _decision,
    _evidence_path,
    _global_evidence_path,
    _json,
    _profiles,
    _public_job,
    _public_run,
    _safe_project_text,
    _worker_run_succeeded,
)
from .job_commands import (
    _cook_core,
    _cook_split_jobs_core,
    _dispatch_core,
    _dispatch_saved_job,
    _job_core,
    cmd_cook,
    cmd_dispatch,
    cmd_job,
    cmd_review,
)
from .visual_commands import (
    _visual_job_core,
    _visual_parameters,
    _visual_run_core,
    cmd_comfyui_doctor,
    cmd_comfyui_models,
    cmd_comfyui_start,
    cmd_comfyui_stop,
    cmd_visual_cancel,
    cmd_visual_job,
    cmd_visual_run,
    cmd_visual_templates,
)
from .sprint_commands import (
    _assignment_changes,
    _assignment_identity,
    _completed_sprint_tasks,
    _create_sprint_outline,
    _dependency_handoff,
    _dispatch_one_task,
    _dispatch_visual_task,
    _existing_plan_files,
    _load_work_ledger,
    _local_support_review,
    _orchestrate_core,
    _primary_run_from_payload,
    _TaskOutcome,
    cmd_orchestrate,
    cmd_sprint_check,
    cmd_work,
)
from .roster_commands import (
    _acquire_refresh_lock,
    _cmd_refresh_unlocked,
    _evaluated_strengths,
    _refresh_core,
    _refresh_lock_is_live,
    cmd_budget,
    cmd_checkpoint,
    cmd_doctor,
    cmd_init,
    cmd_kitchen,
    cmd_models,
    cmd_new_cooks,
    cmd_refresh,
    cmd_resume,
    cmd_route,
)
from .benchmark_commands import cmd_benchmark

__all__ = ["build_parser", "main"]


def _add_job_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--context-file", help="Explicit text file to store in job and send to local worker.")
    parser.add_argument(
        "--category",
        choices=[
            "analysis", "coding", "planning", "vision", "writing", "retrieval", "embedding",
            "image_generation", "video_generation",
        ],
    )
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
    route_cmd.add_argument(
        "--category",
        choices=[
            "analysis", "coding", "planning", "vision", "writing", "retrieval", "embedding",
            "image_generation", "video_generation",
        ],
    )
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

    sprint_check = sub.add_parser(
        "sprint-check",
        aliases=["skill-check", "check-plan"],
        help="Check for a sprint outline, offer to create one, then build the sprint plan.",
    )
    sprint_check.add_argument("--project", default=".")
    sprint_check.add_argument("--sprint-file", help="Project-relative sprint manifest override.")
    sprint_check.add_argument("--yes", action="store_true", help="Create a missing outline without prompting.")
    sprint_check.add_argument("--no-local-analysis", action="store_true")
    sprint_check.add_argument("--timeout", type=int)
    sprint_check.set_defaults(func=cmd_sprint_check)

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
    work.add_argument(
        "--parallel",
        type=int,
        default=1,
        help=(
            "Dispatch up to N dependency-ready tasks concurrently (e.g. --all-ready --parallel 3 "
            "on a multi-GPU box). Head Chef only avoids serializing its own requests; Ollama's own "
            "scheduler (OLLAMA_NUM_PARALLEL, OLLAMA_SCHED_SPREAD, CUDA_VISIBLE_DEVICES) decides how "
            "concurrent requests are placed across GPUs. Default 1 (sequential)."
        ),
    )
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

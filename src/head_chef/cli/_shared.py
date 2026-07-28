"""Cross-cutting helpers shared by every cli.* command module."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

from ..config import Settings
from ..contracts import CONTRACT_VERSION
from ..models import ModelProfile, profile_from_ollama
from ..ollama import OllamaClient, OllamaError
from ..router import RouteRequest, route
from ..runs import RunRecord
from ..storage import append_jsonl


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


def _worker_run_succeeded(
    parsed: object,
    validation_errors: list[str],
    acceptance_complete: bool,
) -> bool:
    blockers = parsed.get("blockers", []) if isinstance(parsed, dict) else []
    return not validation_errors and acceptance_complete and not blockers

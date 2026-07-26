from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
import secrets
import time
from typing import Any

from .comfyui import ComfyUIClient, ComfyUIError
from .storage import atomic_create_json, atomic_write_json, compact_timestamp, utc_now


VISUAL_SCHEMA_VERSION = "1.0"


@dataclass(slots=True)
class VisualJob:
    id: str
    project: str
    template_id: str
    parameters: dict[str, Any]
    acceptance_criteria: list[str]
    created_at: str = field(default_factory=utc_now)
    status: str = "ready"
    timeout_seconds: int = 600
    max_attempts: int = 2
    release_ollama: bool = False
    verifier_model: str = "qwen3-vl:8b-instruct"
    schema_version: str = VISUAL_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def workflow_root() -> Path:
    candidates = [
        Path(__file__).resolve().parents[2] / "workflows" / "comfyui",
        Path(__file__).resolve().parents[3] / "workflows" / "comfyui",
    ]
    for candidate in candidates:
        if (candidate / "manifest.json").exists():
            return candidate
    raise ValueError("Approved ComfyUI workflow registry is missing")


def load_approved_workflow(template_id: str, parameters: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    root = workflow_root()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    templates = manifest.get("templates", {}) if isinstance(manifest, dict) else {}
    spec = templates.get(template_id) if isinstance(templates, dict) else None
    if not isinstance(spec, dict):
        raise ValueError(f"Unknown approved ComfyUI template: {template_id}")
    filename = spec.get("file")
    if not isinstance(filename, str) or Path(filename).name != filename:
        raise ValueError("Approved workflow file is invalid")
    path = root / filename
    raw = path.read_bytes()
    expected = spec.get("sha256")
    actual = hashlib.sha256(raw).hexdigest()
    if not isinstance(expected, str) or actual != expected:
        raise ValueError(f"Approved workflow hash mismatch: {template_id}")
    variable_specs = spec.get("variables", {})
    if not isinstance(variable_specs, dict):
        raise ValueError("Approved workflow variables are invalid")
    unknown = set(parameters) - set(variable_specs)
    if unknown:
        raise ValueError(f"Template parameters are not approved: {', '.join(sorted(unknown))}")
    resolved: dict[str, Any] = {}
    for name, variable in variable_specs.items():
        if not isinstance(variable, dict):
            raise ValueError(f"Invalid variable specification: {name}")
        value = parameters.get(name, variable.get("default"))
        resolved[name] = _validate_parameter(name, value, variable)
    workflow = json.loads(raw.decode("utf-8"))
    return _replace_placeholders(workflow, resolved), spec


def model_usability(inventory: dict[str, list[str]]) -> list[dict[str, Any]]:
    """Map installed ComfyUI models to approved Head Chef workflow inputs."""
    manifest = json.loads((workflow_root() / "manifest.json").read_text(encoding="utf-8"))
    templates = manifest.get("templates", {}) if isinstance(manifest, dict) else {}
    uses: dict[tuple[str, str], list[dict[str, str]]] = {}
    for template_id, template in templates.items() if isinstance(templates, dict) else []:
        variables = template.get("variables", {}) if isinstance(template, dict) else {}
        for parameter, spec in variables.items() if isinstance(variables, dict) else []:
            category = spec.get("model_category") if isinstance(spec, dict) else None
            if isinstance(category, str):
                uses.setdefault((category, parameter), []).append({
                    "template": str(template_id),
                    "parameter": str(parameter),
                })
    result: list[dict[str, Any]] = []
    for category, models in sorted(inventory.items()):
        category_uses = [
            use for (use_category, _), values in uses.items()
            if use_category == category for use in values
        ]
        for model in models:
            result.append({
                "category": category,
                "model": model,
                "usable": bool(category_uses),
                "uses": category_uses,
                "reason": (
                    "approved workflow available"
                    if category_uses else "no approved workflow template"
                ),
            })
    return result


def validate_model_parameters(
    template_id: str,
    parameters: dict[str, Any],
    inventory: dict[str, list[str]],
) -> None:
    manifest = json.loads((workflow_root() / "manifest.json").read_text(encoding="utf-8"))
    template = manifest.get("templates", {}).get(template_id, {})
    variables = template.get("variables", {}) if isinstance(template, dict) else {}
    for parameter, spec in variables.items() if isinstance(variables, dict) else []:
        category = spec.get("model_category") if isinstance(spec, dict) else None
        if not isinstance(category, str) or parameter not in parameters:
            continue
        value = parameters[parameter]
        available = inventory.get(category, [])
        if value not in available:
            raise ValueError(
                f"{parameter} is not installed in ComfyUI category {category}: {value}"
            )


def _validate_parameter(name: str, value: Any, spec: dict[str, Any]) -> Any:
    kind = spec.get("type")
    if kind == "string":
        if not isinstance(value, str) or len(value) > int(spec.get("max_length", 4000)):
            raise ValueError(f"{name} must be a bounded string")
        allowed = spec.get("enum")
        if isinstance(allowed, list) and value not in allowed:
            raise ValueError(f"{name} is not approved")
    elif kind == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{name} must be an integer")
        if value < int(spec.get("minimum", value)) or value > int(spec.get("maximum", value)):
            raise ValueError(f"{name} is outside approved range")
    elif kind == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"{name} must be numeric")
        if value < float(spec.get("minimum", value)) or value > float(spec.get("maximum", value)):
            raise ValueError(f"{name} is outside approved range")
    else:
        raise ValueError(f"Unsupported approved variable type: {kind}")
    return value


def _replace_placeholders(value: Any, parameters: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: _replace_placeholders(item, parameters) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_placeholders(item, parameters) for item in value]
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        name = value[2:-1]
        if name not in parameters:
            raise ValueError(f"Workflow contains undeclared placeholder: {name}")
        return parameters[name]
    return value


def create_visual_job(
    project: Path,
    template_id: str,
    parameters: dict[str, Any],
    acceptance_criteria: list[str],
    *,
    timeout_seconds: int = 600,
    max_attempts: int = 2,
    release_ollama: bool = False,
    verifier_model: str = "qwen3-vl:8b-instruct",
) -> VisualJob:
    if not acceptance_criteria or not all(
        isinstance(item, str) and item.strip() for item in acceptance_criteria
    ):
        raise ValueError("Visual jobs require explicit acceptance criteria")
    load_approved_workflow(template_id, parameters)
    return VisualJob(
        id=f"VJ-{compact_timestamp()}-{secrets.token_hex(4)}",
        project=str(project.resolve()),
        template_id=template_id,
        parameters=parameters,
        acceptance_criteria=acceptance_criteria,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        release_ollama=release_ollama,
        verifier_model=verifier_model,
    )


def save_visual_job(job: VisualJob, directory: Path) -> Path:
    path = directory / f"{job.id}.json"
    atomic_create_json(path, job.to_dict())
    return path


def load_visual_job(path: Path) -> VisualJob:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != VISUAL_SCHEMA_VERSION:
        raise ValueError("Unsupported visual job schema")
    return VisualJob(**value)


def free_vram_mb(stats: dict[str, Any]) -> int | None:
    values: list[int] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key.casefold() in {"vram_free", "free_vram", "vram_free_bytes"} and isinstance(item, (int, float)):
                    number = int(item)
                    values.append(number // (1024 * 1024) if number > 1_000_000 else number)
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(stats)
    return max(values) if values else None


def run_visual_job(
    job: VisualJob,
    state: Path,
    client: ComfyUIClient,
) -> dict[str, Any]:
    workflow, spec = load_approved_workflow(job.template_id, job.parameters)
    minimum_vram = int(spec.get("minimum_free_vram_mb", 0))
    available_vram = free_vram_mb(client.system_stats())
    if available_vram is not None and available_vram < minimum_vram:
        raise ComfyUIError(
            f"ComfyUI has {available_vram} MiB free VRAM; template requires {minimum_vram} MiB"
        )
    progress: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    history: dict[str, Any] | None = None
    prompt_id: str | None = None
    for attempt in range(1, job.max_attempts + 1):
        started = time.perf_counter()
        try:
            prompt_id = client.queue_prompt(workflow)
            history = client.wait(
                prompt_id,
                job.timeout_seconds,
                on_progress=lambda event: progress.append({
                    "created_at": utc_now(),
                    "type": event.get("type"),
                    "data": event.get("data"),
                }),
            )
            attempts.append({
                "attempt": attempt, "prompt_id": prompt_id, "ok": True,
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            })
            break
        except ComfyUIError as exc:
            attempts.append({
                "attempt": attempt, "prompt_id": prompt_id, "ok": False,
                "elapsed_seconds": round(time.perf_counter() - started, 3), "error": str(exc),
            })
            if prompt_id:
                try:
                    client.cancel(prompt_id)
                except ComfyUIError:
                    pass
    if history is None or prompt_id is None:
        raise ComfyUIError(f"ComfyUI job failed after {job.max_attempts} attempt(s)")

    output_dir = state / "visual" / "outputs" / job.id
    manifest_items: list[dict[str, Any]] = []
    outputs = history.get("outputs", {})
    for node_id, node_output in outputs.items() if isinstance(outputs, dict) else []:
        if not isinstance(node_output, dict):
            continue
        for image in node_output.get("images", []):
            if not isinstance(image, dict):
                continue
            filename = image.get("filename")
            if not isinstance(filename, str):
                continue
            payload = client.download_output(
                filename,
                str(image.get("subfolder") or ""),
                str(image.get("type") or "output"),
            )
            suffix = Path(filename).suffix.lower()
            if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
                raise ComfyUIError(f"Unsupported ComfyUI output type: {suffix}")
            destination = output_dir / f"{len(manifest_items) + 1:03d}{suffix}"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
            manifest_items.append({
                "node_id": str(node_id),
                "source_filename": filename,
                "path": str(destination),
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            })
    if not manifest_items:
        raise ComfyUIError("ComfyUI completed without approved image outputs")
    manifest = {
        "schema_version": VISUAL_SCHEMA_VERSION,
        "created_at": utc_now(),
        "job_id": job.id,
        "template_id": job.template_id,
        "prompt_id": prompt_id,
        "attempts": attempts,
        "progress": progress,
        "outputs": manifest_items,
        "coordinator_review_required": True,
    }
    manifest_path = state / "visual" / "manifests" / f"{job.id}.json"
    atomic_write_json(manifest_path, manifest)
    return {"manifest_path": str(manifest_path), "manifest": manifest}

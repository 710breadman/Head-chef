from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Settings:
    ollama_url: str = "http://127.0.0.1:11434"
    state_dir: str = ".head-chef"
    default_context_tokens: int = 8192
    reserved_output_tokens: int = 2048
    safety_margin_tokens: int = 1024
    request_timeout_seconds: int = 180
    max_benchmark_models: int = 8

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "Settings":
        allowed = {field for field in cls.__dataclass_fields__}
        filtered = {key: value for key, value in data.items() if key in allowed}
        return cls(**filtered)


def find_project_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists() or (candidate / "pyproject.toml").exists():
            return candidate
    return current


def resolve_state_dir(root: Path, state_dir: str) -> Path:
    root = root.resolve()
    state_path = Path(state_dir)
    if state_path.is_absolute() or ".." in state_path.parts:
        raise ValueError("state_dir must be a project-relative path without '..'")
    resolved_state = (root / state_path).resolve()
    try:
        resolved_state.relative_to(root)
    except ValueError as exc:
        raise ValueError("state_dir escapes project root") from exc
    return resolved_state


def load_settings(start: Path | None = None) -> tuple[Settings, Path]:
    root = find_project_root(start)
    settings = Settings()
    config_path = root / settings.state_dir / "config.json"

    if config_path.exists():
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            settings = Settings.from_mapping(raw)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid Head Chef config at {config_path}: {exc}") from exc

    env_url = os.getenv("HEAD_CHEF_OLLAMA_URL")
    if env_url:
        settings.ollama_url = env_url.rstrip("/")

    resolve_state_dir(root, settings.state_dir)

    return settings, root


def write_default_config(root: Path, settings: Settings | None = None) -> Path:
    settings = settings or Settings()
    state_dir = resolve_state_dir(root, settings.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "config.json"
    if not path.exists():
        path.write_text(json.dumps(asdict(settings), indent=2) + "\n", encoding="utf-8")
    return path

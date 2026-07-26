from __future__ import annotations

from dataclasses import dataclass, field, asdict
import json
from pathlib import Path
from typing import Any

from .models import ModelProfile
from .storage import atomic_write_json, utc_now


REGISTRY_SCHEMA_VERSION = "2.0"


@dataclass(slots=True)
class RegistryEntry:
    name: str
    digest: str = ""
    capabilities: list[str] = field(default_factory=list)
    context_tokens: int = 8192
    quantization: str = "unknown"
    parameter_billions: float = 0.0
    observed_at: str = field(default_factory=utc_now)
    metadata_complete: bool = False
    capability_source: str = "inferred"
    tokens_per_second: float | None = None
    cold_load_seconds: float | None = None
    memory_bytes: int | None = None
    successes: dict[str, int] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def apply_overrides(profile: ModelProfile, override: dict[str, Any]) -> ModelProfile:
    if not override:
        return profile
    allowed = {"add_capabilities", "remove_capabilities", "context_tokens", "notes"}
    unknown = set(override) - allowed
    if unknown:
        raise ValueError(f"Unknown model override fields: {', '.join(sorted(unknown))}")
    additions = override.get("add_capabilities", [])
    removals = override.get("remove_capabilities", [])
    if not isinstance(additions, list) or not all(isinstance(x, str) for x in additions):
        raise ValueError("add_capabilities must be an array of strings")
    if not isinstance(removals, list) or not all(isinstance(x, str) for x in removals):
        raise ValueError("remove_capabilities must be an array of strings")
    context = override.get("context_tokens", profile.context_tokens)
    if not isinstance(context, int) or isinstance(context, bool) or context < 512:
        raise ValueError("context_tokens must be an integer >= 512")
    notes = override.get("notes", [])
    if not isinstance(notes, list) or not all(isinstance(x, str) for x in notes):
        raise ValueError("notes must be an array of strings")
    profile.capabilities = (profile.capabilities | set(additions)) - set(removals)
    profile.context_tokens = context
    profile.notes = sorted(set(profile.notes + notes + ["owner-override"]))
    return profile


def load_overrides(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, dict) for k, v in data.items()):
        raise ValueError("model-overrides.json must map model names to objects")
    return data


def save_registry(path: Path, profiles: list[ModelProfile]) -> None:
    entries = [
        RegistryEntry(
            name=p.name,
            digest=p.digest,
            capabilities=sorted(p.capabilities),
            context_tokens=p.context_tokens,
            quantization=p.quantization,
            parameter_billions=p.parameter_billions,
            metadata_complete=p.metadata_complete,
            capability_source=p.capability_source,
        ).to_dict()
        for p in profiles
    ]
    atomic_write_json(path, {"schema_version": REGISTRY_SCHEMA_VERSION, "updated_at": utc_now(), "models": entries})


def reconcile_registry(path: Path, profiles: list[ModelProfile]) -> dict[str, Any]:
    previous: dict[str, str] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            previous = {
                str(item["name"]): str(item.get("digest") or "")
                for item in raw.get("models", [])
                if isinstance(item, dict) and item.get("name")
            }
        except (OSError, json.JSONDecodeError, TypeError):
            previous = {}
    current = {profile.name: profile.digest for profile in profiles}
    changes = {
        "new": sorted(current.keys() - previous.keys()),
        "updated": sorted(name for name in current.keys() & previous.keys() if current[name] != previous[name]),
        "removed": sorted(previous.keys() - current.keys()),
        "unchanged": sorted(name for name in current.keys() & previous.keys() if current[name] == previous[name]),
    }
    save_registry(path, profiles)
    return changes

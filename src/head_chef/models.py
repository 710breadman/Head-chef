from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any, Iterable


GENERATIVE_CAPABILITIES = {
    "analysis",
    "coding",
    "planning",
    "writing",
    "vision",
    "retrieval",
}


@dataclass(slots=True)
class ModelProfile:
    name: str
    size_bytes: int = 0
    family: str = "unknown"
    parameter_size: str = "unknown"
    parameter_billions: float = 0.0
    quantization: str = "unknown"
    context_tokens: int = 8192
    capabilities: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)
    digest: str = ""
    metadata_complete: bool = False
    capability_source: str = "inferred"
    benchmark_scores: dict[str, float] = field(default_factory=dict)
    reliability: float = 0.5

    @property
    def is_embedding_only(self) -> bool:
        return self.capabilities == {"embedding"} or "embedding-only" in self.notes

    @property
    def is_cloud(self) -> bool:
        return ":cloud" in self.name.lower() or (0 < self.size_bytes < 1_000_000)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["capabilities"] = sorted(self.capabilities)
        return data


def _parse_billions(value: str) -> float:
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*[bB]", value or "")
    return float(match.group(1)) if match else 0.0


def _context_from_show(show: dict[str, Any], fallback: int) -> int:
    model_info = show.get("model_info", {})
    if isinstance(model_info, dict):
        candidates: list[int] = []
        for key, value in model_info.items():
            if str(key).endswith(".context_length"):
                try:
                    candidates.append(int(value))
                except (TypeError, ValueError):
                    pass
        if candidates:
            return max(candidates)

    parameters = str(show.get("parameters", ""))
    match = re.search(r"num_ctx\s+([0-9]+)", parameters)
    return int(match.group(1)) if match else fallback


def infer_capabilities(name: str, family: str = "") -> tuple[set[str], list[str]]:
    text = f"{name} {family}".lower()
    capabilities: set[str] = {"analysis", "writing"}
    notes: list[str] = []

    if any(token in text for token in ("embed", "embedding", "bge", "nomic-embed")):
        return {"embedding"}, ["embedding-only"]

    if any(token in text for token in ("coder", "code", "codestral", "starcoder")):
        return {"analysis", "coding", "planning"}, ["coding-specialist"]

    if any(token in text for token in ("story", "writer", "creative")):
        return {"analysis", "writing"}, ["writing-specialist"]

    if any(token in text for token in ("vl", "vision", "llava", "pixtral")):
        capabilities = {"analysis", "vision", "retrieval"}
        notes.append("vision-capable-name")
        if "instruct" in text:
            notes.append("instruction-tuned-name")
        return capabilities, notes

    if any(token in text for token in ("gemma", "qwen", "llama", "mistral", "phi", "deepseek")):
        capabilities.update({"planning", "retrieval"})

    if "instruct" in text:
        notes.append("instruction-tuned-name")

    return capabilities, notes


def profile_from_ollama(
    raw: dict[str, Any],
    show: dict[str, Any] | None = None,
    default_context_tokens: int = 8192,
) -> ModelProfile:
    details = raw.get("details", {}) if isinstance(raw.get("details"), dict) else {}
    name = str(raw.get("name") or raw.get("model") or "unknown")
    family = str(details.get("family") or "unknown")
    parameter_size = str(details.get("parameter_size") or "unknown")
    capabilities, notes = infer_capabilities(name, family)

    show = show or {}
    show_capabilities = show.get("capabilities", [])
    if isinstance(show_capabilities, list):
        lowered = {str(value).lower() for value in show_capabilities}
        if "vision" in lowered:
            capabilities.add("vision")
        if "embedding" in lowered:
            capabilities = {"embedding"}
            notes = ["embedding-only"]
        if "tools" in lowered:
            notes.append("tool-call-capable")

    return ModelProfile(
        name=name,
        size_bytes=int(raw.get("size") or 0),
        family=family,
        parameter_size=parameter_size,
        parameter_billions=_parse_billions(parameter_size),
        quantization=str(details.get("quantization_level") or "unknown"),
        context_tokens=_context_from_show(show, default_context_tokens),
        capabilities=capabilities,
        notes=sorted(set(notes)),
        digest=str(raw.get("digest") or ""),
        metadata_complete=bool(show),
        capability_source="ollama" if show_capabilities else "inferred",
    )


def names(profiles: Iterable[ModelProfile]) -> set[str]:
    return {profile.name for profile in profiles}

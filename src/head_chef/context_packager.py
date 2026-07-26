from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
from pathlib import Path
from typing import Any


MAX_FILE_BYTES = 1_000_000
MAX_PACKAGE_BYTES = 4_000_000
SECRET_MARKERS = ("-----BEGIN PRIVATE KEY-----", "AKIA", "ghp_", "github_pat_", "sk-proj-")


@dataclass(slots=True)
class ContextItem:
    path: str
    sha256: str
    size_bytes: int
    content: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def package_context(project: Path, allowed_files: list[str], forbidden_files: list[str] | None = None) -> list[ContextItem]:
    root = project.resolve()
    forbidden = {item.casefold() for item in (forbidden_files or [])}
    items: list[ContextItem] = []
    total = 0
    seen_hashes: set[str] = set()
    for value in allowed_files:
        if value.casefold() in forbidden:
            raise ValueError(f"File is explicitly forbidden: {value}")
        raw = Path(value)
        if raw.is_absolute() or ":" in value:
            raise ValueError(f"Allowed file must be project-relative: {value}")
        path = (root / raw).resolve(strict=True)
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"File escapes project root: {value}") from exc
        if not path.is_file():
            raise ValueError(f"Not a regular file: {value}")
        payload = path.read_bytes()
        if len(payload) > MAX_FILE_BYTES:
            raise ValueError(f"File exceeds {MAX_FILE_BYTES} bytes: {value}")
        if b"\x00" in payload:
            raise ValueError(f"Binary file rejected: {value}")
        try:
            content = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"File is not UTF-8 text: {value}") from exc
        if any(marker in content for marker in SECRET_MARKERS):
            raise ValueError(f"Possible secret found; file rejected: {value}")
        digest = hashlib.sha256(payload).hexdigest()
        if digest in seen_hashes:
            continue
        seen_hashes.add(digest)
        total += len(payload)
        if total > MAX_PACKAGE_BYTES:
            raise ValueError(f"Context package exceeds {MAX_PACKAGE_BYTES} bytes")
        items.append(ContextItem(relative.as_posix(), digest, len(payload), content))
    return items


def render_package(items: list[ContextItem]) -> str:
    return "\n\n".join(
        f'<context-file path="{item.path}" sha256="{item.sha256}">\n{item.content}\n</context-file>'
        for item in items
    )


def split_context(items: list[ContextItem], max_chars: int) -> list[list[ContextItem]]:
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    chunks: list[list[ContextItem]] = []
    current: list[ContextItem] = []
    size = 0
    for item in items:
        if len(item.content) > max_chars:
            raise ValueError(f"Single file cannot fit safely: {item.path}")
        if current and size + len(item.content) > max_chars:
            chunks.append(current)
            current, size = [], 0
        current.append(item)
        size += len(item.content)
    if current:
        chunks.append(current)
    return chunks

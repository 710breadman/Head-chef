from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compact_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def ensure_state(root: Path, state_dir_name: str = ".head-chef") -> Path:
    state = root / state_dir_name
    for child in ("jobs", "runs", "benchmarks"):
        (state / child).mkdir(parents=True, exist_ok=True)
    blockers = state / "blockers.md"
    if not blockers.exists():
        blockers.write_text("# Blockers\n\n", encoding="utf-8")
    handoff = state / "handoff.md"
    if not handoff.exists():
        handoff.write_text("# Handoff\n\nNo active handoff.\n", encoding="utf-8")
    return state


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


def atomic_create_json(path: Path, data: dict[str, Any]) -> None:
    """Create immutable evidence; fail instead of replacing an existing record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(payload)
    except FileExistsError as exc:
        raise FileExistsError(f"Refusing to overwrite immutable record: {path}") from exc


def append_jsonl(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(data, ensure_ascii=False) + "\n")

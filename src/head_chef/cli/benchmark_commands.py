"""Strength-suite benchmarking of assigned local stations."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from ..benchmark import STRENGTH_CASES, run_benchmarks
from ..config import load_settings
from ..kitchen import build_kitchen
from ..ollama import OllamaError
from ..storage import ensure_state

from ._shared import _client, _json, _profiles


def cmd_benchmark(args: argparse.Namespace) -> int:
    settings, root = load_settings()
    state = ensure_state(root, settings.state_dir)
    try:
        client = _client(settings)
        profiles = _profiles(client, settings)
    except OllamaError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    profiles = [profile for profile in profiles if not profile.is_cloud]
    requested = set(args.model or [])
    categories = set(args.station or STRENGTH_CASES)
    if requested:
        selected = [profile for profile in profiles if profile.name in requested]
        assignments = None
    else:
        kitchen = build_kitchen(
            profiles,
            benchmark_path=state / "benchmarks" / "latest.json",
            outcome_path=state / "outcomes.jsonl",
        )
        assigned_names = {
            station["model"]
            for name, station in kitchen["stations"].items()
            if name in categories and station["model"]
        }
        selected = [profile for profile in profiles if profile.name in assigned_names]
        assignments: dict[str, set[str]] = {}
        for category, station in kitchen["stations"].items():
            if category in categories and station["model"]:
                assignments.setdefault(station["model"], set()).add(category)
    missing = requested - {profile.name for profile in selected}
    if missing:
        print(f"Requested local models not installed: {', '.join(sorted(missing))}", file=sys.stderr)
        return 2
    if not selected:
        print("No local station models available to benchmark.", file=sys.stderr)
        return 2

    vision_image = Path(args.image).resolve() if args.image else None
    if "vision" in categories and vision_image is None:
        print("Vision benchmark requires --image; other requested stations will still run.", file=sys.stderr)
    output = state / "benchmarks" / "latest.json"
    result = run_benchmarks(
        client,
        selected,
        output,
        args.timeout or settings.request_timeout_seconds,
        categories=categories,
        vision_image=vision_image,
        assignments=assignments,
    )
    _json({"path": str(output), **result})
    return 0

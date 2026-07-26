# Project status

## Current state

Head Chef moved from an empty repository to a greenfield v0.1 implementation and project-control package.

## Implemented

- Project charter, scope, architecture, board review, roadmap, sprint system, decisions, security plan, and test matrix.
- Standard-library Ollama client.
- Installed-model discovery and metadata inspection.
- Conservative model profiling.
- Explainable task routing with manual override and abstention.
- Token Governor with split recommendations.
- Persistent JSON job cards.
- Safe text-only dispatch and run evidence.
- Tiny opt-in structured-output benchmark.
- CLI commands: init, doctor, models, route, budget, job, dispatch, benchmark.
- Windows installation and test scripts.
- Unit tests for budgeting, core routing, job persistence, and prompt boundaries.

## Validation completed in the isolated build environment

- 10 deterministic unit tests passed.
- Python source and tests passed `compileall`.
- CLI help and the standalone budget command ran successfully.

## Not yet proven

- Clean Windows installation.
- Live connection to the owner’s Ollama instance.
- Correct metadata for every installed model.
- Real dispatch to Gemma, Qwen Coder, and Qwen VL.
- Benchmark behavior on the owner’s hardware.
- CLI usability for a nontechnical user.

## Highest-priority next action

On the owner’s Windows machine:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\Install-HeadChef.ps1
.\scripts\Test-HeadChef.ps1
.\.venv\Scripts\head-chef.cmd doctor
.\.venv\Scripts\head-chef.cmd models --json
```

Save the output before changing routing rules.

## Blockers

- Repository license requires owner selection before a public release.
- Live Ollama and clean-Windows acceptance cannot be completed in the current isolated build environment.

## Release status

**Pre-alpha / provisional.** No fabricated local-hardware validation has been claimed.

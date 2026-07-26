# Project status

## Current state

Head Chef v0.2 core is implemented on draft PR branch. Release remains provisional pending owner hardware acceptance.

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
- Separate chat, vision, and embedding executors.
- Manual override policy invariant: model choice cannot weaken coordinator review.
- Schema v2 jobs, worker results, run attempts, verification, registry, and JSON envelopes.
- Immutable success/failure attempts with unique run IDs and attempt numbers.
- Approved-file context manifests, hashes, secret/binary/traversal rejection, dedupe, child/synthesis splitting.
- Task-specific benchmark cases and Codex aliases: plan, delegate, review, checkpoint, resume.

## Validation completed in the isolated build environment

- 26 deterministic unit tests passed during v0.2 implementation.
- Installer and test script passed on current Windows host with Python 3.11; initial BOM defect in `.pth` creation was found and fixed.
- Ollama 0.32.3 doctor passed on loopback and discovered 12 installed models.
- One real Qwen embedding dispatch passed through `/api/embed`.
- One real Qwen Coder dispatch returned schema-valid JSON and remained pending because coding requires coordinator review.
- Python source and tests passed `compileall`.
- CLI help and the standalone budget command ran successfully.

## Not yet proven

- Clean Windows installation.
- Live connection to the owner’s Ollama instance.
- Correct metadata for every installed model.
- Real Gemma and Qwen VL dispatch.
- Benchmark behavior on the owner’s hardware.
- CLI usability for a nontechnical user.
- Clean Windows installation on a fresh machine. Current-host rerun is not a clean-install claim.

## Highest-priority next action

On the owner’s Windows machine:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\Install-HeadChef.ps1
.\scripts\Test-HeadChef.ps1
.\.venv\Scripts\head-chef.cmd doctor
.\.venv\Scripts\head-chef.cmd models --json
```

Save output, registry, one chat run, one vision run, and one embedding run. Do not change routing weights before evidence review.

## Blockers

- Repository license requires owner selection before a public release.
- Live Ollama and clean-Windows acceptance cannot be completed in the current isolated build environment.

## Release status

**v0.2 pre-alpha / provisional.** No live Ollama, GPU, model-quality, or clean-Windows validation claimed.

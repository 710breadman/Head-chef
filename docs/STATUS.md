# Project status

## Current state

Head Chef v0.2 kitchen and Codex skill are implemented on `main`.

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
- One-command `cook` workflow and installable `head-chef-local-router` Codex skill.
- Digest-bound strength evidence and outcomes; changed tags cannot inherit old evidence.
- Immutable, category-specific benchmark attempts and evidence-based station assignment.
- Repository-independent private runtime and stable launcher installed under Codex home.
- Machine-global digest-safe benchmark fallback; every dispatch adds metadata-only global learning evidence.
- First-use sprint orchestration: contract discovery, source hashing, task-detail merge, dependency/actionability map, primary/supporting stations, and local phase analysis.
- Live registry reconciliation (`refresh`) with new/updated/removed digest reporting and saved kitchen state.
- Adaptive per-task context/output limits passed to Ollama, plus immutable `cook` retry attempts and recovery journal.
- Machine-global model digest inventory; new/changed models automatically run advertised-strength evaluations.
- Automatic split-child dispatch and evidence-fed local synthesis, with manual opt-out.
- Dependency-ready sprint execution through `work`/`run-plan`, using safe plan-file packaging and preassigned models.
- Transient Ollama transport retry plus schema-failure specialist fallback.
- Automatic independent local analysis review of dependency-ready sprint work.
- Hard station-admission rejection for current-digest failed strength evaluations.
- Structured sprint task profiles with risk, modalities, required tools/inputs, and full/advisory/none local scope.
- Explicit assigned/conditional/unfilled/abstained station states with routing score evidence and reasons.
- Independent temperature-zero local station/scope review; disagreements are visible and station conflicts block dispatch.
- Restart-safe sprint work ledger with explicit coordinator acceptance, dynamic dependency readiness, next-ready reporting, and bounded downstream handoffs.
- One-command new-cook onboarding plus full/custom-roster sprint reassignment and timestamped assignment diffs.
- Bounded ComfyUI visual workflow: loopback client, approved workflow templates, portable runtime control, immutable visual jobs, retry/cancel, output manifests, and Qwen3-VL verification.
- Guided `skill-check` / `sprint-check` / `check-plan` entrypoint that recursively detects JSON sprint or roadmap outlines, offers to create a missing one, and invokes sprint planning.

## Validation completed in the isolated build environment

- 103 deterministic unit tests passed; one symlink escape test skipped because this Windows process lacks symlink privilege.
- Live onboarding detected Qwen 3.5 4B/9B and Qwen 3.6 27B during changing inventory; eight focused Qwen 3.5 strength cases completed with incremental checkpoints.
- Qwen 3.5 4B passed analysis (94, 35.0s); its other focused cases and all Qwen 3.5 9B cases failed contract or timed out, so they did not displace proven primary stations. Vision remains pending a fixture.
- Forced skill/runtime upgrade preserved the live benchmark file byte-for-byte (matching SHA-256); full-roster TBWL reassignment restored 45/45 assigned tasks.
- Live concurrent refresh contention exposed a shared-evidence race; refresh now uses a machine-global single-writer PID lock with stale-lock recovery.
- Live refresh on this host reconciled 12 installed entries, excluded the cloud tag, and rebuilt all seven local stations.
- Live 4K forced split dispatched two children; one schema failure rerouted from Gemma e4b to Gemma 26B; synthesis then passed.
- Live `work` on TBWL-011 used Qwen Coder, saved target-local evidence, and ran Gemma e4b as independent reviewer.
- Regenerated TBWL plan: 45 tasks, 45 assigned, 38 full-local, 7 advisory, 6 phase reviews, no review errors; local station/scope disagreements were preserved for review.
- A live Gemma skeptical review reported missing execution evidence and a true blocker; the strengthened dispatcher correctly saved immutable attempt 2 as `ok: false` and exited nonzero.
- Installer and test script passed on current Windows host with Python 3.11; initial BOM defect in `.pth` creation was found and fixed.
- Ollama 0.32.3 doctor passed on loopback and discovered 12 installed models.
- One real Qwen embedding dispatch passed through `/api/embed`.
- One real Qwen Coder dispatch returned schema-valid JSON and remained pending because coding requires coordinator review.
- All 11 installed local models passed transport for their advertised strengths. Ozan required audited 0–100 confidence normalization.
- Qwen VL vision correctly identified generated fixture; Qwen embedding returned a vector; planning, writing, retrieval, and analysis station probes passed.
- Kitchen routing currently assigns Qwen Coder (coding), Qwen VL (vision/retrieval), Qwen Embedding, Ozan Story (writing), and Gemma e4b (planning/analysis).
- `glm-5.2:cloud` is detected and excluded from local-only kitchen routing.
- Gemma 12B retrieval timed out at 180 seconds; immutable failure evidence preserved. Category outcomes now penalize failure and latency.
- Python source and tests passed `compileall`.
- CLI help and the standalone budget command ran successfully.
- Strength suite `hc-strengths-v3.2` passed all assigned stations: coding, vision, embedding, planning, writing, retrieval, and analysis.
- Planning comparison selected Gemma e4b (100, 14.4s) over Gemma 26B (94, 32.4s); Qwen Coder failed that planning contract.
- `cook` auto-routing exercised all seven categories and selected the intended station.
- Codex skill structure passed validation and a forward-use review.
- Fresh skill install, safe upgrade backup, installed doctor/kitchen/cook, portable runtime import, and global outcome learning passed on this host.
- ComfyUI focused unit suite passed: 6 tests.
- Live portable ComfyUI SDXL generation passed on this host: immutable job `VJ-20260726-144347-1da36889` produced a hash-recorded 1024x1024 PNG in 16.1 seconds.
- Qwen3-VL independently verified both visual acceptance criteria with confidence 1.0; Codex visual review agreed. A prior multi-object render was correctly rejected and preserved as evidence.

## Not yet proven

- Clean Windows installation.
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
- Clean-machine Windows acceptance and privileged reparse-path tests remain external acceptance work.
- Additional ComfyUI checkpoints and workflow templates remain hardware acceptance work.

## Release status

**v0.2 release candidate / provisional.** Live Ollama validation is claimed only for this host and the recorded strength probes. No clean-machine Windows claim is made.

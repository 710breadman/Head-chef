# Head Chef

Head Chef is a small control layer for **Codex/OpenAI-led projects that use local Ollama models as bounded workers**.

It does not replace Codex, secretly edit files, or invent a complicated autonomous-agent stack. It helps the coordinator answer five practical questions:

1. Which installed local model best fits this task?
2. Is the requested context small enough for that model?
3. How should the work be split into a safe job card?
4. What exactly was sent to the model, and what came back?
5. What should happen when a worker fails or a task is blocked?

## Current v0.2 capabilities

- Detects locally installed Ollama models and every model category exposed by ComfyUI.
- Keeps a digest-aware model registry; owner overrides are validated and provenance-marked.
- Routes through separate chat, multimodal chat, and embedding executors.
- Explains every routing score and supports a manual override.
- Estimates prompt size and recommends splitting oversized work.
- Creates schema-versioned job cards and automatic child/synthesis jobs when packaged context must split.
- Sends a job to Ollama without granting the model shell or filesystem access.
- Records every success or failure as a unique, immutable run attempt.
- Requires structured worker JSON, validates it, then gates acceptance behind coordinator review policy.
- Runs task-specific, versioned opt-in benchmarks against already-installed models.
- Discovers structured sprint contracts, merges task details, analyzes each phase locally, and pre-assigns primary/supporting stations.
- Provides a Windows-friendly doctor and installation script.

## Safety boundary

Head Chef is an **advisor and dispatcher**, not an unrestricted executor.

- Codex/OpenAI remains the planner, reviewer, and authority.
- Local-model output is untrusted until reviewed or tested.
- Head Chef does not download models automatically.
- Head Chef does not execute model-generated commands.
- Destructive actions, migrations, architecture changes, and public-interface changes require coordinator review.

## Quick start on Windows

Requirements:

- Python 3.11 or newer
- Ollama running locally
- At least one installed Ollama model

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\Install-HeadChef.ps1
.\.venv\Scripts\head-chef.cmd doctor
.\.venv\Scripts\head-chef.cmd models
.\.venv\Scripts\head-chef.cmd route --task "Review this Python function for bugs"
```

Create a job card:

```powershell
.\.venv\Scripts\head-chef.cmd job `
  --project "C:\Projects\MyApp" `
  --task "Add validation to the settings loader" `
  --context-file "src/settings.py" `
  --allowed-file "src/settings.py" `
  --allowed-file "tests/test_settings.py" `
  --acceptance "Invalid JSON returns a clear error" `
  --test "python -m unittest tests.test_settings"
```

Dispatch the generated card:

```powershell
.\.venv\Scripts\head-chef.cmd dispatch --job ".head-chef\jobs\JOB_FILE.json"
```

The explicitly supplied context file is stored in the private job card and sent to the local model. The model only returns text; Codex or the user decides whether and how to apply it.

## Typical workflow

```text
User goal
   ↓
Codex/OpenAI plans and defines acceptance criteria
   ↓
Head Chef classifies the task and checks context size
   ↓
Head Chef selects an installed local model or recommends OpenAI-only
   ↓
A narrow job card is saved
   ↓
The local model produces a bounded result
   ↓
Codex verifies, tests, applies, rejects, or escalates
   ↓
State and evidence are recorded
```

## Commands

| Command | Purpose |
|---|---|
| `doctor` | Check Python-facing Ollama connectivity and local configuration. |
| `models` | List installed models and inferred capabilities. |
| `comfyui-models` | Inventory all ComfyUI model categories and save the result. |
| `refresh` | Diff machine-global names/digests, strength-test new local models, and rebuild the kitchen. |
| `new-cooks` / `onboard` | Detect and evaluate new cooks, rebuild the kitchen, re-seat sprint stations, and report changes. |
| `route` / `plan` | Recommend a model and explain the decision. |
| `budget` | Estimate context usage and split pressure. |
| `job` / `delegate` | Package approved files and create bounded job card(s). |
| `dispatch` | Send one job card to Ollama and save evidence. |
| `benchmark` | Run task-specific structured-output suites on installed models. |
| `review` | Append coordinator verdict without mutating run evidence. |
| `checkpoint` / `resume` | Save or read compact Codex-facing handoff state. |
| `kitchen` | Show local-only specialist stations, alternates, evidence, confidence, and review policy. |
| `cook` | Route, package, dispatch, validate, and record one bounded task. |
| `orchestrate` / `sprint-plan` / `reassign` | Discover sprint files, analyze every phase locally, save assignments, and compare with the prior plan. |
| `sprint-check` / `skill-check` / `check-plan` | Report whether a sprint outline exists, offer to create one when missing, then build the plan. |
| `work` / `run-plan` | Dispatch dependency-ready sprint tasks to preassigned local stations. `--all-ready --parallel N` dispatches every ready task, up to N concurrently. |

## Kitchen routing

Head Chef treats models as stations, not interchangeable general workers:

- coding: coder-specialized models;
- vision: multimodal specialists (image *inspection*, e.g. reviewing a screenshot);
- embedding: embedding-only models through `/api/embed`;
- writing: writing/story specialists;
- planning, retrieval, analysis: capable generalists ranked with task-matched outcomes;
- image_generation, video_generation: asset *creation*, fulfilled by an approved, hash-pinned ComfyUI template (see [Kitchen routing](#kitchen-routing) below and `workflows/comfyui/manifest.json`) rather than an Ollama model. `kitchen`, `orchestrate`, and `work` report these the same way as Ollama stations, marked `"backend": "comfyui"`; a station is `unfilled` until a matching template is registered.

Cloud-tag models are excluded by default. Manual overrides cannot assign a model outside advertised capabilities. Successful, invalid, and timed-out dispatches feed category-specific latency/reliability evidence into future routing.

Installed skills seed new projects with the machine's digest-matched strength evidence. Each dispatch records metadata-only outcomes both in the project and in the private global runtime, so later projects start with improved routing without sharing prompts or responses.

On first use, the skill runs `refresh`, then `orchestrate`. Refresh detects new, changed, and removed Ollama models and inventories every model category exposed by a running ComfyUI, without downloading anything. An offline ComfyUI is reported but does not block Ollama refresh. Orchestration deterministically parses sprint contracts; a local planning model adds needs and risk analysis for every phase. The resulting `.head-chef/orchestration/sprint-plan.json` preserves dependency order.

When models change, one command handles the full flow:

```powershell
head-chef new-cooks --project "C:\Projects\App"
```

It evaluates only new/changed digests, checkpoints every completed case for safe resume, rebuilds the kitchen, regenerates the sprint plan, and writes a reassignment comparison under `.head-chef/orchestration/reassignments/`. A machine-global single-writer lock prevents concurrent refreshes from colliding; stale crash locks recover automatically. Use `--image` when a new vision cook should be tested. `--model MODEL` limits which new cooks are evaluated, while seating still considers the full kitchen. To re-seat against current evidence without inventory work, run `head-chef reassign --project "C:\Projects\App"`. Repeated `--model MODEL` makes a comparison-only candidate roster; add `--apply-roster` only when that restricted plan should replace the active plan.

Sprint assignments are explicit: `assigned`, `conditional` (for example, vision needs an image), `unfilled` (no eligible installed model), or `abstained` (owner/legal/destructive decision). Every task stores a structured profile, assignment reason, score evidence, risk, tool needs, and whether local work is full or advisory.

The local planning station independently recommends a primary station, supporting stations, and local scope for every task. Station and scope disagreements are counted separately, but they are advisory: `work` always dispatches the deterministic assignment and surfaces a `station_disagreement` note on the result for the coordinator to spot-check, rather than blocking. Local advice never silently overrides capability, safety, or abstention gates — those remain hard.

By default, `work` dispatches one dependency-ready task per call. Pass `--all-ready` to dispatch every dependency-ready task in the plan in one call, and `--parallel N` to run up to `N` of them concurrently instead of one at a time — e.g. `head-chef work --project "C:\Projects\App" --all-ready --parallel 3` on a 3-GPU box. Head Chef only avoids serializing its own requests; Ollama's own scheduler (`OLLAMA_NUM_PARALLEL`, `OLLAMA_SCHED_SPREAD`, `CUDA_VISIBLE_DEVICES`) decides how concurrent requests are actually placed across GPUs. `doctor` best-effort detects GPU count and reports a `suggested_work_parallel` value.

Supplied project context is always untrusted data. A schema-valid worker response is successful only when it covers every top-level acceptance criterion and reports no true blockers; incomplete evidence exits `5` and can trigger fallback.

`cook` supports per-task `--context-tokens`, `--output-tokens`, and `--max-attempts`. Context is capped by discovered model metadata. Lower limits drive safe file-boundary splitting. By default, child jobs run locally and a new synthesis job receives their immutable schema-validated evidence. Split children are independent context chunks by construction, so `--parallel N` dispatches up to N of them concurrently, same as `work`. Use `--no-auto-split` only for manual orchestration. Every retry remains immutable.

After `orchestrate`, run `head-chef work --project "C:\Projects\App"` for the first dependency-ready task, or add `--all-ready` for every currently independent ready task. Progress survives restarts in `.head-chef/orchestration/work-ledger.json`. Successful coding/planning work remains coordinator-pending. After review, `head-chef work --project "C:\Projects\App" --accept-task TASK-ID` records the verdict, unlocks dependents, and supplies a bounded accepted-result handoff to the next worker. Output lists `next_ready_task_ids`. Head Chef packages only existing regular files named by the sprint contract; Codex still reviews and applies changes.

Use `head-chef skill-check --project "C:\Projects\App"` for the guided entrypoint. It recursively finds JSON sprint or roadmap outlines below the project, while skipping generated and dependency directories. If none exists, it asks before creating `sprints/SPRINTS.json`, then invokes sprint planning. `sprint-check` and `check-plan` are aliases. Add `--yes` for non-interactive creation.

Install optional Codex skill:

```powershell
.\scripts\Install-CodexSkill.ps1
```

This installs a repository-independent private runtime, the skill, and a stable launcher under your Codex home. Restart Codex, then invoke `$head-chef-local-router`. Use `-Force` to upgrade; registry, benchmarks, benchmark runs, overrides, and outcomes are carried into the upgraded runtime, while a full timestamped backup remains under `backups\head-chef`. Repository `AGENTS.md` also defines direct Head Chef behavior.
| `init` | Create project-local `.head-chef` control files. |

## Project control documents

- [`docs/PROJECT_CHARTER.md`](docs/PROJECT_CHARTER.md)
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- [`docs/BOARD_REVIEW.md`](docs/BOARD_REVIEW.md)
- [`docs/ROADMAP.md`](docs/ROADMAP.md)
- [`docs/SPRINTS.md`](docs/SPRINTS.md)
- [`docs/DECISIONS.md`](docs/DECISIONS.md)
- [`docs/SECURITY.md`](docs/SECURITY.md)
- [`docs/TEST_MATRIX.md`](docs/TEST_MATRIX.md)
- [`docs/STATUS.md`](docs/STATUS.md)
- [`docs/COORDINATOR_AUDIT.md`](docs/COORDINATOR_AUDIT.md)

## Scope discipline

Scope stays narrow: **router, token governor, safe context packaging, typed executors, immutable evidence, verification, diagnostics, and benchmarks**. Dashboard, remote workers, model downloads, autonomous editing, vector memory, and swarms remain deferred.

## Validation boundary

CI-safe tests use mocks and temporary files. No live Ollama, model-quality, GPU, or clean-Windows claim is made unless run on owner hardware. Embeddings are represented by count in CLI evidence; raw vectors are not echoed.

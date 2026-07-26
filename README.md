# Head Chef

Head Chef is a small control layer for **Codex/OpenAI-led projects that use local Ollama models as bounded workers**.

It does not replace Codex, secretly edit files, or invent a complicated autonomous-agent stack. It helps the coordinator answer five practical questions:

1. Which installed local model best fits this task?
2. Is the requested context small enough for that model?
3. How should the work be split into a safe job card?
4. What exactly was sent to the model, and what came back?
5. What should happen when a worker fails or a task is blocked?

## Current v0.1 capabilities

- Detects locally installed Ollama models.
- Infers likely model capabilities from model metadata and names.
- Routes coding, vision, planning, analysis, writing, retrieval, and embedding tasks.
- Explains every routing score and supports a manual override.
- Estimates prompt size and recommends splitting oversized work.
- Creates narrow, persistent JSON job cards.
- Sends a job to Ollama without granting the model shell or filesystem access.
- Records request, response, model, timing, and token metadata.
- Runs a tiny opt-in benchmark against already-installed models.
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
| `route` | Recommend a model and explain the decision. |
| `budget` | Estimate context usage and split pressure. |
| `job` | Create a persistent, bounded job card. |
| `dispatch` | Send one job card to Ollama and save evidence. |
| `benchmark` | Run a small structured-output benchmark on installed models. |
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

## Scope discipline

The first release stays intentionally narrow: **router, token governor, job cards, evidence, diagnostics, and a tiny benchmarker**. A graphical dashboard, remote workers, model downloads, autonomous repository editing, vector-memory infrastructure, and multi-agent swarms are deferred until the core routing loop is proven useful.

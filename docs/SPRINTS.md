# Sprint system

Sprints are intentionally small, sequential where required, independently testable, and safe to stop between.

## Completed foundation sprints

### HC-000 — Establish project authority and scope

- **Goal:** Define Head Chef as a router and governor, not a replacement agent.
- **Files:** `README.md`, `docs/PROJECT_CHARTER.md`, `AGENTS.md`.
- **Acceptance:** Authority, goals, and non-goals are explicit.
- **Status:** Complete.

### HC-001 — Create Ollama read-only inventory client

- **Goal:** Reach local Ollama, list installed models, inspect metadata, and chat.
- **Files:** `src/head_chef/ollama.py`.
- **Exclusions:** Pull, delete, copy, or create models.
- **Tests:** Error paths covered indirectly; live acceptance pending.
- **Status:** Implemented; Windows/Ollama acceptance pending.

### HC-002 — Build conservative model profiles

- **Goal:** Convert Ollama metadata into transparent capabilities and context hints.
- **Files:** `src/head_chef/models.py`.
- **Acceptance:** Embedding-only models cannot be selected for generative work.
- **Status:** Implemented and unit-covered through router fixtures.

### HC-003 — Add Token Governor

- **Goal:** Estimate input size, preserve output room, and recommend splitting.
- **Files:** `src/head_chef/token_governor.py`, `tests/test_token_governor.py`.
- **Acceptance:** Safe, warning, and split states are deterministic.
- **Status:** Complete.

### HC-004 — Add explainable routing

- **Goal:** Score all installed candidates, permit override, abstain when unsupported, and expose reasons.
- **Files:** `src/head_chef/router.py`, `tests/test_router.py`.
- **Acceptance:** Coding, vision, planning, and incompatible override fixtures pass.
- **Status:** Complete for heuristic v0.1.

### HC-005 — Add durable bounded job cards

- **Goal:** Save the exact task contract and render a safe worker prompt.
- **Files:** `src/head_chef/jobs.py`, `tests/test_jobs.py`.
- **Acceptance:** Job cards round-trip and prompts preserve safety requirements.
- **Status:** Complete.

### HC-006 — Add CLI, evidence store, and dispatch

- **Goal:** Expose init, doctor, models, route, budget, job, dispatch, and benchmark commands.
- **Files:** `src/head_chef/cli.py`, `src/head_chef/storage.py`, `src/head_chef/benchmark.py`.
- **Acceptance:** CLI help works; dispatch never executes returned commands.
- **Status:** Implemented; live Ollama acceptance pending.

### HC-007 — Add Windows installation and test scripts

- **Goal:** Install into `.venv` and provide one test command.
- **Files:** `scripts/Install-HeadChef.ps1`, `scripts/Test-HeadChef.ps1`.
- **Acceptance:** Clean Windows test remains pending.
- **Status:** Implemented, unverified on target machine.

## Next execution sprints

### HC-010 — Clean Windows installation acceptance

- **Goal:** Prove installation on Windows 11 without admin rights.
- **Allowed files:** installer, README, packaging metadata.
- **Tasks:** Run installer; run help; run tests; record exact Python and PowerShell versions.
- **Acceptance:** A fresh clone reaches `head-chef --help` and all unit tests pass.
- **Failure recovery:** Record command and full error; do not change architecture during diagnosis.
- **Recommended worker:** Codex/OpenAI; local model may analyze logs.
- **Context load:** Low.

### HC-011 — Owner model inventory acceptance

- **Goal:** Verify profiling for the current installed models.
- **Inputs:** Output from `head-chef models --json`.
- **Tasks:** Check capabilities, parameter size, quantization, and context metadata for every model.
- **Acceptance:** No embedding model routes to generation; vision and coder models are recognized.
- **Recommended worker:** Codex with Head Chef output.
- **Context load:** Low.

### HC-012 — Add explicit model overrides

- **Goal:** Let owner-confirmed capabilities supersede heuristic inference.
- **Allowed files:** config loader, model profiler, tests, docs.
- **Data change:** Optional `.head-chef/model-overrides.json`.
- **Acceptance:** Override can add/remove capability and context; invalid entries fail clearly.
- **Recommended model:** Qwen coder for implementation guidance; Codex reviews.
- **Context load:** Medium.

### HC-013 — Add guided PowerShell job creator

- **Goal:** Ask simple questions and create a job without CLI flags.
- **Allowed files:** new PowerShell script and README.
- **Acceptance:** Prompts for task, project, allowed files, acceptance, and tests; prints the generated path.
- **Recommended model:** Gemma 4 12B or Qwen coder; Codex reviews usability.
- **Context load:** Low.

### HC-014 — Add result review state

- **Goal:** Mark each run accepted, partial, rejected, revision-needed, or blocked.
- **Allowed files:** storage, CLI, tests, docs.
- **Acceptance:** Status history is append-only and preserves reviewer note.
- **Recommended model:** Qwen coder; Codex reviews schema.
- **Context load:** Medium.

### HC-015 — Strengthen task profiles

- **Goal:** Replace broad keyword routing with explicit task profiles and weighted signals.
- **Allowed files:** router, fixtures, docs.
- **Acceptance:** At least 30 deterministic routing fixtures with documented expectations.
- **Recommended worker:** Codex designs; local coder implements fixture-driven changes.
- **Context load:** Medium.

### HC-016 — Add read-only context packager

- **Goal:** Include only explicitly allowed text files in a worker packet.
- **Security:** Reject path traversal, binary files, secrets, oversized files, and files outside project root.
- **Acceptance:** Security and truncation tests pass; no implicit repository scan.
- **Recommended worker:** Codex required due to data boundary.
- **Context load:** High.

### HC-017 — Add task-specific benchmark suite

- **Goal:** Evaluate installed models on coding review, planning, writing, and vision-ready schema compliance.
- **Exclusions:** No universal leaderboard claim.
- **Acceptance:** Results include prompt version, hardware note, model digest, latency, tokens/sec, schema pass, and human usefulness label.
- **Recommended worker:** Codex designs; local models run only on owner hardware.
- **Context load:** Medium.

### HC-018 — Add Codex command contract

- **Goal:** Document stable JSON fields and exit codes so Codex can invoke Head Chef reliably.
- **Acceptance:** Contract tests cover success, abstention, missing model, unreachable Ollama, oversized task, and malformed job.
- **Recommended worker:** Qwen coder with Codex verification.
- **Context load:** Medium.

### HC-019 — Add context-close checkpoint

- **Goal:** Generate a compact handoff from selected job/run/blocker state.
- **Acceptance:** New sessions need charter, status, active job, relevant decisions, and next command—not full history.
- **Recommended worker:** Gemma 4 12B for draft; Codex reviews.
- **Context load:** Medium.

## Release gate

Do not call v0.1 released until:

- unit tests pass;
- clean Windows installation passes;
- Ollama doctor passes;
- the owner’s installed models are profiled correctly;
- one coding, one planning, and one vision route are manually reviewed;
- one job is dispatched and its evidence is recovered;
- security boundaries are confirmed;
- the owner selects a repository license.

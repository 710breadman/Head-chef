# Roadmap

## Milestone 0 — Foundation

**Outcome:** A safe, installable CLI with tested routing primitives.

- M0.1 Repository charter, architecture, board review, decisions, and security boundaries.
- M0.2 Standard-library Ollama client.
- M0.3 Model inventory and conservative capability inference.
- M0.4 Token Governor.
- M0.5 Explainable router with abstention and manual override.
- M0.6 Durable job-card and evidence format.
- M0.7 Unit tests and Windows installer.

## Milestone 1 — Usable local routing

**Outcome:** The owner can route and dispatch real project tasks.

- M1.1 Validate against the owner’s installed model set.
- M1.2 Add explicit model override configuration.
- M1.3 Add richer task profiles: coding review, coding implementation guidance, visual review, research synthesis, writing, and retrieval planning.
- M1.4 Add guided `New-HeadChefJob.ps1` prompts for nontechnical use.
- M1.5 Add result review status: accepted, rejected, needs revision, or blocked.
- M1.6 Add benchmark history without claiming universal model ranking.

## Milestone 2 — Codex integration

**Outcome:** Codex can call Head Chef as a composable project command.

- M2.1 Stable JSON output contract and exit codes.
- M2.2 Codex handoff templates.
- M2.3 Read-only context packager for explicitly allowed files.
- M2.4 Patch-proposal schema that still requires Codex application and tests.
- M2.5 Project policy profiles controlling which task categories may be delegated.
- M2.6 Usage ledger estimating local versus coordinator work.

## Milestone 3 — Continuity and evaluation

**Outcome:** Long projects survive context resets and routing improves from evidence.

- M3.1 Session-start summary generator.
- M3.2 Context-close checkpoint command.
- M3.3 Project-level routing outcomes: useful, partially useful, rejected, or failed.
- M3.4 Task-specific benchmark fixtures.
- M3.5 Regression checks for routing changes.
- M3.6 Optional retrieval over approved project summaries only.

## Milestone 4 — Polished local control center

**Outcome:** A minimal UI is justified by proven workflows.

- M4.1 Read-only local dashboard for models, jobs, runs, blockers, and benchmark history.
- M4.2 Guided job creation.
- M4.3 Progress and cancellation for active Ollama requests.
- M4.4 Clear privacy, cost, model, context, and review badges.
- M4.5 Packaging for simple Windows installation.

## Long-term possibilities

Only after measured need:

- remote Ollama workers on trusted home-network machines;
- hardware-aware scheduling;
- model scouting and owner-approved downloads;
- task-specific learned routing;
- parallel independent review jobs;
- integrations with additional local model servers.

## Explicit non-goals

- Unattended destructive operations.
- Hidden model substitution.
- Unreviewed migrations or public API changes.
- Cloud accounts other than the chosen coordinator.
- A generalized autonomous-agent framework.
# v0.2 roadmap update

Core completed: typed executors, policy invariants, schema v2, immutable attempts, registry/overrides, structured profiles, multidimensional routing signals, safe packaging/splitting, task suites, structured outputs, review loop, Codex contracts.

Next gates:

1. Owner Windows/Ollama acceptance across chat, vision, embedding.
2. Collect digest-matched benchmark and reliability evidence.
3. Add Windows CI matrix plus junction/UNC/ADS adversarial fixtures.
4. Add explicit v1 migration fixtures and response-size transport cap.
5. Owner selects license before public release.

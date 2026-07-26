# Architecture decisions

## ADR-001 — Codex/OpenAI remains coordinator

**Decision:** Head Chef advises and dispatches; Codex/OpenAI plans, verifies, and applies changes.

**Reason:** This minimizes paid usage without assigning architectural authority to smaller local workers.

## ADR-002 — Synchronous CLI before background services

**Decision:** v0.1 uses direct commands and one request at a time.

**Reason:** Easier installation, cancellation, debugging, and recovery.

**Deferred:** Queue daemon, scheduler, supervisor heartbeat, and remote workers.

## ADR-003 — Standard-library Python

**Decision:** Runtime has no third-party Python dependencies.

**Reason:** Simple Windows installation and a smaller supply-chain surface.

## ADR-004 — Project-local file state

**Decision:** Jobs, runs, blockers, handoffs, config, and benchmarks live under `.head-chef/`.

**Reason:** Portable, inspectable, recoverable, and usable across projects.

**Privacy:** `.head-chef/` is ignored by Git by default.

## ADR-005 — Heuristic routing must be explainable

**Decision:** Show all candidate scores, reasons, confidence, and abstention.

**Reason:** Model names and sizes are imperfect proxies; hidden selection would create false trust.

## ADR-006 — Context estimate is advisory

**Decision:** Use a conservative character-based estimate until model-specific tokenizers are justified.

**Reason:** It is fast, dependency-free, and sufficient to prevent obvious prompt overflow.

## ADR-007 — No automatic model management

**Decision:** Head Chef does not pull, remove, copy, or update models.

**Reason:** Downloads consume storage and bandwidth and should remain owner-approved.

## ADR-008 — No execution of local-model output

**Decision:** Dispatch returns and records text only.

**Reason:** The worker cannot prove commands are safe or correct; Codex must validate and apply.

## ADR-009 — License remains an owner decision

**Decision:** Do not add a software license during the greenfield bootstrap.

**Reason:** A license is a legal permission grant and should reflect owner intent.

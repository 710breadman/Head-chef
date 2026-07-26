# Board of experts review

The repository began empty. Each specialist reviewed the intended Head Chef concept independently before the first architecture was consolidated.

## 1. Product director

**Finding:** The valuable product is not “another agent.” It is confidence that the right worker received the right-sized task.

**Risk:** Feature creep into dashboards, swarms, model marketplaces, and full automation before routing proves useful.

**Recommendation:** Keep v1 to inventory, route, budget, job, dispatch, evidence, doctor, and benchmark.

**Rejected:** Automatic model downloads and a large GUI in the first release.

**Highest-priority action:** Make the first successful route understandable to a nontechnical user.

**Confidence:** High.

## 2. Systems architect

**Finding:** The clean boundary is Codex → Head Chef decision → Ollama worker → evidence → Codex verification.

**Risk:** Mixing orchestration, patch application, shell execution, and model serving would create an unsafe monolith.

**Recommendation:** Use small Python modules and a stable JSON job-card contract.

**Rejected:** A permanent queue daemon in v1.

**Highest-priority action:** Preserve strict module and authority boundaries.

**Confidence:** High.

## 3. Local-AI optimization engineer

**Finding:** Installed model names alone are not enough, but they are useful when combined with Ollama metadata and local benchmark evidence.

**Risk:** Treating parameter count or one benchmark as a universal quality score.

**Recommendation:** Explain all score components; let the owner override; reserve output and safety tokens; split at high utilization.

**Rejected:** Hard-coding one model globally.

**Highest-priority action:** Build a tiny benchmarker and context governor before sophisticated learning.

**Confidence:** High.

## 4. Codex workflow specialist

**Finding:** Codex is strongest when given durable project instructions, narrow tasks, verification gates, and explicit next actions.

**Risk:** Delegation can cost more context than it saves when every task includes the entire repository.

**Recommendation:** Job cards must list allowed files, exclusions, acceptance criteria, and tests.

**Rejected:** Passing full chat history to every worker.

**Highest-priority action:** Optimize context packets, not just model selection.

**Confidence:** High.

## 5. Context and memory engineer

**Finding:** Long-term continuity should live in files; only relevant summaries should enter a prompt.

**Risk:** A large “memory” database becomes another uncurated context dump.

**Recommendation:** Start with `.head-chef/jobs`, `runs`, `blockers.md`, and `handoff.md`; add retrieval only after measured need.

**Rejected:** Vector memory as a v1 prerequisite.

**Highest-priority action:** Define context-start and context-close procedures.

**Confidence:** High.

## 6. Security reviewer

**Finding:** The safest useful worker has no shell, no implicit file access, and no authority to apply its answer.

**Risk:** Prompts and responses can contain secrets or private source code.

**Recommendation:** Keep generated state out of Git by default, use local HTTP, avoid credentials, and never execute worker output.

**Rejected:** Global `danger-full-access` and `approval_policy = never` as defaults.

**Highest-priority action:** Make the non-execution boundary explicit in code and documentation.

**Confidence:** High.

## 7. Reliability engineer

**Finding:** Most orchestration failures are ordinary: Ollama unavailable, model missing, timeout, invalid JSON, or oversized input.

**Risk:** A single failed worker stalls an entire roadmap.

**Recommendation:** Return actionable errors, record failed runs, and permit unrelated work to continue.

**Rejected:** Infinite retry loops and silent fallback.

**Highest-priority action:** Add doctor checks and durable failure evidence.

**Confidence:** High.

## 8. QA and evaluation lead

**Finding:** Routing correctness is testable with fixtures; model quality is not proven by a single smoke prompt.

**Risk:** Calling a fast structured-output response “the best model.”

**Recommendation:** Unit-test classification, incompatibility rejection, budgeting, and job persistence. Label benchmark results as hints.

**Rejected:** Fabricated model comparisons without running them on the owner’s machine.

**Highest-priority action:** Establish deterministic tests before adding adaptive routing.

**Confidence:** High.

## 9. Windows deployment engineer

**Finding:** Installation must work through a copy-paste PowerShell command and a local virtual environment.

**Risk:** Requiring Docker, WSL, Node, Redis, or admin privileges would undermine adoption.

**Recommendation:** Standard-library Python, editable install, simple doctor output, and no service installation.

**Rejected:** Containers and background services for v1.

**Highest-priority action:** Verify clean Windows installation.

**Confidence:** Medium until tested on a clean Windows machine.

## 10. UX writer and nontechnical-user advocate

**Finding:** The command names should describe outcomes: `doctor`, `models`, `route`, `budget`, `job`, `dispatch`.

**Risk:** Raw model jargon and unexplained numeric scores.

**Recommendation:** Pair machine-readable JSON with plain explanations and copy-paste examples.

**Rejected:** Requiring users to edit Python or TOML for normal use.

**Highest-priority action:** Add a guided PowerShell wrapper after CLI validation.

**Confidence:** High.

## 11. Open-source and licensing reviewer

**Finding:** The project currently has no owner-selected license.

**Risk:** Adding a license without owner intent can create an unintended permission grant.

**Recommendation:** Keep dependencies at zero and record license selection as an owner decision.

**Rejected:** Copying code from orchestration frameworks merely to accelerate v1.

**Highest-priority action:** Owner chooses a license before a public release.

**Confidence:** High.

## 12. Skeptical technical reviewer

**Finding:** Heuristic routing can look smarter than it is.

**Risk:** A “Head Chef” label creates false confidence while real model behavior varies by quantization, prompt, hardware, and task.

**Recommendation:** Expose every candidate, support manual override, permit “no suitable local model,” and require coordinator review for coding and planning.

**Rejected:** Self-modifying routing rules in v1.

**Highest-priority action:** Make abstention a first-class success state.

**Confidence:** High.

# Architecture council resolution

The board unanimously selected a **small, synchronous, transparent control layer**.

## Required now

- Ollama inventory and metadata.
- Explainable routing with manual override and abstention.
- Context budgeting and split recommendations.
- Durable, narrow job cards.
- Safe text-only dispatch.
- Evidence and failure records.
- Unit tests, doctor, and a tiny benchmark.
- Windows-first installation.

## Deferred

- GUI dashboard.
- Background queue or supervisor.
- Automatic code application.
- Automatic model downloads.
- Cross-machine workers.
- Vector memory and RAG.
- Adaptive learned routing.
- Multi-agent swarms.

## Major tradeoff

The selected architecture is less autonomous, but substantially easier to inspect, test, recover, and trust. That is the correct trade for a coordinator intended to reduce paid usage without sacrificing project quality.

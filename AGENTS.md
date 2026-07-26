# Head Chef repository instructions

## Authority

Codex/OpenAI is the coordinator, reviewer, and final authority. Local models are optional bounded workers.

## Before work

1. Read `README.md`.
2. Read `docs/STATUS.md`.
3. Read only the active section of `docs/SPRINTS.md`.
4. Inspect the relevant source and tests.
5. Use the smallest complete change.

## Local-model delegation

Use Head Chef only when a local model can reduce paid usage without increasing risk.

- Give the worker a narrow task, allowed files, explicit exclusions, acceptance criteria, and tests.
- Do not give a local model the whole repository by default.
- Do not let a local model make architecture, migration, licensing, security, or destructive-action decisions silently.
- Validate all local-model output before applying it.
- Do not silently substitute a different model.
- Run `head-chef kitchen` before delegation when station state may have changed.
- Prefer one-command `head-chef cook` for bounded tasks.
- Route only through advertised station capabilities; cloud-tag models remain excluded.
- Treat exit 3 as a split plan, not completed work.
- Inspect validation and review status before using output.

## Blockers

A non-blocking failure must not stop unrelated work.

- Record the failure in `.head-chef/blockers.md` or `docs/STATUS.md`.
- Preserve commands, errors, and the next exact action.
- Continue with the next independent sprint when safe.

Stop and escalate when:

- requirements conflict;
- stored user data may change;
- a public interface must change;
- a dependency or license is questionable;
- repeated failures suggest an architectural issue;
- acceptance criteria cannot be met honestly.

## Verification

Every completed change needs evidence:

- automated tests where practical;
- a precise manual check otherwise;
- updated documentation when behavior changes;
- no fabricated passes.

## Context close

Before ending a long work session:

1. Stop starting new work.
2. Run relevant tests.
3. Record completed and incomplete work.
4. Update `docs/STATUS.md`.
5. Update blockers and decisions when needed.
6. Record the next exact command or action.
7. Leave the repository at a safe checkpoint.

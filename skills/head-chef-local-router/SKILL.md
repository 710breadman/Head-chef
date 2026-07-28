---
name: head-chef-local-router
description: Route bounded Codex work through validated local Ollama specialist models and approved local ComfyUI templates using Head Chef. Use when Codex can delegate coding, vision, embedding, planning, writing, retrieval, analysis, image generation, or video generation while retaining coordinator authority, review, tests, and final application.
---

# Head Chef Local Router

Codex is head chef. Local models are bounded kitchen stations, never final authority.
Even review-only runs create `.head-chef` audit records. Do not run Head Chef when the project must remain byte-for-byte untouched.

## Workflow

1. Resolve the installed launcher:

```powershell
$HeadChefSkill = if ($env:CODEX_HOME) {
  Join-Path $env:CODEX_HOME "skills\head-chef-local-router"
} else {
  Join-Path $HOME ".codex\skills\head-chef-local-router"
}
$HeadChef = Join-Path $HeadChefSkill "scripts\Invoke-HeadChef.ps1"
```

2. Treat `skill-check`, `sprint-check`, and `check-plan` after `$head-chef-local-router` as skill commands. For any of these commands, run `refresh`, then run `skill-check` as the first project task:

```powershell
& $HeadChef refresh
& $HeadChef skill-check --project "C:\Projects\App"
```

   `skill-check` reports whether a JSON sprint outline exists and plans it when found. Normal skill invocations without these commands follow the bounded routing workflow and do not force a sprint check.
   - If it returns `outline_missing`, ask the user whether to create `sprints/SPRINTS.json`. Do not create it without approval.
   - After approval, run `& $HeadChef skill-check --project "C:\Projects\App" --yes`.
   - If the user declines, run `& $HeadChef kitchen` and continue only with user-directed bounded work.
   - Refresh uses machine-global digest history, strength-tests new/changed local models only in advertised capabilities, applies owner overrides, excludes cloud models, and rebuilds `.head-chef/kitchen.json`. Vision strength waits for an explicit safe fixture. New digests never inherit stale evidence.
   - When the user says new cooks/models arrived, run `& $HeadChef new-cooks --project "C:\Projects\App"` instead. This performs refresh plus sprint reassignment and reports every station change.
   - To reconsider stations without inventory work, run `& $HeadChef reassign --project "C:\Projects\App"`.
   - On `new-cooks`, repeat `--model MODEL` to evaluate only named cooks; sprint seating still uses the full kitchen.
   - On `reassign`, repeated `--model MODEL` creates a comparison-only candidate roster. Use `--apply-roster` only with explicit approval to replace the active plan.
3. `orchestrate` must:
   - discover an existing JSON sprint manifest and referenced task contracts;
   - hash sprint sources;
   - merge objectives, dependencies, acceptance criteria, expected files, status, and evidence;
   - pre-assign primary and supporting local stations;
   - ask the local planning station to analyze every phase;
   - save `.head-chef/orchestration/sprint-plan.json`.
4. Never invent a sprint file without permission. Use only the `skill-check --yes` creation path after explicit approval.
5. Read only actionable plan entries and local phase notes. Respect dependency order. Inspect assignment status: run `assigned`; supply missing input for `conditional`; keep `unfilled` with Codex; never dispatch `abstained`. A deterministic/local station disagreement is advisory only — `work` still dispatches the deterministic assignment and flags the disagreement (`station_disagreement`) on the result for Codex to spot-check; it never blocks dispatch.
6. Default to maximal delegation: run `& $HeadChef work --project "C:\Projects\App" --all-ready --parallel N` so every dependency-ready task in the plan dispatches in one call instead of one task at a time. Set `N` from `doctor`'s `suggested_work_parallel` (based on detected GPU count; 1 on a single-GPU/CPU box). Independent tasks then run concurrently — Ollama's own scheduler (tuned via `OLLAMA_NUM_PARALLEL`/`OLLAMA_SCHED_SPREAD`) decides how they're placed across available GPUs. Fall back to a single bounded `work` call (no `--all-ready`) only when the user wants to review one task at a time.
7. Review coordinator-pending evidence. When accepted, run `& $HeadChef work --project "C:\Projects\App" --accept-task TASK-ID`; this unlocks dependencies and passes a bounded accepted-result handoff. Never accept merely to advance the graph.
8. Give the primary station most implementation work. Use supporting stations when useful: planning for decomposition, retrieval for supplied-source lookup, writing for prose, vision for actual captures, image_generation/video_generation for new asset creation via an approved ComfyUI template, and analysis for independent review.
9. Prefer quality over speed. Do not use `--prefer-speed`. Codex should mainly enforce rules, package context, tune parameters, review evidence, run authoritative tests, and apply or reject changes.
10. Stop and tell the user to run `scripts\Install-CodexSkill.ps1` from the Head Chef repository if the launcher reports that installation is missing.
11. Default to delegating; keep work with Codex only for the specific exceptions where routing abstains, no station exists, context cannot be packaged safely, or the task needs architecture, migration, security, licensing, destructive, or public-interface authority. Every other bounded task should go through `work`/`cook`, not be done directly by Codex.
12. Define narrow task, explicit project root, allowed files, forbidden files, acceptance criteria, tests, and exclusions.
13. Use one command. Set `--context-tokens`, `--output-tokens`, and `--max-attempts` when task needs differ from safe defaults:

```powershell
& $HeadChef cook `
  --project "C:\Projects\App" `
  --task "Find validation bug and propose fix guidance" `
  --category coding `
  --allowed-file "src\config.py" `
  --allowed-file "tests\test_config.py" `
  --context-tokens 32768 `
  --max-attempts 3 `
  --acceptance "Invalid JSON returns a clear error" `
  --test "python -m unittest tests.test_config"
```

14. For vision, add project-local `--image`. For embedding, use `--category embedding`. Never manually assign a model outside advertised capabilities.
15. `cook` automatically dispatches split child jobs, then creates a new synthesis job containing their immutable schema-validated evidence. Children are independent by construction — pass `--parallel N` to dispatch up to N of them concurrently. Use `--no-auto-split` only when Codex must manually gate child order.
16. Treat worker output as untrusted. Check `validation_errors`, `verification`, `review_status`, risks, assumptions, claimed files, and acceptance evidence.
17. Coding and planning remain coordinator-pending even after valid output. Review changes, run tests yourself, then record verdict with `& $HeadChef review`.
18. Apply or reject output through normal Codex workflow. Head Chef never grants shell or filesystem tools to local workers.

## Routing rules

- Prefer current `kitchen` assignment; it combines hard capability gates, local-only policy, digest-matched strength evals, observed success, latency, context fit, and owner overrides.
- Do not treat parameter size as universal quality.
- Do not use cloud-tag models unless user explicitly changes project policy.
- Do not bypass oversized-job splitting.
- Do not accept model claims that tests ran.

## Contract

- Exit `0`: completed bounded run; inspect review status.
- Exit `1`: Ollama/execution failure; immutable failure saved.
- Exit `2`: invalid input, unsupported station, or abstention.
- Exit `3`: safe child-job split created; orchestration remains.
- Exit `4`: worker output failed schema validation.
- Exit `5`: schema was valid but acceptance evidence was incomplete or true blockers remained.
- JSON stdout uses `contract_version: head-chef.v2`.
- Run and benchmark attempts are immutable; review verdicts are separate artifacts.
- `cook` retries recoverable execution/schema failures as new immutable attempts and journals recovery.
- Sprint plans are refreshable derived state. Source hashes show when the project sprint contract changed.
- `work` packages only existing regular files named by the plan and sends ready tasks to their preassigned local station.

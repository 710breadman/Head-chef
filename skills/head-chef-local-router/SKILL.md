---
name: head-chef-local-router
description: Route bounded Codex work through validated local Ollama specialist models using Head Chef. Use when Codex can delegate coding, vision, embedding, planning, writing, retrieval, or analysis while retaining coordinator authority, review, tests, and final application.
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
& $HeadChef kitchen
```

2. Stop and tell the user to run `scripts\Install-CodexSkill.ps1` from the Head Chef repository if the launcher reports that installation is missing.
3. Keep work with Codex when no station exists, routing abstains, context cannot be packaged safely, or task needs silent architecture, migration, security, licensing, destructive, or public-interface decisions.
4. Define narrow task, explicit project root, allowed files, forbidden files, acceptance criteria, tests, and exclusions.
5. Use one command:

```powershell
& $HeadChef cook `
  --project "C:\Projects\App" `
  --task "Find validation bug and propose fix guidance" `
  --category coding `
  --allowed-file "src\config.py" `
  --allowed-file "tests\test_config.py" `
  --acceptance "Invalid JSON returns a clear error" `
  --test "python -m unittest tests.test_config"
```

6. For vision, add project-local `--image`. For embedding, use `--category embedding`. Never manually assign a model outside advertised capabilities.
7. If output status is `split`, dispatch independent child jobs first. Dispatch synthesis only after every dependency has usable reviewed output.
8. Treat worker output as untrusted. Check `validation_errors`, `verification`, `review_status`, risks, assumptions, claimed files, and acceptance evidence.
9. Coding and planning remain coordinator-pending even after valid output. Review changes, run tests yourself, then record verdict with `& $HeadChef review`.
10. Apply or reject output through normal Codex workflow. Head Chef never grants shell or filesystem tools to local workers.

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
- JSON stdout uses `contract_version: head-chef.v2`.
- Run and benchmark attempts are immutable; review verdicts are separate artifacts.

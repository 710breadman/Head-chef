# Project state

- Goal: strongest safe local coordinator: discover model changes, tune context per task, maximize local work, and recover from failures.
- Decisions: benchmark models only in advertised strengths; local-only by default; hard capability gates; Codex retains review and application authority.
- Changes: added machine-global inventory/new-digest eval; adaptive split/child/synthesis; transient retry and capability fallback; dependency-ready `work`; local support review; cross-project isolation fix; source/context safety.
- Tests: 69 passed, 1 Windows symlink privilege skip; live 12-model refresh, forced split/fallback/synthesis, and target-local TBWL-011 Qwen plus Gemma review passed.
- Blockers: clean-machine Windows install and privileged Windows reparse-path acceptance remain unproven; repository license needs owner selection before release.
- Next action: final security/diff audit, push, verify CI; observe a future real model install separately.

# Project state

- Goal: strongest safe local coordinator: discover model changes, tune context per task, maximize local work, and recover from failures.
- Decisions: benchmark models only in advertised strengths; local-only by default; hard capability gates; Codex retains review and application authority.
- Changes: added live `refresh` inventory reconciliation/kitchen persistence; per-task Ollama context/output limits; immutable retry/fallback jobs and recovery journal; public job source redaction; updated skill/docs.
- Tests: 62 passed, 1 Windows symlink privilege skip; compileall and diff check passed; live 12-entry refresh passed; installed-skill Gemma e4b cook honored 8K/512 limits and returned valid structured output in 22.9s.
- Blockers: clean-machine Windows install and privileged Windows reparse-path acceptance remain unproven; repository license needs owner selection before release.
- Next action: commit/push, verify draft PR CI; clean-machine Windows and forced-failure fallback remain acceptance gaps.

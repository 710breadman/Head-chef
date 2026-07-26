# Project state

- Goal: ship an installable skill where Codex routes bounded work to the best locally proven Ollama station and improves routing from digest-bound evidence.
- Decisions: benchmark models only in advertised strengths; local-only by default; hard capability gates; Codex retains review and application authority.
- Changes: strength suite v3.2, seven-station kitchen, one-command `cook`, installable Codex skill, digest-bound jobs/runs/outcomes, immutable benchmark attempts, drift rejection, safe context splitting, prompt-private stdout, contained state paths.
- Tests: 48 passed, 1 Windows symlink test skipped for missing privilege; compile and skill validation passed. All seven assigned stations passed live strength probes on this host.
- Blockers: clean-machine Windows install and privileged Windows reparse-path acceptance remain unproven; repository license needs owner selection before release.
- Next action: commit, push, inspect draft PR CI, then final skeptical completion audit.

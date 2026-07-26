# Project state

- Goal: ship a fully runnable skill where Codex routes bounded work to the best locally proven Ollama station and improves routing from digest-bound evidence.
- Decisions: benchmark models only in advertised strengths; local-only by default; hard capability gates; Codex retains review and application authority.
- Changes: strength suite v3.2, seven-station kitchen, one-command `cook`, installed Codex skill with repository-independent runtime/launcher, global digest-safe evidence fallback and learning, immutable evidence, drift rejection, safe context splitting, prompt-private stdout, contained state paths.
- Tests: 50 passed, 1 Windows symlink test skipped for missing privilege; compile and skill validation passed. Installed skill doctor, kitchen, cook, upgrade, portable import, and global learning passed.
- Blockers: clean-machine Windows install and privileged Windows reparse-path acceptance remain unproven; repository license needs owner selection before release.
- Next action: commit, push, inspect draft PR CI, then restart Codex for skill discovery.

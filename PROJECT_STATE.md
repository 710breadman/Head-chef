# Project state

- Goal: build reviewable Head Chef v0.2 on draft PR branch.
- Decisions: standard-library only; schema v2; separate chat/vision/embed executors; append-only run attempts; manual model choice never weakens review policy; local output remains untrusted.
- Changes: v0.2 contracts, policy, typed executors, immutable attempts, registry, routing, safe context split, structured verification, task suites, Codex commands, Windows CI/docs.
- Tests: 26 unit tests, compileall, CLI help, current-host Windows installer/test passed. Ollama 0.32.3 doctor/model inventory passed; real embedding and coding dispatch passed.
- Blockers: clean Windows, live vision/Gemma, benchmark suite, Windows reparse edge cases, license.
- Next action: final diff review, commit, push, update draft PR.

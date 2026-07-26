# Project state

- Goal: build reviewable Head Chef v0.2 on draft PR branch.
- Decisions: standard-library only; schema v2; separate chat/vision/embed executors; append-only run attempts; manual model choice never weakens review policy; local output remains untrusted.
- Changes: v0.2 plus local-only kitchen stations, strict specialist capability gates, task-matched benchmarks, observed success/latency routing, cloud exclusion, confidence normalization, verifier fixes.
- Tests: 33 unit tests expected; all 11 installed local models exercised only in advertised roles. Real coding, vision, embedding, planning, writing, retrieval, analysis passed. Gemma 12B retrieval timeout preserved.
- Blockers: clean Windows, full station benchmark suite, Windows reparse edge cases, license.
- Next action: run final tests, inspect kitchen map, commit/push, update draft PR.

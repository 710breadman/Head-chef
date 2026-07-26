# Project state

- Goal: best coordinator of local-AI workflows, with sprint-aware evidence-based station assignment and explicit abstention.
- Decisions: remain one focused skill plus local CLI; plugins deferred until stable distribution or MCP/connectors are needed; hard capability gates; Codex remains final authority.
- Changes: structured sprint profiles; explicit assignment states/scope/evidence; hard capability rejection; local station review; untrusted-context boundary; incomplete/blocker runs now fail.
- Tests: 81 passed, 1 Windows symlink privilege skip; TBWL plan found 45 assigned tasks; live Gemma review correctly saved an incomplete blocker run as `ok:false`.
- Blockers: clean-machine Windows install and privileged Windows reparse-path acceptance remain unproven; repository license needs owner selection before release.
- Next action: commit, push, verify draft PR CI.

# Project state

- Goal: best coordinator of local-AI workflows, with sprint-aware evidence-based station assignment and explicit abstention.
- Decisions: remain one focused skill plus local CLI; plugins deferred until stable distribution or MCP/connectors are needed; hard capability gates; Codex remains final authority.
- Changes: restart-safe sprint work ledger; explicit coordinator acceptance; dynamic dependency unlock; bounded accepted-result handoffs; next-ready reporting.
- Tests: 82 passed, 1 Windows symlink privilege skip; installed TBWL command refused acceptance without ledger-backed pending success.
- Blockers: clean-machine Windows install and privileged Windows reparse-path acceptance remain unproven; repository license needs owner selection before release.
- Next action: final tests, push, verify CI.

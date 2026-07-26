# Project state

- Goal: best coordinator of local-AI workflows, with sprint-aware evidence-based station assignment and explicit abstention.
- Decisions: remain one focused skill plus local CLI; plugins deferred until stable distribution or MCP/connectors are needed; hard capability gates; Codex remains final authority.
- Changes: `new-cooks`; resumable targeted evaluation; compare-only rosters; upgrade-safe evidence; single-writer refresh lock; digest-aware reassignment; bounded ComfyUI visual jobs with approved workflows, retry/cancel, output manifests, and Qwen3-VL review.
- Tests: 97 passed, 1 Windows symlink privilege skip; live SDXL generation and Qwen3-VL acceptance verification passed; live Qwen evidence, upgrade hash, full TBWL plan, and contention behavior validated.
- Blockers: clean-machine Windows install and privileged Windows reparse-path acceptance remain unproven; repository license needs owner selection before release.
- Next action: run focused/full tests, final audit, push, verify CI.

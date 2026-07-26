# Coordinator completion audit

## Objective evidence

| Requirement | Evidence | Verdict |
|---|---|---|
| Detect newly installed models | `refresh` compares machine-global name/digest inventory and reports new, updated, removed, unchanged | Proven by unit reconciliation and live 12-model unchanged refresh |
| Add models to kitchen safely | New/changed local digests run only advertised-strength suites; failed current-digest strengths are rejected; cloud tags excluded; stale evidence ignored | Proven by routing/eval/admission tests; new-model live event still requires an actual future install |
| Change context per task | `job`, `cook`, and `work` expose context/output limits; values reach Ollama `num_ctx`/`num_predict` | Proven by unit test and live 8K/512 cook |
| Handle oversized work | Lower task limit drives file-boundary children; children run; immutable evidence feeds a new synthesis job | Proven by live forced 4K two-file split |
| Recover on worker failure | Transient transport retry; invalid structured output can create capability-gated fallback jobs | Proven by unit retry and live Gemma e4b failure to Gemma 26B success |
| Understand premade sprints first | `orchestrate` discovers and hashes sprint sources, merges task contracts, dependencies, acceptance, evidence, and roles | Proven on The Boy Who Lived: 7 sources, 45 tasks, one actionable task |
| Push ready work local | `work` dispatches dependency-ready tasks to preassigned primary models and runs an independent local analysis review | Proven live on TBWL-011 with Qwen Coder plus Gemma reviewer |
| Preserve project isolation | Project-relative context, plan-file traversal filtering, job-path verification, target-local jobs/runs | Proven by tests and corrected live cross-project run |
| Preserve coordinator safety | No local shell/filesystem tools; manual model choice cannot weaken capability/review policy; coding/planning remain pending | Proven by policy tests and live pending coding result |

## Honest boundary

“All local” means all suitable bounded AI inference is routed to installed local models. Local workers do not receive shell or filesystem authority. Codex remains responsible for applying patches, running authoritative commands, security decisions, and final acceptance. This is intentional, not an unimplemented worker permission.

## Remaining external acceptance

- Observe and evaluate a genuinely newly installed future model.
- Clean-machine Windows installation.
- Privileged Windows reparse-point test.
- Repository license selection by owner.

# Test matrix

| Area | Automated | Local acceptance | Human review | Status |
|---|---|---|---|---|
| Token estimation | Unit tests | Not required | Formula sanity | Automated pass |
| Safe/warn/split budget | Unit tests | Not required | Threshold review | Automated pass |
| Coding route | Fixture test | Qwen Coder strength probe | Coordinator review | Automated + local hardware pass |
| Vision route | Fixture test | Qwen VL image probe | Output review | Automated + local hardware pass |
| Large planning route | Fixture test | Gemma comparison | Contract review | Automated + local hardware pass |
| Embedding rejection | Fixture test | Owner model inventory | None | Automated pass |
| Manual missing model | Fixture/contract test | Ollama | Error clarity | Partially covered |
| Job persistence | Unit test | Windows filesystem | Inspect JSON | Automated pass; Windows pending |
| Worker prompt boundary | Unit test | Real dispatch | Review output | Automated + local hardware pass |
| Ollama unavailable | Contract test needed | Stop Ollama | Error clarity | Pending |
| Ollama model inventory | Mock test needed | Real Ollama | Profile review | Pending |
| Structured benchmark | Category/digest tests | Seven assigned stations | Label usefulness | Automated + local hardware pass |
| Windows install | Script syntax only | Clean Windows 11 | Nontechnical flow | Pending |
| Privacy boundary | Documentation | Inspect `.gitignore` | Security review | Initial |
| Oversized dispatch block | Contract tests | Real job | Error clarity | Automated pass |
| Sprint orchestration | Discovery/merge/role/coverage tests | 45-task project, six local phase calls | Role audit | Automated + local hardware pass |

## Test commands

```powershell
.\scripts\Test-HeadChef.ps1
```

or:

```text
python -m unittest discover -s tests -v
```

## Evidence labels

- **Automated pass:** deterministic test completed.
- **Local hardware pass:** completed on the owner’s machine and Ollama setup.
- **Human review pass:** usefulness or clarity was explicitly reviewed.
- **Provisional:** implemented but not proven in the target environment.
- **Fully accepted:** all required labels for the item are complete.
# v0.2 additions

- Executor contract: chat, embedding, missing vision input.
- Policy: coding/planning manual overrides require review.
- Evidence: unique attempt count, create-exclusive overwrite rejection.
- Contracts: valid structured result, prose rejection, acceptance/review gate.
- Context: manifest hashes, dedupe, traversal, binary, deterministic split.
- Registry: valid add/remove/context override and fail-closed unknown fields.
- Routing: embedding hard requirement.

Live `/api/chat`, multimodal vision, `/api/embed`, and model digests passed on this host. Pending: clean Windows install, privileged junction/reparse behavior, UNC/ADS behavior, and GPU/runtime metrics.

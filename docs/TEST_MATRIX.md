# Test matrix

| Area | Automated | Local acceptance | Human review | Status |
|---|---|---|---|---|
| Token estimation | Unit tests | Not required | Formula sanity | Automated pass |
| Safe/warn/split budget | Unit tests | Not required | Threshold review | Automated pass |
| Coding route | Fixture test | Owner model inventory | Confirm useful choice | Automated pass; local pending |
| Vision route | Fixture test | Owner model inventory | Confirm useful choice | Automated pass; local pending |
| Large planning route | Fixture test | Owner model inventory | Confirm useful choice | Automated pass; local pending |
| Embedding rejection | Fixture test | Owner model inventory | None | Automated pass |
| Manual missing model | Fixture/contract test | Ollama | Error clarity | Partially covered |
| Job persistence | Unit test | Windows filesystem | Inspect JSON | Automated pass; Windows pending |
| Worker prompt boundary | Unit test | One real dispatch | Review output | Automated pass; local pending |
| Ollama unavailable | Contract test needed | Stop Ollama | Error clarity | Pending |
| Ollama model inventory | Mock test needed | Real Ollama | Profile review | Pending |
| Structured benchmark | Parser tests needed | Real models | Label usefulness | Pending |
| Windows install | Script syntax only | Clean Windows 11 | Nontechnical flow | Pending |
| Privacy boundary | Documentation | Inspect `.gitignore` | Security review | Initial |
| Oversized dispatch block | Contract test needed | Real job | Error clarity | Pending |

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

Pending target checks: live `/api/chat`, multimodal images, `/api/embed`, model digests, clean Windows install, junction/UNC/ADS behavior, GPU/runtime metrics.

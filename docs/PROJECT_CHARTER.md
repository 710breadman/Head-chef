# Project charter

## Mission

Head Chef helps Codex/OpenAI coordinate installed local AI models with less paid usage, less context waste, and fewer long-project failures.

## Product role

Head Chef is the **planner-support, routing, budgeting, job-card, and evidence layer**. It is not the primary coding agent.

## Authority model

| Role | Authority |
|---|---|
| User | Product owner and final approval authority. |
| Codex/OpenAI | Coordinator, architect, verifier, and escalation layer. |
| Head Chef | Transparent router, token governor, dispatcher, and recorder. |
| Local model | Bounded worker that returns untrusted output. |

## v1 goals

1. Scan installed Ollama models without downloading anything.
2. Choose a sensible model for a narrow task and show why.
3. Support manual model selection.
4. Estimate context pressure before dispatch.
5. Turn broad work into small, inspectable job cards.
6. Preserve requests, responses, metrics, blockers, and handoffs.
7. Keep installation simple for a Windows user who does not code.
8. Continue around non-blocking failures.

## Non-goals for v1

- Replacing Codex.
- Giving local models unrestricted shell access.
- Automatically applying model-generated patches.
- Automatically downloading or deleting models.
- Building a multi-agent swarm.
- Running a permanent background supervisor.
- Adding a vector database before basic project state proves insufficient.
- Building a desktop dashboard before the CLI workflow is validated.

## Success criteria

Head Chef v1 is successful when the owner can:

- install it with one PowerShell script;
- see all available models;
- request a routing recommendation;
- create a job card without understanding Python;
- dispatch the card safely;
- find the saved result and metadata;
- understand why a task was routed, split, rejected, or escalated.

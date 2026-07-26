# Security and privacy

## Threat model

Head Chef processes project descriptions, file paths, prompts, and local-model outputs. These may include private source code, credentials accidentally pasted into prompts, or unsafe instructions produced by a model.

## v0.1 controls

- Default Ollama endpoint is loopback-only: `127.0.0.1`.
- No credential storage.
- No model download, deletion, or update endpoints.
- No shell execution.
- No automatic patch application.
- No implicit repository scan.
- Generated `.head-chef/` state is Git-ignored.
- Every run records its model and metrics.
- Oversized tasks are blocked from dispatch unless explicitly overridden.
- Failed requests are recorded without infinite retry.

## User responsibilities

- Do not include secrets in task text or context files.
- Review job cards before dispatch.
- Review worker output before applying it.
- Keep Ollama bound to trusted interfaces.
- Treat remote Ollama URLs as a separate security decision.

## Required review for future work

The following changes require a dedicated security review:

- file-context packaging;
- remote workers;
- shell or tool execution;
- patch application;
- credentialed providers;
- dashboard network binding;
- plugin loading;
- automatic model downloads;
- persistent databases containing source text.

## Known limitations

- Name-based model capability inference can be wrong.
- A local model can generate malicious or incorrect instructions.
- Git ignore does not protect files from other local users or backup software.
- The current job format does not encrypt private content.
- The first release has not yet been validated on the owner’s Windows machine.

# T05 — Provider abstraction, fake provider, Ollama provider

Tier: mid · Depends on: nothing · Branch: `ai-desktop/t05-provider-fake-and-ollama`

## Read first
- `docs/ai/00-what-is-a-generative-distro.md` (principles)
- `docs/ai/01-contracts.md` (the contract(s) this task implements or consumes)
- `docs/ai/TASKS.md` (how to deliver)


## Do
Write `packages/syn-core/syn_core/providers/{base,fake,ollama}.py` per C5. `fake` replays `fixtures/providers/*.json` keyed by a hash of the last user message; `ollama` calls `POST /api/chat` on `http://127.0.0.1:11434` with `format: json` when a schema is given, `stream: false`, timeout 60 s; a missing server raises `ProviderUnavailable`. Configuration loader for `providers.toml` (user then company, company wins). Tests use `fake` and a stub HTTP server for `ollama` (no network).

## Do not
- Change any contract; if one is wrong, stop and write the problem in the pull request.
- Add dependencies beyond the Python standard library, `jsonschema`, `PyGObject`, `psutil` (Python) or GNOME Shell's own modules (JavaScript).
- Build a shell command from user or model text, anywhere.
- Mention AI assistance, tools or models used to write the code, in code, comments or commit messages.

## Definition of done
Tests pass offline; `syn_core.providers.load()` returns the configured provider or `fake` when `SYN_PROVIDER=fake`.

## Deliver
One pull request on the branch above against `ai-desktop`, with a plain commit message, a
short description of what was built and how it was tested, and the exact command(s) a
reviewer runs. Unit tests run offline in under 60 seconds.

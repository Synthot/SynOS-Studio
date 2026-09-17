# T06 — Planner: prompt, parse, one retry

Tier: mid · Depends on: T04, T05 · Branch: `ai-desktop/t06-planner-prompt-and-parse`

## Read first
- `docs/ai/00-what-is-a-generative-distro.md` (principles)
- `docs/ai/01-contracts.md` (the contract(s) this task implements or consumes)
- `docs/ai/TASKS.md` (how to deliver)
- The task(s) this depends on: `docs/ai/tasks/T04-*.md`, `docs/ai/tasks/T05-*.md`

## Do
Write `packages/syn-core/syn_core/planner.py`: builds the system prompt from the tool declarations and the C2 action list, sends the request (C1) through the provider with the plan JSON schema, parses, validates (T04); on a validation error, retries once with the error appended; returns the plan or a `say`-only plan explaining the failure. Add 8 fixture conversations for the fake provider covering the M1 intents (open app, open path, tile two windows, switch workspace, find a file, unknown request, confirmation needed, provider down).

## Do not
- Change any contract; if one is wrong, stop and write the problem in the pull request.
- Add dependencies beyond the Python standard library, `jsonschema`, `PyGObject`, `psutil` (Python) or GNOME Shell's own modules (JavaScript).
- Build a shell command from user or model text, anywhere.
- Mention AI assistance, tools or models used to write the code, in code, comments or commit messages.

## Definition of done
Tests pass offline with `SYN_PROVIDER=fake`; a prompt fixture file `fixtures/prompts/system.txt` is written by the test so reviewers can read what the model sees.

## Deliver
One pull request on the branch above against `ai-desktop`, with a plain commit message, a
short description of what was built and how it was tested, and the exact command(s) a
reviewer runs. Unit tests run offline in under 60 seconds.

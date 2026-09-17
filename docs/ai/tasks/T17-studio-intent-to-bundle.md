# T17 — Studio: describe the machine in words

Tier: mid · Depends on: T05 · Branch: `ai-desktop/t17-studio-intent-to-bundle`

## Read first
- `docs/ai/00-what-is-a-generative-distro.md` (principles)
- `docs/ai/01-contracts.md` (the contract(s) this task implements or consumes)
- `docs/ai/TASKS.md` (how to deliver)
- The task(s) this depends on: `docs/ai/tasks/T05-*.md`

## Do
In the Studio repository (private), add to the welcome screen a text box 'Describe the machine you need' and a button. The page calls a provider through a small proxy endpoint (`server.py` gains `/api/intent`, keys stay server-side) with the catalog and a JSON schema for the wizard state (`fresh()` shape), then fills the wizard fields and shows what it understood as a review list; nothing is downloaded without the user walking through the steps. Falls back to a message when no provider is configured.

## Do not
- Change any contract; if one is wrong, stop and write the problem in the pull request.
- Add dependencies beyond the Python standard library, `jsonschema`, `PyGObject`, `psutil` (Python) or GNOME Shell's own modules (JavaScript).
- Build a shell command from user or model text, anywhere.
- Mention AI assistance, tools or models used to write the code, in code, comments or commit messages.

## Definition of done
`server.py` unit tests cover the endpoint with the fake provider; the wizard state produced by 5 fixture intents validates with the existing bundle validator.

## Deliver
One pull request on the branch above against `ai-desktop`, with a plain commit message, a
short description of what was built and how it was tested, and the exact command(s) a
reviewer runs. Unit tests run offline in under 60 seconds.

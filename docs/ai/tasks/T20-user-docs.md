# T20 — User documentation

Tier: cheap · Depends on: T10 · Branch: `ai-desktop/t20-user-docs`

## Read first
- `docs/ai/00-what-is-a-generative-distro.md` (principles)
- `docs/ai/01-contracts.md` (the contract(s) this task implements or consumes)
- `docs/ai/TASKS.md` (how to deliver)
- The task(s) this depends on: `docs/ai/tasks/T10-*.md`

## Do
Write `docs/ai/USER.md`: what the assistant can do at M1, how to talk to it (examples in 5 languages), the microphone indicator, what leaves the machine and when, how to see and erase memory, how a company can restrict it. Plain language, no internals.

## Do not
- Change any contract; if one is wrong, stop and write the problem in the pull request.
- Add dependencies beyond the Python standard library, `jsonschema`, `PyGObject`, `psutil` (Python) or GNOME Shell's own modules (JavaScript).
- Build a shell command from user or model text, anywhere.
- Mention AI assistance, tools or models used to write the code, in code, comments or commit messages.

## Definition of done
Reviewed for accuracy against the contracts; every claim maps to a shipped feature or is marked 'M2'.

## Deliver
One pull request on the branch above against `ai-desktop`, with a plain commit message, a
short description of what was built and how it was tested, and the exact command(s) a
reviewer runs. Unit tests run offline in under 60 seconds.

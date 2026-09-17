# T09 — Executor: plan to actions with confirmation and logging

Tier: mid · Depends on: T04, T07, T08 · Branch: `ai-desktop/t09-executor`

## Read first
- `docs/ai/00-what-is-a-generative-distro.md` (principles)
- `docs/ai/01-contracts.md` (the contract(s) this task implements or consumes)
- `docs/ai/TASKS.md` (how to deliver)
- The task(s) this depends on: `docs/ai/tasks/T04-*.md`, `docs/ai/tasks/T07-*.md`, `docs/ai/tasks/T08-*.md`

## Do
Write `packages/syn-core/syn_core/executor.py`: runs a validated plan step by step, substitutes `$ref` values, calls tools from the registry and actions through a `DesktopActions` client interface (an abstract class; T10 provides the D-Bus implementation, tests use an in-memory fake), asks `Confirm` before write/destructive steps, records everything in memory, emits `Step` and `Done` callbacks.

## Do not
- Change any contract; if one is wrong, stop and write the problem in the pull request.
- Add dependencies beyond the Python standard library, `jsonschema`, `PyGObject`, `psutil` (Python) or GNOME Shell's own modules (JavaScript).
- Build a shell command from user or model text, anywhere.
- Mention AI assistance, tools or models used to write the code, in code, comments or commit messages.

## Definition of done
Tests pass with the fake desktop and fake tools; a refused confirmation stops the plan and records the outcome `refused`.

## Deliver
One pull request on the branch above against `ai-desktop`, with a plain commit message, a
short description of what was built and how it was tested, and the exact command(s) a
reviewer runs. Unit tests run offline in under 60 seconds.

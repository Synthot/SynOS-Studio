# T04 — Plan validator (C1)

Tier: cheap · Depends on: T01 · Branch: `ai-desktop/t04-plan-validator`

## Read first
- `docs/ai/00-what-is-a-generative-distro.md` (principles)
- `docs/ai/01-contracts.md` (the contract(s) this task implements or consumes)
- `docs/ai/TASKS.md` (how to deliver)
- The task(s) this depends on: `docs/ai/tasks/T01-*.md`

## Do
Write `packages/syn-core/syn_core/plan.py`: `PLAN_SCHEMA`, `validate_plan(plan, tools: dict[str, decl], actions: set[str]) -> list[str]` checking: schema, every `tool` exists, every `action` is a C2 method name in snake_case (`desktop.tile`), `$ref` targets an earlier step, `needs_confirmation` matches the tools' permission classes. Add `fixtures/plans/` with 5 valid and 5 invalid plans and a test.

## Do not
- Change any contract; if one is wrong, stop and write the problem in the pull request.
- Add dependencies beyond the Python standard library, `jsonschema`, `PyGObject`, `psutil` (Python) or GNOME Shell's own modules (JavaScript).
- Build a shell command from user or model text, anywhere.
- Mention AI assistance, tools or models used to write the code, in code, comments or commit messages.

## Definition of done
Tests pass; the validator's error strings name the step index and the reason.

## Deliver
One pull request on the branch above against `ai-desktop`, with a plain commit message, a
short description of what was built and how it was tested, and the exact command(s) a
reviewer runs. Unit tests run offline in under 60 seconds.

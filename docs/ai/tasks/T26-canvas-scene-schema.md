# T26 — Canvas scene schema, validator and diff

Tier: cheap · Depends on: T02 · Branch: `ai-desktop/t26-canvas-scene-schema`

## Read first
- `docs/ai/00-what-is-a-generative-distro.md` (the mandate and the collaborative 4D canvas)
- `docs/ai/01-contracts.md` (C9 and the contract(s) this task implements or consumes)
- `docs/ai/TASKS.md` (how to deliver)
- The task(s) this depends on: `docs/ai/tasks/T02-*.md`

## Do
Write `packages/syn-core/syn_core/canvas/schema.py` with `SCENE_SCHEMA` and `OBJECT_SCHEMA` (C9, all six kinds), `validate_scene`, and `diff(before, after) -> list[event]` producing C9 scene events with dotted paths. Add `fixtures/scenes/`: 5 valid scenes (one per kind at least, one with 300 objects) and 4 invalid, plus 4 before/after pairs with their expected events.

## Do not
- Change any contract; if one is wrong, stop and write the problem in the pull request.
- Lower the target to fit a budget: if the described interaction needs a rewrite of an existing component, do the rewrite and say so.
- Build a shell command from user or model text, anywhere.
- Mention AI assistance, tools or models used to write the code, in code, comments or commit messages.

## Definition of done
Tests pass; `diff` on the 300-object scene runs under 50 ms; event paths are exactly those in the fixtures.

## Deliver
One pull request on the branch above against `ai-desktop`, with a plain commit message, a
short description of what was built and how it was tested, the exact command(s) a reviewer
runs, and a screen recording for anything visual.

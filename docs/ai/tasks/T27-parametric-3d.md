# T27 — Parametric 3D: spec to mesh

Tier: mid · Depends on: T26 · Branch: `ai-desktop/t27-parametric-3d`

## Read first
- `docs/ai/00-what-is-a-generative-distro.md` (the mandate and the collaborative 4D canvas)
- `docs/ai/01-contracts.md` (C9 and the contract(s) this task implements or consumes)
- `docs/ai/TASKS.md` (how to deliver)
- The task(s) this depends on: `docs/ai/tasks/T26-*.md`

## Do
Write `packages/syn-core/syn_core/canvas/parametric.py`: the C9 parametric tree (box, cylinder, sphere, cone, torus, extrude, revolve, group, union, subtract, materials) rendered to an indexed triangle mesh and exported as glTF 2.0 (pure Python, no external CAD dependency; boolean operations may be approximated by mesh concatenation for version 1 with a note). Add `fixtures/parametric/` with 6 specs (a heat pump, a chair, a gear, a house, a mug, a bracket) and a viewer script rendering them to PNG with a software rasteriser for review.

## Do not
- Change any contract; if one is wrong, stop and write the problem in the pull request.
- Lower the target to fit a budget: if the described interaction needs a rewrite of an existing component, do the rewrite and say so.
- Build a shell command from user or model text, anywhere.
- Mention AI assistance, tools or models used to write the code, in code, comments or commit messages.

## Definition of done
Every fixture exports a glTF that validates with the official glTF validator (run if installed, skipped otherwise); the six PNGs are attached to the pull request and look like the named objects.

## Deliver
One pull request on the branch above against `ai-desktop`, with a plain commit message, a
short description of what was built and how it was tested, the exact command(s) a reviewer
runs, and a screen recording for anything visual.

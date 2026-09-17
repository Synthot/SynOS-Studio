# T29 — 3D objects on the canvas, editable by hand

Tier: mid · Depends on: T27, T28 · Branch: `ai-desktop/t29-canvas-3d-editing`

## Read first
- `docs/ai/00-what-is-a-generative-distro.md` (the mandate and the collaborative 4D canvas)
- `docs/ai/01-contracts.md` (C9 and the contract(s) this task implements or consumes)
- `docs/ai/TASKS.md` (how to deliver)
- The task(s) this depends on: `docs/ai/tasks/T27-*.md`, `docs/ai/tasks/T28-*.md`

## Do
In the canvas engine, render `mesh` objects (glTF from T27) with orbit, gizmo move/rotate/scale of the whole object and of any parametric child, dimension handles on primitives (radius, height, size), and labels anchored to points. Every edit patches the parametric spec, re-meshes, and emits a `changed` event with the dotted path. Undo and redo per object.

## Do not
- Change any contract; if one is wrong, stop and write the problem in the pull request.
- Lower the target to fit a budget: if the described interaction needs a rewrite of an existing component, do the rewrite and say so.
- Build a shell command from user or model text, anywhere.
- Mention AI assistance, tools or models used to write the code, in code, comments or commit messages.

## Definition of done
Editing the heat pump fixture (move the compressor, change a pipe radius) produces the expected events; the re-meshed glTF still validates; a screen recording is attached.

## Deliver
One pull request on the branch above against `ai-desktop`, with a plain commit message, a
short description of what was built and how it was tested, the exact command(s) a reviewer
runs, and a screen recording for anything visual.

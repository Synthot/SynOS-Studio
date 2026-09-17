# T32 — Live applications as canvas objects

Tier: mid · Depends on: T23, T28 · Branch: `ai-desktop/t32-apps-on-canvas`

## Read first
- `docs/ai/00-what-is-a-generative-distro.md` (the mandate and the collaborative 4D canvas)
- `docs/ai/01-contracts.md` (C9 and the contract(s) this task implements or consumes)
- `docs/ai/TASKS.md` (how to deliver)
- The task(s) this depends on: `docs/ai/tasks/T23-*.md`, `docs/ai/tasks/T28-*.md`

## Do
Two implementations of `app` objects, behind one interface: (a) in the compositor spike, application surfaces are rendered as textures placed by the canvas transform (`PlaceWindow`), with input routed through the transform; (b) on GNOME, the extension implements `PlaceWindow` by moving and scaling the real window to the transformed rectangle and hiding it when off-canvas. `PlayMedia` opens the media object in the canvas engine. Document the limits of (b) in `docs/ai/STATUS.md`.

## Do not
- Change any contract; if one is wrong, stop and write the problem in the pull request.
- Lower the target to fit a budget: if the described interaction needs a rewrite of an existing component, do the rewrite and say so.
- Build a shell command from user or model text, anywhere.
- Mention AI assistance, tools or models used to write the code, in code, comments or commit messages.

## Definition of done
Nested compositor: three `weston-terminal` surfaces on the canvas, pan and zoom with input working; GNOME: the same scene with real windows following the canvas; recordings attached.

## Deliver
One pull request on the branch above against `ai-desktop`, with a plain commit message, a
short description of what was built and how it was tested, the exact command(s) a reviewer
runs, and a screen recording for anything visual.

# T28 — The infinite canvas engine

Tier: mid · Depends on: T21, T26 · Branch: `ai-desktop/t28-canvas-engine`

## Read first
- `docs/ai/00-what-is-a-generative-distro.md` (the mandate and the collaborative 4D canvas)
- `docs/ai/01-contracts.md` (C9 and the contract(s) this task implements or consumes)
- `docs/ai/TASKS.md` (how to deliver)
- The task(s) this depends on: `docs/ai/tasks/T21-*.md`, `docs/ai/tasks/T26-*.md`

## Do
Write `prototypes/canvas/`: a full-screen GTK4 window with a WebKitGTK view (no network) running a WebGL scene: infinite pan and zoom (touchpad, wheel, drag, keyboard), objects of the six C9 kinds rendered with the INTERFACE.md depth model and motion system, level-of-detail when zoomed out (objects collapse to their shape and time), a minimap of the whole work. Input and output over a local WebSocket speaking C9 (`Scene`, `Create`, `Update`, `Move`, `SetViewport`, `SceneEvent`). `media` objects play with `<video>` from local files; `app` objects show a placeholder texture until T32.

## Do not
- Change any contract; if one is wrong, stop and write the problem in the pull request.
- Lower the target to fit a budget: if the described interaction needs a rewrite of an existing component, do the rewrite and say so.
- Build a shell command from user or model text, anywhere.
- Mention AI assistance, tools or models used to write the code, in code, comments or commit messages.

## Definition of done
60 fps at 1000 objects on an integrated GPU (measured, printed); `play.sh scenes/atlas.json` shows the fixture; dragging an object emits a `moved` event on the socket; a screen recording is attached.

## Deliver
One pull request on the branch above against `ai-desktop`, with a plain commit message, a
short description of what was built and how it was tested, the exact command(s) a reviewer
runs, and a screen recording for anything visual.

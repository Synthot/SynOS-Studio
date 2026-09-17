# T22 — The generative surface: standalone prototype

Tier: mid · Depends on: T02, T21 · Branch: `ai-desktop/t22-surface-prototype`

## Read first
- `docs/ai/00-what-is-a-generative-distro.md` (the mandate and what science-fiction grade means)
- `docs/ai/01-contracts.md` (the contract(s) this task implements or consumes)
- `docs/ai/TASKS.md` (how to deliver)
- The task(s) this depends on: `docs/ai/tasks/T02-*.md`, `docs/ai/tasks/T21-*.md`

## Do
Write `prototypes/surface/`: a full-screen GTK4 window hosting a WebKitGTK view with no network (or a GTK4 + Cairo/GL scene; say which and why) that renders the INTERFACE.md language: the resting scene, the listening field driven by microphone level (PipeWire, or a `--fake-audio` sine for tests), partial transcript text, a plan drawn as connected steps, and C4 panels appearing on the surface with the motion system. Input: a local WebSocket or stdin JSON stream of C7 signals (`Transcript`, `Plan`, `Step`, `Done`) so it runs without the service. Ship `prototypes/surface/play.sh` that replays `fixtures/surface/*.jsonl`.

## Do not
- Change any contract; if one is wrong, stop and write the problem in the pull request.
- Lower the target to fit a budget: if the described interaction needs a rewrite of an existing component, do the rewrite and say so.
- Build a shell command from user or model text, anywhere.
- Mention AI assistance, tools or models used to write the code, in code, comments or commit messages.

## Definition of done
`play.sh demo-m1.jsonl` runs for 30 seconds at 60 fps on an integrated GPU without dropping below 45 fps (measured, printed at exit); a screen recording is attached to the pull request; every motion uses the parameters from INTERFACE.md.

## Deliver
One pull request on the branch above against `ai-desktop`, with a plain commit message, a
short description of what was built and how it was tested, the exact command(s) a reviewer
runs, and a screen recording for anything visual.

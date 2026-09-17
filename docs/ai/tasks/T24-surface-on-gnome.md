# T24 — The surface inside the GNOME session

Tier: mid · Depends on: T12, T22 · Branch: `ai-desktop/t24-surface-on-gnome`

## Read first
- `docs/ai/00-what-is-a-generative-distro.md` (the mandate and what science-fiction grade means)
- `docs/ai/01-contracts.md` (the contract(s) this task implements or consumes)
- `docs/ai/TASKS.md` (how to deliver)
- The task(s) this depends on: `docs/ai/tasks/T12-*.md`, `docs/ai/tasks/T22-*.md`

## Do
Bring the T22 scene into the GNOME Shell extension as a full-screen layer under the windows (resting scene, listening field, plan drawing) and above them for panels, driven by the real C7 signals from syn-core instead of the replay stream. Windows arranged by `Tile` animate with the motion system rather than jumping. The shell's own panel and overview are hidden while the surface is active and come back on Escape.

## Do not
- Change any contract; if one is wrong, stop and write the problem in the pull request.
- Lower the target to fit a budget: if the described interaction needs a rewrite of an existing component, do the rewrite and say so.
- Build a shell command from user or model text, anywhere.
- Mention AI assistance, tools or models used to write the code, in code, comments or commit messages.

## Definition of done
On a live session, `syn ask` from T10 produces the same sequence as the T22 replay; a screen recording is attached; frame times logged from the extension stay under 16 ms during the demo on QEMU with virtio-gpu.

## Deliver
One pull request on the branch above against `ai-desktop`, with a plain commit message, a
short description of what was built and how it was tested, the exact command(s) a reviewer
runs, and a screen recording for anything visual.

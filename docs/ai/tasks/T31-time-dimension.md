# T31 — Time: timeline store, scrubbing, time-adaptive layout

Tier: mid · Depends on: T08, T26, T28 · Branch: `ai-desktop/t31-time-dimension`

## Read first
- `docs/ai/00-what-is-a-generative-distro.md` (the mandate and the collaborative 4D canvas)
- `docs/ai/01-contracts.md` (C9 and the contract(s) this task implements or consumes)
- `docs/ai/TASKS.md` (how to deliver)
- The task(s) this depends on: `docs/ai/tasks/T08-*.md`, `docs/ai/tasks/T26-*.md`, `docs/ai/tasks/T28-*.md`

## Do
Write `syn_core/canvas/timeline.py`: append-only event log in the memory database (C6), `Timeline(from, to)`, `state_at(scene_id, t)` by replaying events, `BringForward`. In the canvas engine: a time scrubber that seeks the scene to any moment with the motion system (objects fade to their state at `t`), and the time-adaptive rest layout: distance from the working area grows with age, and an hourly profile (counts of object kinds and apps used per hour, kept locally) pre-arranges the working area when the canvas is idle.

## Do not
- Change any contract; if one is wrong, stop and write the problem in the pull request.
- Lower the target to fit a budget: if the described interaction needs a rewrite of an existing component, do the rewrite and say so.
- Build a shell command from user or model text, anywhere.
- Mention AI assistance, tools or models used to write the code, in code, comments or commit messages.

## Definition of done
Replaying 10 000 events to a state takes under 200 ms; scrubbing a fixture day shows the morning and the evening layouts; a screen recording is attached.

## Deliver
One pull request on the branch above against `ai-desktop`, with a plain commit message, a
short description of what was built and how it was tested, the exact command(s) a reviewer
runs, and a screen recording for anything visual.

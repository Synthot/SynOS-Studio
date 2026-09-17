# T21 — Interface language: visual, motion and sound system

Tier: review · Depends on: nothing · Branch: `ai-desktop/t21-interface-language`

## Read first
- `docs/ai/00-what-is-a-generative-distro.md` (the mandate and what science-fiction grade means)
- `docs/ai/01-contracts.md` (the contract(s) this task implements or consumes)
- `docs/ai/TASKS.md` (how to deliver)


## Do
Write `docs/ai/INTERFACE.md`: the design language of the surface. The infinite canvas: depth model (near / working / far), how objects of every kind look at each zoom level, how time is shown (age as distance, the scrubber, a past state), how a proposal appears next to an object, the listening field, how a plan is drawn while it forms, how a generated panel appears, moves and dissolves, colour derived from the brand kit, typography, the sound vocabulary (listening, acknowledged, done, refused), and the motion system as numbers: durations, easings, spring parameters, one table. Include 8 storyboards as SVG frames under `docs/ai/storyboards/` for the M1 demo and for a generated chart. No code.

## Do not
- Change any contract; if one is wrong, stop and write the problem in the pull request.
- Lower the target to fit a budget: if the described interaction needs a rewrite of an existing component, do the rewrite and say so.
- Build a shell command from user or model text, anywhere.
- Mention AI assistance, tools or models used to write the code, in code, comments or commit messages.

## Definition of done
A reader can implement every screen of the M1 demo from this document alone; every animation has a duration and an easing; the storyboards are referenced from the text.

## Deliver
One pull request on the branch above against `ai-desktop`, with a plain commit message, a
short description of what was built and how it was tested, the exact command(s) a reviewer
runs, and a screen recording for anything visual.

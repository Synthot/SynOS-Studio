# T25 — Listening field: the audio-reactive component

Tier: cheap · Depends on: T21 · Branch: `ai-desktop/t25-voice-visualiser`

## Read first
- `docs/ai/00-what-is-a-generative-distro.md` (the mandate and what science-fiction grade means)
- `docs/ai/01-contracts.md` (the contract(s) this task implements or consumes)
- `docs/ai/TASKS.md` (how to deliver)
- The task(s) this depends on: `docs/ai/tasks/T21-*.md`

## Do
Write `prototypes/surface/listening.js` (or `.py` matching T22's choice): a self-contained component that takes a stream of RMS and spectral-centroid values at 50 Hz and renders the listening field described in INTERFACE.md (a field that breathes at rest, focuses when speech starts, and settles when a final transcript arrives). Provide `fixtures/audio/*.json` (recorded levels) and a page or script that replays them.

## Do not
- Change any contract; if one is wrong, stop and write the problem in the pull request.
- Lower the target to fit a budget: if the described interaction needs a rewrite of an existing component, do the rewrite and say so.
- Build a shell command from user or model text, anywhere.
- Mention AI assistance, tools or models used to write the code, in code, comments or commit messages.

## Definition of done
The replay runs at 60 fps; the three states (rest, speech, settled) are visually distinct in the attached recording; parameters are those of INTERFACE.md.

## Deliver
One pull request on the branch above against `ai-desktop`, with a plain commit message, a
short description of what was built and how it was tested, the exact command(s) a reviewer
runs, and a screen recording for anything visual.

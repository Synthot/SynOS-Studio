# T30 — The assistant sees changes and proposes

Tier: mid · Depends on: T06, T26 · Branch: `ai-desktop/t30-ai-sees-and-proposes`

## Read first
- `docs/ai/00-what-is-a-generative-distro.md` (the mandate and the collaborative 4D canvas)
- `docs/ai/01-contracts.md` (C9 and the contract(s) this task implements or consumes)
- `docs/ai/TASKS.md` (how to deliver)
- The task(s) this depends on: `docs/ai/tasks/T06-*.md`, `docs/ai/tasks/T26-*.md`

## Do
Extend the planner: scene events since the last plan are added to the request context; a new plan kind `proposal` (C9) is produced when the assistant has something to suggest after a user edit; the executor shows a proposal next to the object through `canvas.propose` and applies it on `ProposalAnswered(accepted)`. Add the `canvas.*` tools from C9 as registry entries calling `org.synos.Syn.Canvas`. Fixtures: 6 edit sequences with the proposals a good assistant would make (pipes follow the compressor; a label moved off its part; a note next to a video suggesting a chapter).

## Do not
- Change any contract; if one is wrong, stop and write the problem in the pull request.
- Lower the target to fit a budget: if the described interaction needs a rewrite of an existing component, do the rewrite and say so.
- Build a shell command from user or model text, anywhere.
- Mention AI assistance, tools or models used to write the code, in code, comments or commit messages.

## Definition of done
Tests pass offline with the fake provider; a proposal is never applied without `ProposalAnswered(true)`; refused proposals are recorded in memory.

## Deliver
One pull request on the branch above against `ai-desktop`, with a plain commit message, a
short description of what was built and how it was tested, the exact command(s) a reviewer
runs, and a screen recording for anything visual.

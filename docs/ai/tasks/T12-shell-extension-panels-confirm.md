# T12 — GNOME Shell extension: panels and confirmation

Tier: mid · Depends on: T02, T11 · Branch: `ai-desktop/t12-shell-extension-panels-confirm`

## Read first
- `docs/ai/00-what-is-a-generative-distro.md` (principles)
- `docs/ai/01-contracts.md` (the contract(s) this task implements or consumes)
- `docs/ai/TASKS.md` (how to deliver)
- The task(s) this depends on: `docs/ai/tasks/T02-*.md`, `docs/ai/tasks/T11-*.md`

## Do
Add `panel.js` rendering C4 blocks with St widgets (text, table, simple bar chart drawn with Clutter/cairo, form with St.Entry, buttons) in a right-side sliding panel; `ShowPanel`, `ClosePanel`, `Confirm` (a modal dialog with options), and the `PanelAction` signal. Reject specs that fail the limits from T02 (port the checks to JavaScript).

## Do not
- Change any contract; if one is wrong, stop and write the problem in the pull request.
- Add dependencies beyond the Python standard library, `jsonschema`, `PyGObject`, `psutil` (Python) or GNOME Shell's own modules (JavaScript).
- Build a shell command from user or model text, anywhere.
- Mention AI assistance, tools or models used to write the code, in code, comments or commit messages.

## Definition of done
Syntax check passes; the four valid panel fixtures render without exceptions in a headless `gjs` unit that stubs St (a minimal stub is part of the task).

## Deliver
One pull request on the branch above against `ai-desktop`, with a plain commit message, a
short description of what was built and how it was tested, and the exact command(s) a
reviewer runs. Unit tests run offline in under 60 seconds.

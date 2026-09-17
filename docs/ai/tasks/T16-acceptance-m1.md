# T16 — Acceptance: 'open my documents next to the terminal' in QEMU

Tier: mid · Depends on: T15 · Branch: `ai-desktop/t16-acceptance-m1`

## Read first
- `docs/ai/00-what-is-a-generative-distro.md` (principles)
- `docs/ai/01-contracts.md` (the contract(s) this task implements or consumes)
- `docs/ai/TASKS.md` (how to deliver)
- The task(s) this depends on: `docs/ai/tasks/T15-*.md`

## Do
Add a scenario to the acceptance framework (`tests/business/`, read `tests/README.md`): boot the live image, run `syn ask "open my documents next to the terminal"` over the serial shell with `SYN_PROVIDER=fake` and the T06 fixture, then assert through `ListWindows` (via `gdbus` on the serial console) that a Nautilus window and a terminal window exist and are tiled left-right. Screenshot to artifacts.

## Do not
- Change any contract; if one is wrong, stop and write the problem in the pull request.
- Add dependencies beyond the Python standard library, `jsonschema`, `PyGObject`, `psutil` (Python) or GNOME Shell's own modules (JavaScript).
- Build a shell command from user or model text, anywhere.
- Mention AI assistance, tools or models used to write the code, in code, comments or commit messages.

## Definition of done
The scenario runs with `make test` when KVM is available; its JSON report is written even on failure.

## Deliver
One pull request on the branch above against `ai-desktop`, with a plain commit message, a
short description of what was built and how it was tested, and the exact command(s) a
reviewer runs. Unit tests run offline in under 60 seconds.

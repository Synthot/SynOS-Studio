# T18 — Settings page: providers, memory, indicator

Tier: mid · Depends on: T08, T14 · Branch: `ai-desktop/t18-settings-page`

## Read first
- `docs/ai/00-what-is-a-generative-distro.md` (principles)
- `docs/ai/01-contracts.md` (the contract(s) this task implements or consumes)
- `docs/ai/TASKS.md` (how to deliver)
- The task(s) this depends on: `docs/ai/tasks/T08-*.md`, `docs/ai/tasks/T14-*.md`

## Do
A GTK4/libadwaita page in `synos-control-panel` (read its structure first): choose provider, connect a cloud key (stored in the keyring), see and erase memory, set the wake shortcut, and a switch to disable the assistant when policy allows.

## Do not
- Change any contract; if one is wrong, stop and write the problem in the pull request.
- Add dependencies beyond the Python standard library, `jsonschema`, `PyGObject`, `psutil` (Python) or GNOME Shell's own modules (JavaScript).
- Build a shell command from user or model text, anywhere.
- Mention AI assistance, tools or models used to write the code, in code, comments or commit messages.

## Definition of done
The page opens in `synos-control-panel` with `SYN_PROVIDER=fake`; screenshots attached to the PR.

## Deliver
One pull request on the branch above against `ai-desktop`, with a plain commit message, a
short description of what was built and how it was tested, and the exact command(s) a
reviewer runs. Unit tests run offline in under 60 seconds.

# T07 — The six milestone-1 tools

Tier: mid · Depends on: T01 · Branch: `ai-desktop/t07-m1-tools`

## Read first
- `docs/ai/00-what-is-a-generative-distro.md` (principles)
- `docs/ai/01-contracts.md` (the contract(s) this task implements or consumes)
- `docs/ai/TASKS.md` (how to deliver)
- The task(s) this depends on: `docs/ai/tasks/T01-*.md`

## Do
Implement in `packages/syn-core/syn_core/tools/`: `files.search` (walk the home folder with `os.scandir`, case-insensitive substring, skip hidden and `node_modules`, limit), `files.open` (`gio open` via subprocess with an argument list), `apps.list` (parse `.desktop` files with `Gio.DesktopAppInfo`), `apps.launch` (`Gio.DesktopAppInfo.launch_uris`), `system.status` (disk free, battery, network state via `psutil`/`/sys`), `say` (returns its text). Register them with declarations validated by T01.

## Do not
- Change any contract; if one is wrong, stop and write the problem in the pull request.
- Add dependencies beyond the Python standard library, `jsonschema`, `PyGObject`, `psutil` (Python) or GNOME Shell's own modules (JavaScript).
- Build a shell command from user or model text, anywhere.
- Mention AI assistance, tools or models used to write the code, in code, comments or commit messages.

## Definition of done
Unit tests with a temporary home folder and fake `.desktop` files pass; no tool builds a shell string.

## Deliver
One pull request on the branch above against `ai-desktop`, with a plain commit message, a
short description of what was built and how it was tested, and the exact command(s) a
reviewer runs. Unit tests run offline in under 60 seconds.

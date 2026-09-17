# T10 — syn-core D-Bus service and the `syn` command

Tier: mid · Depends on: T03, T06, T09 · Branch: `ai-desktop/t10-core-service-and-cli`

## Read first
- `docs/ai/00-what-is-a-generative-distro.md` (principles)
- `docs/ai/01-contracts.md` (the contract(s) this task implements or consumes)
- `docs/ai/TASKS.md` (how to deliver)
- The task(s) this depends on: `docs/ai/tasks/T03-*.md`, `docs/ai/tasks/T06-*.md`, `docs/ai/tasks/T09-*.md`

## Do
Write `packages/syn-core/syn_core/service.py` exposing C7 `org.synos.Syn.Core` with `Gio.DBusConnection` (session bus), wiring planner and executor; a `DesktopActions` client over D-Bus to `org.synos.Syn.Desktop`; `bin/syn` CLI: `syn ask "…"` prints the plan and the outcome, `syn providers`, `syn memory export|erase`. A systemd user unit `syn-core.service`.

## Do not
- Change any contract; if one is wrong, stop and write the problem in the pull request.
- Add dependencies beyond the Python standard library, `jsonschema`, `PyGObject`, `psutil` (Python) or GNOME Shell's own modules (JavaScript).
- Build a shell command from user or model text, anywhere.
- Mention AI assistance, tools or models used to write the code, in code, comments or commit messages.

## Definition of done
`python3 -m syn_core.service --dry-run` starts without a bus and exits 0; the CLI's argument parsing is unit-tested; a session-bus test runs when `dbus-run-session` exists and is skipped otherwise.

## Deliver
One pull request on the branch above against `ai-desktop`, with a plain commit message, a
short description of what was built and how it was tested, and the exact command(s) a
reviewer runs. Unit tests run offline in under 60 seconds.

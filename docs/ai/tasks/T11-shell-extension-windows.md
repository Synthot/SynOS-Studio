# T11 — GNOME Shell extension: desktop actions for windows and workspaces

Tier: mid · Depends on: T03 · Branch: `ai-desktop/t11-shell-extension-windows`

## Read first
- `docs/ai/00-what-is-a-generative-distro.md` (principles)
- `docs/ai/01-contracts.md` (the contract(s) this task implements or consumes)
- `docs/ai/TASKS.md` (how to deliver)
- The task(s) this depends on: `docs/ai/tasks/T03-*.md`

## Do
Write `packages/gnome-shell-extension-syn/` (`extension.js`, `metadata.json`, `dbus.js`) exporting `org.synos.Syn.Desktop` with `ListWindows`, `FocusWindow`, `CloseWindow`, `MoveWindowToWorkspace`, `Tile` (implement the four layouts with `Meta.Window.move_resize_frame` on the work area), `LaunchApp`, `OpenPath`, `SwitchWorkspace`, `Notify`, `SetIndicator` (a panel indicator with the four states) and the `WindowsChanged` signal. Target GNOME 48 and 50 (`shell-version` in metadata).

## Do not
- Change any contract; if one is wrong, stop and write the problem in the pull request.
- Add dependencies beyond the Python standard library, `jsonschema`, `PyGObject`, `psutil` (Python) or GNOME Shell's own modules (JavaScript).
- Build a shell command from user or model text, anywhere.
- Mention AI assistance, tools or models used to write the code, in code, comments or commit messages.

## Definition of done
`gjs -m` syntax check passes on every file; a `tests/README.md` documents the manual check on a live session; the extension's D-Bus XML is the one from T03 (imported, not copied).

## Deliver
One pull request on the branch above against `ai-desktop`, with a plain commit message, a
short description of what was built and how it was tested, and the exact command(s) a
reviewer runs. Unit tests run offline in under 60 seconds.

# T23 — Our compositor: first spike implementing the desktop actions interface

Tier: mid · Depends on: T03 · Branch: `ai-desktop/t23-compositor-spike`

## Read first
- `docs/ai/00-what-is-a-generative-distro.md` (the mandate and what science-fiction grade means)
- `docs/ai/01-contracts.md` (the contract(s) this task implements or consumes)
- `docs/ai/TASKS.md` (how to deliver)
- The task(s) this depends on: `docs/ai/tasks/T03-*.md`

## Do
Write `compositor/` in Rust on Smithay (latest release): a Wayland compositor that starts nested under an existing session (winit backend) and on a TTY (udev backend), runs XWayland, and exposes `org.synos.Syn.Desktop` (C2) over the session bus with `zbus`: `ListWindows`, `FocusWindow`, `CloseWindow`, `Tile` (four layouts), `LaunchApp`, `SwitchWorkspace`, `Notify` (as an overlay text), `SetIndicator`. Rendering: solid scene with the depth model from INTERFACE.md as three layers, no decorations. Document what Smithay gives for free and what remains (fractional scaling, screen sharing, accessibility) in `compositor/STATUS.md`.

## Do not
- Change any contract; if one is wrong, stop and write the problem in the pull request.
- Lower the target to fit a budget: if the described interaction needs a rewrite of an existing component, do the rewrite and say so.
- Build a shell command from user or model text, anywhere.
- Mention AI assistance, tools or models used to write the code, in code, comments or commit messages.

## Definition of done
`cargo build --release` succeeds on Ubuntu 26.04 with the packages listed in `compositor/README.md`; nested, `gdbus call ... Tile left-right` tiles two `weston-terminal` windows; a screen recording is attached.

## Deliver
One pull request on the branch above against `ai-desktop`, with a plain commit message, a
short description of what was built and how it was tested, the exact command(s) a reviewer
runs, and a screen recording for anything visual.

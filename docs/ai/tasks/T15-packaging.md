# T15 — Packages, stack entries and the synthot-desktop archetype

Tier: mid · Depends on: T10, T11, T12, T13 · Branch: `ai-desktop/t15-packaging`

## Read first
- `docs/ai/00-what-is-a-generative-distro.md` (principles)
- `docs/ai/01-contracts.md` (the contract(s) this task implements or consumes)
- `docs/ai/TASKS.md` (how to deliver)
- The task(s) this depends on: `docs/ai/tasks/T10-*.md`, `docs/ai/tasks/T11-*.md`, `docs/ai/tasks/T12-*.md`, `docs/ai/tasks/T13-*.md`

## Do
Recipes `packages/syn-core`, `packages/syn-voice`, `packages/gnome-shell-extension-syn` (control, assets, scripts; follow `packages/README.md`); add them to `packages/stack.yml` under a new group `assistant` with `only_profile`-free entries; profile `profiles/synthot-desktop.yml` extending `workstation` with the `ai-dev` bundle, `hardware.gpu: none`, the Ollama service, and `policy.ai` defaults; catalog and Studio text for the card.

## Do not
- Change any contract; if one is wrong, stop and write the problem in the pull request.
- Add dependencies beyond the Python standard library, `jsonschema`, `PyGObject`, `psutil` (Python) or GNOME Shell's own modules (JavaScript).
- Build a shell command from user or model text, anywhere.
- Mention AI assistance, tools or models used to write the code, in code, comments or commit messages.

## Definition of done
`make packages` builds the three packages on the builder image; `tools/render_manifest.py --check` passes for a manifest using `synthot-desktop`; unit suite green.

## Deliver
One pull request on the branch above against `ai-desktop`, with a plain commit message, a
short description of what was built and how it was tested, and the exact command(s) a
reviewer runs. Unit tests run offline in under 60 seconds.

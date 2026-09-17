# T14 — Policy (C8) and provider configuration

Tier: cheap · Depends on: nothing · Branch: `ai-desktop/t14-policy-and-config`

## Read first
- `docs/ai/00-what-is-a-generative-distro.md` (principles)
- `docs/ai/01-contracts.md` (the contract(s) this task implements or consumes)
- `docs/ai/TASKS.md` (how to deliver)


## Do
Add `policy.ai` to `schema/profile.schema.json`; the `container_services`-style Ansible role `syn_policy` writes `/etc/syn/policy.toml` from the profile; `syn_core.policy` loads it and exposes `allowed(provider)`, `allowed_tool(decl)`, `memory_mode()`. Unit tests for each combination.

## Do not
- Change any contract; if one is wrong, stop and write the problem in the pull request.
- Add dependencies beyond the Python standard library, `jsonschema`, `PyGObject`, `psutil` (Python) or GNOME Shell's own modules (JavaScript).
- Build a shell command from user or model text, anywhere.
- Mention AI assistance, tools or models used to write the code, in code, comments or commit messages.

## Definition of done
Schema validates the example in C8; tests pass; `tools/render_manifest.py --check` still passes on every manifest in `manifests/`.

## Deliver
One pull request on the branch above against `ai-desktop`, with a plain commit message, a
short description of what was built and how it was tested, and the exact command(s) a
reviewer runs. Unit tests run offline in under 60 seconds.

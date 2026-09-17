# T02 — Panel schema, validator and fixtures

Tier: cheap · Depends on: nothing · Branch: `ai-desktop/t02-panel-schema-validator`

## Read first
- `docs/ai/00-what-is-a-generative-distro.md` (principles)
- `docs/ai/01-contracts.md` (the contract(s) this task implements or consumes)
- `docs/ai/TASKS.md` (how to deliver)


## Do
Write `packages/syn-core/syn_core/panels/schema.py` with `PANEL_SCHEMA` (C4, with the limits as schema constraints where possible and as code checks otherwise) and `validate_panel(spec) -> list[str]`. Add `fixtures/panels/*.json`: 4 valid (text only; table; chart; form with buttons) and 4 invalid (13 blocks; HTML in text; 201 rows; unknown block type). Tests iterate the fixtures.

## Do not
- Change any contract; if one is wrong, stop and write the problem in the pull request.
- Add dependencies beyond the Python standard library, `jsonschema`, `PyGObject`, `psutil` (Python) or GNOME Shell's own modules (JavaScript).
- Build a shell command from user or model text, anywhere.
- Mention AI assistance, tools or models used to write the code, in code, comments or commit messages.

## Definition of done
Tests pass; every fixture file is named `valid-*.json` or `invalid-*.json` and the test asserts accordingly.

## Deliver
One pull request on the branch above against `ai-desktop`, with a plain commit message, a
short description of what was built and how it was tested, and the exact command(s) a
reviewer runs. Unit tests run offline in under 60 seconds.

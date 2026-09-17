# T01 — Tool declaration schema and validator

Tier: cheap · Depends on: nothing · Branch: `ai-desktop/t01-tool-schema-validator`

## Read first
- `docs/ai/00-what-is-a-generative-distro.md` (principles)
- `docs/ai/01-contracts.md` (the contract(s) this task implements or consumes)
- `docs/ai/TASKS.md` (how to deliver)


## Do
Write `packages/syn-core/syn_core/tools/schema.py` with `TOOL_SCHEMA` (JSON Schema for C3 declarations) and `validate_declaration(decl) -> list[str]`. Add `tests/test_tool_schema.py` with 6 cases: a valid declaration, missing permission, unknown permission, parameters not an object schema, name not `^[a-z]+(\.[a-z_]+)+$`, returns missing.

## Do not
- Change any contract; if one is wrong, stop and write the problem in the pull request.
- Add dependencies beyond the Python standard library, `jsonschema`, `PyGObject`, `psutil` (Python) or GNOME Shell's own modules (JavaScript).
- Build a shell command from user or model text, anywhere.
- Mention AI assistance, tools or models used to write the code, in code, comments or commit messages.

## Definition of done
python3 -m unittest packages/syn-core/tests/test_tool_schema.py passes; `python3 -c "from syn_core.tools.schema import validate_declaration"` imports with only the standard library and jsonschema.

## Deliver
One pull request on the branch above against `ai-desktop`, with a plain commit message, a
short description of what was built and how it was tested, and the exact command(s) a
reviewer runs. Unit tests run offline in under 60 seconds.

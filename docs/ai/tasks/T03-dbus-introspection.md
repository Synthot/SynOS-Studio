# T03 — D-Bus introspection XML for C2 and C7

Tier: cheap · Depends on: nothing · Branch: `ai-desktop/t03-dbus-introspection`

## Read first
- `docs/ai/00-what-is-a-generative-distro.md` (principles)
- `docs/ai/01-contracts.md` (the contract(s) this task implements or consumes)
- `docs/ai/TASKS.md` (how to deliver)


## Do
Write `packages/syn-core/data/dbus/org.synos.Syn.Desktop.xml`, `org.synos.Syn.Core.xml`, `org.synos.Syn.Voice.xml` exactly per the tables in `01-contracts.md`, with `<doc:doc>` comments per method. Add `tests/test_dbus_xml.py` that parses each file with `xml.etree` and asserts every method and signal of the contract exists with the right signature.

## Do not
- Change any contract; if one is wrong, stop and write the problem in the pull request.
- Add dependencies beyond the Python standard library, `jsonschema`, `PyGObject`, `psutil` (Python) or GNOME Shell's own modules (JavaScript).
- Build a shell command from user or model text, anywhere.
- Mention AI assistance, tools or models used to write the code, in code, comments or commit messages.

## Definition of done
Tests pass; `gdbus introspect --xml` is not needed (no bus in tests).

## Deliver
One pull request on the branch above against `ai-desktop`, with a plain commit message, a
short description of what was built and how it was tested, and the exact command(s) a
reviewer runs. Unit tests run offline in under 60 seconds.

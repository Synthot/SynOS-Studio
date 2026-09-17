# T08 — Memory store (C6)

Tier: cheap · Depends on: nothing · Branch: `ai-desktop/t08-memory-store`

## Read first
- `docs/ai/00-what-is-a-generative-distro.md` (principles)
- `docs/ai/01-contracts.md` (the contract(s) this task implements or consumes)
- `docs/ai/TASKS.md` (how to deliver)


## Do
Write `packages/syn-core/syn_core/memory.py`: SQLite schema creation, `record_event`, `set_outcome`, `add_fact`, `facts()`, `export_json()`, `erase(event_id|all)`. Path from `$XDG_DATA_HOME` or `~/.local/share/syn/memory.db`; a `session-only` mode keeps it in memory.

## Do not
- Change any contract; if one is wrong, stop and write the problem in the pull request.
- Add dependencies beyond the Python standard library, `jsonschema`, `PyGObject`, `psutil` (Python) or GNOME Shell's own modules (JavaScript).
- Build a shell command from user or model text, anywhere.
- Mention AI assistance, tools or models used to write the code, in code, comments or commit messages.

## Definition of done
Tests pass on a temporary database; the file is created with mode 0600.

## Deliver
One pull request on the branch above against `ai-desktop`, with a plain commit message, a
short description of what was built and how it was tested, and the exact command(s) a
reviewer runs. Unit tests run offline in under 60 seconds.

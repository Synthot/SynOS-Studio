# T13 — syn-voice: push-to-talk over the whisper worker

Tier: mid · Depends on: nothing · Branch: `ai-desktop/t13-voice-service`

## Read first
- `docs/ai/00-what-is-a-generative-distro.md` (principles)
- `docs/ai/01-contracts.md` (the contract(s) this task implements or consumes)
- `docs/ai/TASKS.md` (how to deliver)


## Do
Write `packages/syn-voice/`: a session service exposing C7 `org.synos.Syn.Voice`, recording with PipeWire (`pw-record` argument list, 16 kHz mono) while a shortcut is held or after `StartListening`, streaming to the existing `synos-whisper-worker` (read its protocol in `packages/synos-whisper-worker/upstream/`), emitting `Transcript(text, final)`; language from the session locale. `syn-voice --file sample.wav` transcribes a file for tests.

## Do not
- Change any contract; if one is wrong, stop and write the problem in the pull request.
- Add dependencies beyond the Python standard library, `jsonschema`, `PyGObject`, `psutil` (Python) or GNOME Shell's own modules (JavaScript).
- Build a shell command from user or model text, anywhere.
- Mention AI assistance, tools or models used to write the code, in code, comments or commit messages.

## Definition of done
A fixture WAV under `fixtures/voice/` transcribes to the expected text with the small model when the worker is installed, and the test is skipped otherwise; no audio is ever written to disk.

## Deliver
One pull request on the branch above against `ai-desktop`, with a plain commit message, a
short description of what was built and how it was tested, and the exact command(s) a
reviewer runs. Unit tests run offline in under 60 seconds.

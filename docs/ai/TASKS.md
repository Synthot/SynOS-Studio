# Tasks for the AI-driven desktop

How the work is cut so that it can be given to different people or models,
verified mechanically, and reviewed by us. Read `00-what-is-a-generative-distro.md`
for the why and `01-contracts.md` for the interfaces; each task is a file in
`tasks/`, self-contained.

## Rules for every task

- The contracts are fixed. A task implements or consumes them, never changes
  them; a needed change is a task of its own.
- No task depends on another task's internals: only on files named in
  `01-contracts.md` and on the deliverables listed in its "Depends on".
- Tests run offline, without a display, without a bus unless explicitly
  optional, in under a minute. A task whose tests need hardware says so and
  provides a skip.
- Deliver as one pull request on a branch named `ai-desktop/<task-id>-<slug>`,
  targeting `ai-desktop`. Plain commit messages. No mention of assistance,
  tools or models used to write the code.
- A task that is blocked writes what is missing in the pull request and
  stops; it does not improvise around the block.

## Tiers

- **cheap**: schema, validators, fixtures, documentation, small pure-Python
  modules with tests. Any capable model can do these from the task file
  alone; we review the fixtures, which is where the meaning lives.
- **mid**: modules with behaviour (planner, executor, extension, services).
  A stronger model, or a person; we review design against the contract and
  run the tests.
- **review**: us. Contract changes, the interface language, milestone
  acceptance, anything touching privacy or policy.

Cost is not a criterion for any tier. A task that needs a stronger model or
a rewrite of an existing component gets it; what we save with cheap models
on schemas and fixtures is spent on the interface.

## Task list and order

| Id | Task | Tier | Depends on |
|---|---|---|---|
| T01 | [Tool declaration schema and validator](tasks/T01-tool-schema-validator.md) | cheap | — |
| T02 | [Panel schema, validator and fixtures](tasks/T02-panel-schema-validator.md) | cheap | — |
| T03 | [D-Bus introspection XML for C2 and C7](tasks/T03-dbus-introspection.md) | cheap | — |
| T04 | [Plan validator (C1)](tasks/T04-plan-validator.md) | cheap | T01 |
| T05 | [Provider abstraction, fake provider, Ollama provider](tasks/T05-provider-fake-and-ollama.md) | mid | — |
| T06 | [Planner: prompt, parse, one retry](tasks/T06-planner-prompt-and-parse.md) | mid | T04, T05 |
| T07 | [The six milestone-1 tools](tasks/T07-m1-tools.md) | mid | T01 |
| T08 | [Memory store (C6)](tasks/T08-memory-store.md) | cheap | — |
| T09 | [Executor: plan to actions with confirmation and logging](tasks/T09-executor.md) | mid | T04, T07, T08 |
| T10 | [syn-core D-Bus service and the `syn` command](tasks/T10-core-service-and-cli.md) | mid | T03, T06, T09 |
| T11 | [GNOME Shell extension: desktop actions for windows and workspaces](tasks/T11-shell-extension-windows.md) | mid | T03 |
| T12 | [GNOME Shell extension: panels and confirmation](tasks/T12-shell-extension-panels-confirm.md) | mid | T02, T11 |
| T13 | [syn-voice: push-to-talk over the whisper worker](tasks/T13-voice-service.md) | mid | — |
| T14 | [Policy (C8) and provider configuration](tasks/T14-policy-and-config.md) | cheap | — |
| T15 | [Packages, stack entries and the synthot-desktop archetype](tasks/T15-packaging.md) | mid | T10, T11, T12, T13 |
| T16 | [Acceptance: 'open my documents next to the terminal' in QEMU](tasks/T16-acceptance-m1.md) | mid | T15 |
| T17 | [Studio: describe the machine in words](tasks/T17-studio-intent-to-bundle.md) | mid | T05 |
| T18 | [Settings page: providers, memory, indicator](tasks/T18-settings-page.md) | mid | T08, T14 |
| T19 | [Prompts and transcripts in French, German, Dutch, Spanish](tasks/T19-i18n.md) | cheap | T06 |
| T20 | [User documentation](tasks/T20-user-docs.md) | cheap | T10 |
| T21 | [Interface language: visual, motion and sound system](tasks/T21-interface-language.md) | review | — |
| T22 | [The generative surface: standalone prototype](tasks/T22-surface-prototype.md) | mid | T02, T21 |
| T23 | [Our compositor: first spike implementing the desktop actions interface](tasks/T23-compositor-spike.md) | mid | T03 |
| T24 | [The surface inside the GNOME session](tasks/T24-surface-on-gnome.md) | mid | T12, T22 |
| T25 | [Listening field: the audio-reactive component](tasks/T25-voice-visualiser.md) | cheap | T21 |

Waves (tasks in a wave are independent of each other):

1. T01 T02 T03 T05 T08 T13 T14 T21 — contracts made concrete, and the interface language written.
2. T04 T07 T11 T17 T23 T25 — validators, first behaviour, the compositor spike, the listening field.
3. T06 T09 T12 T19 T22 — planner, executor, panels, languages, the surface prototype.
4. T10 T18 T20 T24 — service, settings, documentation, the surface inside the session.
5. T15 T16 — packaging and the milestone-1 acceptance run, judged on the surface, not on a sidebar.

Tracks: A (Studio) = T17. B (session) = T01–T16, T18–T20. C (interface) = T21–T25.
T21 is ours to write first; every visual task is measured against it.

## Review checklist (what we check on every pull request)

- The task file's "Definition of done" commands were run by the reviewer, not only by the author.
- Fixtures read as things a real person would say and a real plan would do.
- No shell command is built from text; no network in tests; no new dependency.
- Nothing leaves the machine by default; cloud paths are opt-in and visible.
- Privacy and policy behaviour matches `00-what-is-a-generative-distro.md`.
- Commit messages and code carry no attribution to assistance, tools or models.
- Visual work is judged against `INTERFACE.md`: same motion parameters, same depth model, and it looks like nothing that ships today.
- The contract was not changed; if the author found it wrong, the pull request says so and we open a contract task.

## Milestone 1 acceptance (review tier)

On a live image built from `profiles/synthot-desktop.yml`: `syn ask "open my documents
next to the terminal"` with the local Ollama model opens Nautilus on Documents beside the
terminal; the indicator shows listening, thinking, idle; the memory page lists the event; a
company profile with `policy.ai.enabled: false` produces an image where the assistant is
absent from the session.

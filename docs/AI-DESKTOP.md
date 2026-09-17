# The AI-driven desktop

Work in progress on the `ai-desktop` branch. This document is the design;
code follows it, milestone by milestone. Working name for the assistant:
**Syn** (the user may rename it; the name is one string in the brand kit).

## What is different

Every desktop today is a set of windows the person arranges, and a voice
assistant is at best an app inside it. In SynOS the assistant is the shell.
The person says or types what they want to do; the desktop arranges itself
around that intent, and can generate the piece of interface it needs when
no application has it. Three things a user can feel in the first minute:

1. **Talk to the desktop, not to an app.** "Open last month's invoices next
   to my mail and start a reply to the accountant." The shell opens the
   file manager on the right folder, the mail client on the thread, tiles
   both, and drafts the reply in the compose window. No app was told
   anything; the shell drove them.
2. **Interfaces that do not exist yet appear on demand.** "Show me how much
   disk each project takes as a chart." There is no such app: the shell
   generates a small, sandboxed panel (a web view or a GTK scene) fed by a
   tool that reads the disk, and keeps it as long as it is useful.
3. **The user chooses the brain and owns the memory.** Local models by
   default (whisper for speech, an Ollama model for language); cloud
   providers only when the user connects one (OpenAI, Anthropic, Mistral,
   Kexyn); memory in a local, inspectable store the user can wipe. Every
   action the assistant takes is logged and reversible.

The desktop stays a desktop: windows, files, applications, a taskbar. A
person who never talks to it loses nothing. That is the difference between
an AI feature and an AI gimmick.

## What we build on, and why not a compositor first

A Wayland compositor from scratch is one to two years before it is usable
for daily work: input handling, multi-monitor, XWayland, screen sharing,
accessibility, fractional scaling, and the thousand papercuts GNOME solved
over twenty years. The assistant does not need a new compositor to exist;
it needs **control** of the compositor. GNOME Shell (Mutter) exposes that
control through extensions running inside the shell process with full
access to windows, workspaces, input and the panel, plus a D-Bus surface.
SynOS already ships and patches a dozen shell extensions, a session, and a
whisper.cpp voice worker with a GTK front end.

So the plan is: the assistant is built as a **shell layer** (an extension
plus a system service) on the compositor we ship today, behind an
interface of its own (the "desktop actions" API below). When that layer
proves itself, the same API can be implemented by a compositor of our own
(Rust, on Smithay or wlroots) without rewriting the assistant. The
compositor becomes a later milestone with a clear specification, not the
starting point.

## Architecture

```
  voice / keyboard / touch
          │
  ┌───────▼────────┐   speech      ┌──────────────────────┐
  │ Syn shell layer │◄────────────►│ syn-voice (whisper)   │  local, always
  │ (GNOME Shell    │              └──────────────────────┘
  │  extension)     │   intents    ┌──────────────────────┐
  │                 │◄────────────►│ syn-core (system svc) │
  └───────┬────────┘              │  planner · memory ·   │
          │ desktop actions API   │  tools · providers    │
  ┌───────▼────────┐              └──────────┬───────────┘
  │ compositor      │                         │ providers
  │ (Mutter today,  │              ┌──────────▼───────────┐
  │  ours later)    │              │ local: Ollama models  │
  └────────────────┘              │ cloud: OpenAI, Anthropic, Mistral, Kexyn (opt-in)
                                   └──────────────────────┘
```

- **syn-voice**: the existing whisper worker, promoted to a session service
  with wake word and push-to-talk, streaming partial transcripts to the
  shell layer. Entirely local. Already in the stack (`synos-whisper-*`).
- **syn-core**: a Python service (GLib main loop, D-Bus on the session bus)
  holding the planner, the tool registry, the memory store and the provider
  abstraction. It turns a transcript into a plan of desktop actions and tool
  calls, asks for confirmation when an action is destructive, executes,
  and records what it did.
- **Desktop actions API**: a D-Bus interface the shell layer implements:
  list/focus/move/tile/close windows, open an application with arguments,
  open a file, switch workspace, show a generated panel, show a
  confirmation, speak/notify. Small on purpose; every verb is something a
  person could do with a mouse. Implemented today by a GNOME Shell
  extension; later by our compositor.
- **Tools**: functions the planner may call, declared with a schema and a
  permission class: files (search, read metadata, open), applications
  (launch, which ones exist), system (disk, battery, network status),
  calendar/mail through their desktop interfaces, and generated panels.
  Tools that write or delete require confirmation; the class is in the
  declaration, not in the prompt.
- **Providers**: one interface (`complete(messages, tools) → plan`), several
  back ends: Ollama (default, local, the AI workstation profile already
  ships it), OpenAI, Anthropic, Mistral, and Kexyn as Synthot's own
  knowledge provider. The user picks in Settings; the profile can pin or
  forbid providers for a company (`policy.ai.providers`).
- **Memory**: a local SQLite store of what was asked, what was done, and
  facts the user chose to keep ("my accountant is Marie"). Inspectable in
  Settings, exportable, erasable. Nothing leaves the machine unless a cloud
  provider is selected, and then only the current request and the memory
  entries the planner attaches, shown to the user.
- **Generated panels**: the planner may answer with a small UI description
  (a constrained schema: text, table, chart, form, buttons) that the shell
  layer renders in a sandboxed panel; a "web" variant renders HTML in a
  WebKitGTK view with no network. Panels are ephemeral unless pinned.

## Privacy and trust rules (non-negotiable)

- Local by default; every cloud call is a setting the user or the company
  turned on, and the indicator shows when audio or text leaves the machine.
- The microphone is a hardware-state indicator in the panel, never silent.
- Every action is logged with its intent and reversible where the desktop
  allows (window layout, opened apps); destructive tools confirm first.
- A company profile can disable the assistant, restrict providers, or force
  local-only, through the same profile mechanism as every other policy.

## Milestones

**M0 — Design and API** (this document, the D-Bus interfaces, the tool
schema, the panel schema). Reviewable without running anything.

**M1 — Voice to windows.** Wake word or shortcut, whisper transcript,
planner with a local model, the desktop actions API implemented as a GNOME
Shell extension, six tools (find file, open file, launch app, focus/tile
windows, switch workspace, tell me). Deliverable: "open my documents next
to the terminal" works on a live SynOS session. This is the demo that
makes people say "oh, it is different".

**M2 — Generated panels and memory.** The panel schema and renderer,
persistent memory with its Settings page, provider selection, Kexyn as a
provider, company policy.

**M3 — Daily driver.** Mail, calendar and files integration through their
desktop interfaces, dictation into any window, multilingual (French, German,
Dutch, Spanish first, matching the regions), accessibility pass.

**M4 — The compositor.** Only after M1 to M3 hold up: a Rust compositor
implementing the desktop actions API natively, with the layouts the planner
asks for as first-class citizens (intent-driven tiling, ephemeral panels as
surfaces) instead of extension-level tricks. Its specification is the API
that M1 stabilised.

## How it ships

An archetype `synthot-desktop` (profile) that extends `workstation` and adds
the `syn-*` packages, the Ollama service and the models chosen for the
machine; the AI workstation GPU choices apply. Studio shows it as one more
card. Everything else in the image is unchanged, so the assistant can be
added to any existing profile with one line.

## First step of M1

`packages/syn-core`: the service skeleton with the D-Bus interface, the tool
registry and the Ollama provider; `packages/gnome-shell-extension-syn`: the
desktop actions API as an extension; a `syn` command-line client to type
intents before voice is wired. Tested in QEMU with the existing acceptance
framework, which can already drive a GNOME session.

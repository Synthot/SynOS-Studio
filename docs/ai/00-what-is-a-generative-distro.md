# What a generative distro is

A generative distro is an operating system where generation is a system
service, not an application: the machine can produce, on request and from
intent, the three things a person otherwise assembles by hand.

1. **The system itself.** "A workstation for a French accounting firm, with
   eID, locked down, in French and Dutch" becomes a bundle, then an image.
   The generation happens in Studio (intent to configuration) and is checked
   by the same validators a hand-written bundle goes through. This is the
   cheapest and most visible generative feature and needs no new desktop.
2. **The session.** The desktop arranges itself around what the person says
   or types: applications opened, windows tiled, files found, workspaces
   switched. The assistant drives applications; it does not replace them.
3. **Interfaces that do not exist yet.** A chart, a table, a small form: when
   no application has the view the person needs, the assistant generates a
   sandboxed panel from a constrained description. Panels are ephemeral
   unless pinned; they never run arbitrary code.

## Principles that every task must respect

- **Local by default.** Speech (whisper) and language (an Ollama model) run
  on the machine. Cloud providers (OpenAI, Anthropic, Mistral, Kexyn) are a
  setting the user or the company turns on; when on, an indicator shows
  when text or audio leaves the machine.
- **Bounded actions.** The assistant acts only through the desktop actions
  interface and the tool registry, both enumerable and documented. Every
  verb is something a person could do with a mouse. No shell commands from
  prompts, ever.
- **Permission classes on tools, not in prompts.** read, act, write,
  destructive. write and destructive confirm before running.
- **Everything logged, most things reversible.** A local, inspectable,
  erasable memory: what was asked, what was planned, what was done.
- **Policy is data.** A company profile can disable the assistant, restrict
  providers, force local-only, or forbid tool classes (`policy.ai`).
- **The desktop stays a desktop.** A person who never talks to it loses
  nothing. GNOME Shell remains; the assistant is a layer with a small
  interface that a future compositor of our own can implement natively.
- **No new compositor before the interface is proven** (milestone M4 in
  AI-DESKTOP.md).

## Non-goals for the first milestones

Autonomous agents acting without a request; browsing the web on the user's
behalf; cloud accounts as a prerequisite; replacing GNOME applications with
generated ones; a chat window as the primary interface.

## Two tracks, in parallel

- **Track A, Studio: intent to bundle.** One text box on the welcome screen.
  Cheap to build, immediately demonstrable, uses any provider through the
  same abstraction as the desktop.
- **Track B, the session: Syn.** Voice or text to desktop actions, then
  generated panels and memory. The demo is "open my documents next to the
  terminal" working on a live session.

# What a generative distro is

## The mandate

Nothing is impossible. The interface is the product. The target is the
interaction people have only seen in science fiction: you speak, the room
answers; surfaces appear where they are needed and dissolve when they are
not; the machine shows what it is thinking while it works; nothing looks
like a window manager from 1995 with a chat box bolted on. If reaching it
costs rewriting something that already exists, we rewrite it. If it costs
every token we have, so be it. Staging below is a delivery order, never a
ceiling: every milestone is judged by whether it moves the interface toward
that target, not by whether it was cheap.

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
- **The desktop still works as a desktop.** A person who never talks to it
  can still open windows and files. That is a floor, not the design.
- **Our own compositor is the destination.** GNOME Shell carries the first
  demos because it exists today; the desktop actions interface is written
  so the compositor of our own implements it natively, and the compositor
  work starts now, in parallel, as a track of its own (Track C). We do not
  wait for permission from milestone numbers.

## Non-goals for the first milestones

Autonomous agents acting without a request; browsing the web on the user's
behalf; cloud accounts as a prerequisite; replacing GNOME applications with
generated ones; a chat window as the primary interface.

## Three tracks, in parallel

- **Track A, Studio: intent to bundle.** One text box on the welcome screen.
  Cheap to build, immediately demonstrable, uses any provider through the
  same abstraction as the desktop.
- **Track B, the session: Syn.** Voice or text to desktop actions, then
  generated panels and memory. The demo is "open my documents next to the
  terminal" working on a live session.
- **Track C, the interface.** The visual and motion language of the
  science-fiction interface, a running prototype of the generative
  surface (a full-screen scene, not widgets in a sidebar), and the first
  spike of our own compositor. This track sets what the other two must
  look like, and it starts in wave 1.

## What "science-fiction grade" means, concretely

So that tasks can be judged, the interface target is written down:

- **One continuous surface.** No chrome, no title bars, no fixed panel.
  Applications, generated views and the assistant's own output live on the
  same scene, arranged by intent, with depth (near, working, far) rather
  than a stack of rectangles.
- **Voice-reactive.** Speaking produces an immediate visible response: a
  listening field that follows the voice, partial words appearing as they
  are recognised, the plan drawn as it forms, each step lighting up as it
  runs. Latency is hidden by motion, never by a spinner.
- **Generated, not pre-drawn.** Panels, charts, forms, controls are laid out
  by the machine from data and intent, with a single motion system so that
  everything that appears, moves or leaves obeys the same physics.
- **Spatial memory.** Things the person used stay where they were, at a
  distance that reflects age; "the thing from this morning" is a place,
  not a search.
- **Legible thought.** When the assistant works, the person sees what it is
  doing and can stop it with a word or a gesture. Nothing runs invisibly.
- **Beautiful at rest.** With nothing happening, the surface is a calm,
  living scene that carries the brand, not a wallpaper with icons.

These are the acceptance criteria of Track C and, from M2 on, of the
product.

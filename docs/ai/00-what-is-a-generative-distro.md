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
  science-fiction interface, a running prototype of the infinite canvas
  (a full-screen scene, not widgets in a sidebar), the 3D objects and the
  timeline, and the first spike of our own compositor. This track sets what the other two must
  look like, and it starts in wave 1.

## The interface is a collaborative 4D canvas

The surface is an **infinite canvas** shared by the person and the
assistant. Both act on the same scene; neither owns it. On it:

- **Existing applications run as objects.** "Play the video in the Atlas
  folder, over there" places a playing video at that spot; a terminal, a
  browser, a spreadsheet are objects with a position and a size, not
  windows in a stack. The assistant launches, places and arranges them.
- **The assistant draws.** "Draw me what a heat pump looks like" produces a
  3D object on the canvas, editable in place: the person moves a part,
  changes a dimension, adds a label. The assistant sees every change as a
  scene event and proposes updates ("you moved the compressor outside; do
  you want the pipes to follow?"). Drawings, notes, ink, charts, forms and
  generated panels are objects of the same kind.
- **The fourth dimension is time.** Every object carries its timeline
  (created, changed, seen, by whom). The canvas adapts to time: what was
  used this morning is near, last week is far; the layout at 9:00 is not
  the layout at 21:00; the person can scrub the canvas back to any moment
  and see it as it was, and bring an object forward from there. Time is a
  place you can go, not a log you read.
- **Infinite in both directions.** Pan and zoom without limits; zooming
  out shows the shape of the work over months; zooming in reaches the
  pixel of an application or the vertex of a model.

## What "science-fiction grade" means, concretely

So that tasks can be judged, the interface target is written down:

- **One continuous canvas.** No chrome, no title bars, no fixed panel.
  Applications, generated views, 3D objects and the assistant's own output
  are objects on the same infinite scene, arranged by intent and by time,
  with depth (near, working, far) rather than a stack of rectangles.
- **Voice-reactive.** Speaking produces an immediate visible response: a
  listening field that follows the voice, partial words appearing as they
  are recognised, the plan drawn as it forms, each step lighting up as it
  runs. Latency is hidden by motion, never by a spinner.
- **Generated, not pre-drawn.** Panels, charts, forms, controls are laid out
  by the machine from data and intent, with a single motion system so that
  everything that appears, moves or leaves obeys the same physics.
- **Spatial and temporal memory.** Things the person used stay where they
  were, at a distance that reflects age; "the thing from this morning" is a
  place, not a search; the timeline can be scrubbed and any past state of
  the canvas revisited.
- **Two hands on the same object.** The person edits what the assistant
  made, the assistant reacts to what the person changed; the canvas shows
  who did what and when.
- **Legible thought.** When the assistant works, the person sees what it is
  doing and can stop it with a word or a gesture. Nothing runs invisibly.
- **Beautiful at rest.** With nothing happening, the surface is a calm,
  living scene that carries the brand, not a wallpaper with icons.

These are the acceptance criteria of Track C and, from M2 on, of the
product.

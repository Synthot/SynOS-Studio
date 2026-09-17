# Contracts

Every task implements or consumes one of these. They are the only things
two tasks share; a task never depends on another task's internals. Changes
to a contract are their own task and bump the version in the file.

## C1. Intent request and plan (JSON)

A request from the user, and the plan the planner returns. Version 1.

```json
{
  "request": {
    "id": "uuid", "text": "open my documents next to the terminal", "language": "en",
    "source": "voice|text|cli",
    "context": {"focused_window": "id|null", "workspace": 0, "windows": [{"id": "…", "app": "org.gnome.Nautilus", "title": "…"}],
                "time": "2026-09-17T10:00:00Z", "locale": "fr_FR"}
  }
}
```

```json
{
  "plan": {
    "request_id": "uuid",
    "say": "Opening Documents next to the terminal.",
    "steps": [
      {"tool": "apps.launch", "args": {"desktop_id": "org.gnome.Nautilus", "path": "~/Documents"}, "ref": "s1"},
      {"action": "desktop.tile", "args": {"layout": "left-right", "windows": ["$s1.window", "$context.focused_window"]}}
    ],
    "needs_confirmation": false,
    "confidence": 0.86
  }
}
```

Rules: a step is either a `tool` call (registry, C3) or a desktop `action`
(C2). `$ref.field` substitutes a previous step's result; `$context.*`
substitutes the request context. `needs_confirmation` is true when any step
uses a tool of class write or destructive. An empty `steps` with a `say` is a
valid plan (an answer). A plan that references an unknown tool or action is
rejected by the validator; the planner is asked once more with the error.

## C2. Desktop actions (D-Bus, session bus)

Name `org.synos.Syn.Desktop`, path `/org/synos/Syn/Desktop`. Implemented by
the GNOME Shell extension now, by our compositor later. Version 1.

| Method | Signature | Meaning |
|---|---|---|
| `ListWindows` | `() → aa{sv}` | id, app (desktop id), title, workspace, focused, monitor, geometry |
| `FocusWindow` | `(s id)` | |
| `CloseWindow` | `(s id)` | class act |
| `MoveWindowToWorkspace` | `(s id, i index)` | |
| `Tile` | `(s layout, as ids)` | layouts: `left-right`, `top-bottom`, `grid`, `focus` (one window, others hidden) |
| `LaunchApp` | `(s desktop_id, as args) → s window_id` | waits up to 10 s for the window, returns "" on timeout |
| `OpenPath` | `(s path) → s window_id` | default handler |
| `SwitchWorkspace` | `(i index)` | |
| `ShowPanel` | `(s panel_id, s spec_json)` | spec per C4; replaces a panel with the same id |
| `ClosePanel` | `(s panel_id)` | |
| `Notify` | `(s title, s body)` | |
| `Confirm` | `(s question, as options) → s` | modal; returns the chosen option or "" |
| `SetIndicator` | `(s state)` | `idle`, `listening`, `thinking`, `cloud` (text or audio leaving the machine) |
| `PlaceWindow` | `(s id, d x, d y, d z, d scale)` | puts an application on the canvas (C9) at canvas coordinates; version 2 |
| `PlayMedia` | `(s path, d x, d y) → s object_id` | a playing media object on the canvas; version 2 |

Signals: `WindowsChanged()`, `PanelAction(s panel_id, s action_id, s payload_json)`,
`IndicatorClicked()`. Introspection XML is in `packages/syn-core/data/dbus/`.

## C3. Tools (registry entries)

A tool is a Python callable registered with a declaration:

```json
{"name": "files.search", "description": "Find files and folders by name under the home folder",
 "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 10}}, "required": ["query"]},
 "returns": {"type": "object", "properties": {"paths": {"type": "array", "items": {"type": "string"}}}},
 "permission": "read"}
```

`permission` is one of `read`, `act`, `write`, `destructive`. The registry
exposes `list()`, `declarations()` (for the provider), `call(name, args)`.
Milestone 1 tools: `files.search`, `files.open`, `apps.list`, `apps.launch`,
`system.status`, `say`. A tool never runs a shell command built from
arguments; it calls libraries or fixed executables with argument lists.

## C4. Generated panels (JSON)

A panel is a constrained description rendered by the shell layer. Version 1.

```json
{"type": "panel", "title": "Disk use by project", "pinned": false,
 "blocks": [
   {"type": "text", "text": "Three projects use 41 GB."},
   {"type": "table", "columns": ["Project", "Size"], "rows": [["atlas", "22 GB"], ["nimbus", "12 GB"]]},
   {"type": "chart", "kind": "bar", "labels": ["atlas", "nimbus"], "series": [{"name": "GB", "values": [22, 12]}]},
   {"type": "form", "fields": [{"id": "name", "label": "Name", "kind": "text"}, {"id": "when", "label": "When", "kind": "date"}]},
   {"type": "buttons", "buttons": [{"id": "clean", "label": "Clean caches", "style": "primary"}]}
 ]}
```

Limits: at most 12 blocks, table 200 rows, chart 50 points, text 2000
characters, no HTML, no URLs opened without a `buttons` action the user
clicks. A button click arrives as `PanelAction(panel_id, button_id, form_json)`.

## C5. Provider interface (Python)

```python
class Provider:
    name: str                       # "ollama", "openai", "anthropic", "mistral", "kexyn", "fake"
    local: bool                     # True when nothing leaves the machine
    def complete(self, messages: list[dict], tools: list[dict], *, json_schema: dict | None = None,
                 max_tokens: int = 1024) -> dict:   # returns {"content": str, "tool_calls": [...], "usage": {...}}
```

Configuration in `~/.config/syn/providers.toml` (user) and
`/etc/syn/providers.toml` (company, wins on conflict); API keys in the
keyring, never in files. `fake` replays fixtures for tests.

## C6. Memory (SQLite, `~/.local/share/syn/memory.db`)

`events(id, ts, request_json, plan_json, outcome_json, provider, cloud bool)`,
`facts(id, ts, key, value, source_event)`, `settings(key, value)`. Exportable
as JSON, erasable per row or entirely. The Settings page reads it.

## C7. Core and voice services (D-Bus, session bus)

`org.synos.Syn.Core` at `/org/synos/Syn/Core`: `Ask(s text, a{sv} opts) → s request_id`,
`Cancel(s request_id)`, `Providers() → aa{sv}`, `SetProvider(s name)`;
signals `Plan(s request_id, s plan_json)`, `Step(s request_id, s ref, s status, s result_json)`,
`Done(s request_id, s outcome_json)`.

`org.synos.Syn.Voice` at `/org/synos/Syn/Voice`: `StartListening()`, `StopListening()`,
`State() → s`; signals `Transcript(s text, b final)`, `StateChanged(s state)`.

## C8. Policy (profile schema, `policy.ai`)

```yaml
policy:
  ai:
    enabled: true
    providers: [ollama, kexyn]      # allowed; empty means all
    local_only: false
    tools_denied: [destructive]     # permission classes or tool names
    memory: enabled                 # enabled | session-only | disabled
```

## C9. The canvas (scene model and D-Bus)

The canvas is a scene graph shared by the person and the assistant. Version 1.
Coordinates are canvas units (1 unit = 1 CSS pixel at zoom 1), unbounded; `z`
is depth (0 working, negative far, positive near); `t` is a UTC timestamp.

```json
{"scene": {"id": "uuid", "viewport": {"x": 0, "y": 0, "zoom": 1, "t": "now"},
 "objects": [
  {"id": "o1", "kind": "app",   "app": "org.gnome.Nautilus", "window": "w-12",
   "transform": {"x": 100, "y": 80, "z": 0, "w": 900, "h": 600, "rotation": 0, "scale": 1},
   "time": {"created": "…", "changed": "…", "seen": "…"}, "by": "user|syn"},
  {"id": "o2", "kind": "media", "path": "~/Atlas/site.mp4", "state": "playing", "position_s": 12.4, "transform": {"…": 0}},
  {"id": "o3", "kind": "mesh",  "format": "parametric|gltf", "spec": {"…": 0}, "labels": [{"id": "l1", "text": "compressor", "anchor": [0.2, 0.1, 0]}]},
  {"id": "o4", "kind": "panel", "spec": {"type": "panel", "…": 0}},
  {"id": "o5", "kind": "ink",   "strokes": [[[x, y, pressure, t]]]},
  {"id": "o6", "kind": "note",  "text": "…"}
 ]}}
```

**Parametric 3D** (what the assistant generates and edits; rendered to a mesh
by the canvas): a tree of primitives and operations,
`{"node": "group", "children": [{"node": "cylinder", "r": 0.3, "h": 1.2, "at": [0,0,0], "material": "steel"}, {"node": "box", "size": [1,0.6,0.4], "at": [1.2,0,0]}]}`
with `union`, `subtract`, `extrude(path)`, `revolve(path)`, materials by name,
metres as unit. `gltf` objects carry a glTF 2.0 file for meshes the assistant
did not generate. Editing in place by the person produces **scene events**.

**Scene events** (what the assistant sees): `{"event": "changed", "object": "o3", "by": "user",
"t": "…", "diff": {"spec.children[0].at": [[0,0,0], [2,0,0]]}}`, also `created`,
`deleted`, `moved`, `seen`, `viewport`. Events are appended to the object's
history and to the timeline; the planner receives the events since its last
plan as context and may answer with proposals: a plan whose steps are
`canvas.*` tools flagged `proposal: true`, shown next to the object and applied
only when the person accepts.

**Time.** `Timeline(t_from, t_to)` returns the events in a range;
`SeekTime(t)` renders the scene as it was at `t` (objects with their state at
`t`, others faded); `BringForward(object_id, t)` copies an object's state at `t`
into the present. The layout at rest is a function of time: distance from the
working area grows with age, and a per-hour profile (learned locally from the
timeline) pre-arranges the working area.

**D-Bus** `org.synos.Syn.Canvas` at `/org/synos/Syn/Canvas`:
`Scene() → s json`, `Create(s object_json) → s id`, `Update(s id, s patch_json)`,
`Delete(s id)`, `Move(s id, d x, d y, d z)`, `SetViewport(d x, d y, d zoom)`,
`Timeline(s t_from, s t_to) → s json`, `SeekTime(s t)`, `BringForward(s id, s t)`;
signals `SceneEvent(s event_json)`, `ProposalAnswered(s proposal_id, b accepted)`.

**Canvas tools** for the planner (C3, all class `act` except `canvas.delete`
which is `write`): `canvas.place_app`, `canvas.play_media`, `canvas.draw`
(parametric spec), `canvas.update` (patch), `canvas.note`, `canvas.panel`,
`canvas.arrange` (layout by intent), `canvas.seek_time`, `canvas.propose`.

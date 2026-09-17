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

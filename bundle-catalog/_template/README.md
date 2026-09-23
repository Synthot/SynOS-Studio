# Bundle catalog template

Copy this folder to `bundle-catalog/<your-id>/`, rename `template-appliance`
to your id everywhere (the folder, `bundle.json`, `manifests/*.yml`, and
`profiles/*.yml` if you ship one), and edit `profiles/<your-id>.yml` to
describe your appliance. See docs/BUNDLE.md, "The bundle catalog", for the
full contribution guide and what a reviewer checks.

This folder is not itself a catalog entry: it is not listed in
`bundle-catalog/index.yml`, and the tests skip it by name. It is a
starting point, not part of the exported catalog.

When you add your entry to `bundle-catalog/index.yml`, keep `summary` and
`first_boot` separate — a card is not a manual page:

```yaml
  - id: your-id
    name: Your Appliance
    summary: "One sentence: what it is and what it runs. No commands, no 'first boot' clause."
    first_boot:
      - "The first thing to do, in a line or two. A command may appear inside a step."
      - "The next thing, if there is one."
    tags: [...]
    folder: your-id
    services: [...]      # software.services names, [] if none
    ports: [...]         # bare numbers security.open_ports opens by default
    verified: true        # only after you actually checked the package names and image tags
    contributed_by: you   # optional
```

`first_boot: []` is correct, not lazy, when there is no meaningful first
step (see `ai-workstation` or either Yocto entry in `index.yml`) — do not
invent a step to fill the list.

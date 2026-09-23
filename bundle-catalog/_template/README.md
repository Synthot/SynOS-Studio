# Bundle catalog template

Copy this folder to `bundle-catalog/<your-id>/`, rename `template-appliance`
to your id everywhere (the folder, `bundle.json`, `manifests/*.yml`, and
`profiles/*.yml` if you ship one), and edit `profiles/<your-id>.yml` to
describe your appliance. See docs/BUNDLE.md, "The bundle catalog", for the
full contribution guide and what a reviewer checks.

This folder is not itself a catalog entry: it is not listed in
`bundle-catalog/index.yml`, and the tests skip it by name. It is a
starting point, not part of the exported catalog.

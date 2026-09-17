# Backlog

Agreed but not built. Each item says what it is and where it would go.

## Next, for the public

The public flow is the product; corporate features wait behind these.
The hosted Studio is the corporate offer: convenience on the same open
engine, never captivity (a company leaves with its bundle and builds alone).

1. Builder images published on the registry by the workflow: without them a
   downloaded bundle cannot build.
2. The site on its domain (the Studio workflow produces the single file).
3. Screenshots in the gallery.
4. The acceptance suite run in QEMU (`make test`).
5. One end-to-end run by an outsider: download, unzip, launcher, boot; read
   their `dist/build.log`.


## Product

- **Headless mode for the AI workstation and the server.** A "Desktop /
  Headless server" switch on those cards. Headless installs the same image
  but boots to the console with SSH and Cockpit; the desktop stays on disk
  and can be started later. Frees the GPU from the desktop session. Profile
  field `system.headless`, applied by the first-boot services unit
  (`systemctl set-default multi-user.target`), ports of the AI services
  opened. A desktop-free media type is deliberately not planned.
- **One brand, several machine types in one bundle.** Bundle format 2:
  several manifests sharing one brand kit, the engine building each; the
  workspace feature of the corporate mode needs it.
- **Corporate mode of the tool.** Lock input for reproducible rebuilds
  (`packages.lock` in, identical image out), an Ansible module around
  `synos build --json`, CI templates taking a bundle instead of a manifest,
  per-tenant repositories.
- **Company applications (corporate).** A "Your applications" step:
  a company APT repository with its key (the right channel, it carries
  updates), a `.deb` or AppImage by URL and SHA-256, a Flatpak remote; the
  app store inside the image lists them under the company's name
  (`software.store.catalog`). The engine already accepts all four forms;
  only Studio and the store catalog are missing. Source builds stay on the
  company's side: the engine's recipe format and package builder are public
  and run in their CI; the bundle references the result.
- **External recipe directories (corporate).** `packages.recipes:
  [../acme-apps]` in the manifest: the package builder builds recipes from
  those folders exactly like `packages/`, signed into the image repository,
  fingerprinted, in the SBOM. A company keeps its applications' recipes in
  its own repository and never forks the engine. Bundles never carry
  recipes: a downloaded zip stays data.
- **Studio workspace.** Accounts and organisations on one platform, saved
  bundle versions with history, pipeline pickup.
- **Immutable workstation.** See ARCHITECTURE.md, transactional updates.

## Quality and evidence

- **Screenshots for the Studio gallery.** `screenshots/` in the Studio repo;
  the page embeds what it finds.
- **Run the acceptance suite in QEMU** (`make test`). Never executed in this
  project; the single-entry boot menu, the themed GRUB path on amd64 and the
  region override arguments are new code that this suite is meant to prove.
- **Boot an AI workstation image on real GPU hardware**: NVIDIA first, then
  AMD; check Ollama answers on 11434 and the CDI generation at boot.
- **Boot a server image**: SSH reachable after first boot, Cockpit on 9090,
  firewall openings applied.
- **macOS launcher** on an Intel and an Apple Silicon Mac.
- **French, German and Dutch guest UI labels** for the desktop acceptance
  suites (they skip until labels exist).
- **Canadian and Belgian locales in the installer's language list.** The
  list knows fr_FR and en_US but not fr_CA, en_CA, nl_BE, fr_BE, so a
  bundle for Canada shows France first. Match by language when the exact
  locale is absent, and add the regional variants the regions declare.
- **Login screen background follows the brand.** GDM shows GNOME's stock
  blue artwork behind the brand wordmark; the greeter's background comes
  from the shell theme, not from the desktop background, so the brand kit
  needs a greeter stylesheet override with its own colours or wallpaper.
- **No extension update toast at first boot.** "Dash to Panel has been
  updated" appears in the live session; the version notice of the
  extension must be silenced in the image.
- **No update notifications in the live session.** GNOME Software announces
  "updates ready" minutes after a live boot; the session lives in memory.
  Disable automatic update checks for the live user only (synos-live-settings).
- **Regenerate the application catalog** whenever a suite changes
  (`make catalog`), and consider Intel's GPU repository for Debian.

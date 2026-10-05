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

**Deliberately deferred, 27 September 2026.** The two acceptance-suite items
below (Secure Boot unreachable through the harness's GRUB typing, and that
typing corrupting under load) are not oversights. The test engine already
proves, unattended and per bundle, the chain that matters most: the page
generates a bundle, its own launcher builds it, the image boots in QEMU, and
the boot screen is read by OCR to certify the customer's own distribution
reaching its own installer. The install itself was verified by hand on Debian
13 the same day. Making the harness type reliably — or better, implementing
`installer.unattended`, which the schema, `docs/BUNDLE.md` and the Studio's
Advanced tab all offer and nothing in the engine reads — is an enhancement to
take after the distro is otherwise steady, not a gate on it. Whoever picks it
up: prefer the unattended-install route, because it removes the typing
entirely, and it ships a feature customers are already being shown.


- **Screenshots for the Studio gallery.** `screenshots/` in the Studio repo;
  the page embeds what it finds.
- **Run the acceptance suite in QEMU** (`make test`). Never executed in this
  project; the single-entry boot menu, the themed GRUB path on amd64 and the
  region override arguments are new code that this suite is meant to prove.
- **Boot an AI workstation image on real GPU hardware**: NVIDIA first, then
  AMD; check Ollama answers on 11434 and the CDI generation at boot.
- **No network in the live session on real hardware (reported against a
  built yocto-builder image; the kernel, its modules, Wi-Fi/Ethernet
  firmware and network-manager were all confirmed present in that build's
  package lock).** Booting that exact ISO in QEMU with a virtio NIC found
  the live session's own configuration correct and working: NetworkManager
  is enabled and active, netplan's `01-network-manager-all.yaml`
  (`mods/83-network-manager-patch`) hands it the device with no
  `unmanaged-devices` side effect, nothing competes with it
  (`systemd-networkd` present from systemd's own stock preset but
  confirmed inactive; no `ifupdown`/`connman`/`/etc/network/interfaces`
  shipped), ufw runs with `default-deny-inbound` (`base_hardening`) but its
  stock, unmodified `before.rules` still passes DHCP client traffic, and
  GNOME Shell has its own NetworkManager integration
  (`gir1.2-nm-1.0`, via `synos-installer-beta`) independent of the absent
  `network-manager-gnome` tray applet. The device reached `connected`,
  got a real DHCP lease, and systemd reached
  `network-online.target` — all proven by `tools/smoke_test.py`'s new
  `check_network` (see `docs/BUILD_MATRIX.md`), which now runs on every
  smoke-tested image specifically so a future regression here cannot ship
  unnoticed again. What this does **not** and cannot prove: a virtio NIC
  needs no firmware blob, no vendor driver and no probe delay, and never
  fails to appear the way a real Wi-Fi or Ethernet adapter can — the
  reported failure is therefore still open as a real-hardware question
  (driver binding, firmware loading order, USB enumeration timing on a
  live-boot medium, or a WPA network that a live session cannot join
  without someone selecting it in GNOME's own network menu). Needs an
  actual boot on the reporter's hardware with `journalctl -b` and
  `nmcli device status` compared against this finding before assuming a
  driver-level fix is required at all.
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
- **Verify what QMP actually typed into GRUB, not just that GRUB's screen
  went stable afterward.** The acceptance suite's first real run found its
  own keystroke injection (`QmpClient.type_text`, used to edit the boot
  command line under a themed GRUB menu) corrupting characters under host
  load -- caught only because the corrupted command then failed to boot
  and a screenshot happened to be captured. `_wait_for_stable_prompt`
  proves the framebuffer stopped changing, not that the typed text matches
  what was sent; a command that GRUB accepts syntactically but that types
  wrong (a flipped locale code, a dropped argument) would currently pass
  silently. Read back the command line (GRUB's `echo` or a screenshot OCR
  of the prompt) and retry the specific keystrokes that do not match
  before treating a submission as done, rather than only pacing keystrokes
  more slowly and hoping.

  A cheaper mitigation, worth trying before or alongside read-back: the
  themed-menu path retypes the *entire* ~250-character kernel command line
  from scratch through the raw GRUB command line, because "the menu cannot
  be read from screenshots" so the existing entry can't be visually
  selected. But the *default* entry needs no selection -- GRUB's own
  `set default="0"` already highlights it on boot. The untimed (non-themed)
  path already does the cheaper thing for exactly this reason: open the
  existing, already-correct entry's editor (`e`), move down to its `linux`
  line, and type only the short suffix (`debug_kernel_arguments`, ~70
  characters) that needs appending. Every character not typed is a
  character that cannot be corrupted. Only entries other than the default
  (Safe Graphics, To Go, Integrity Check, or a non-default region) still
  need the full raw-command-line reconstruction, since those cannot be
  reached by selection alone on a themed menu.

- **uefi-sb-* cannot boot at all through this suite's current GRUB
  automation.** Confirmed on a real run: shim/GRUB's Secure Boot lockdown
  rejects the `linux`/`initrd` commands the themed command-line path types
  from scratch, with GRUB's own real "error: prohibited by secure boot
  policy" -- by design, lockdown disables loading an arbitrary unverified
  kernel from the command line, precisely to prevent what this automation
  does. Every uefi-sb-offline-btrfs and uefi-sb-online-btrfs attempt in
  the first real run failed this way; no uefi-sb-* scenario has ever
  reached Linux. The same "edit the existing signed entry instead of
  retyping it" redesign above is very likely also the fix here: appending
  arguments to an already-verified menu entry via `e` is the standard,
  widely-used way real users pass one-off kernel arguments under Secure
  Boot, and normal lockdown policy permits it while blocking the raw
  command line. Needs a decision and real engineering, not a quick patch:
  building and validating GRUB automation that only ever edits the
  existing entry (default and non-default) rather than reconstructing one,
  themed menu or not.

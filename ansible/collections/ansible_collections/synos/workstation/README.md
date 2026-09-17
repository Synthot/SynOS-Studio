# synos.workstation

One collection, two moments:

- **Build time**: `playbooks/build.yml` renders the manifest and brand kit,
  runs the image build in a container and collects the ISO, signature, SBOM
  and lock. `playbooks/customize_chroot.yml` is executed by `build.sh` inside
  the chroot (connection `community.general.chroot`) for the playbooks a
  profile lists under `ansible.playbooks`.
- **Run time**: the same roles apply to installed machines over SSH, and
  `synos-first-boot.service` runs `ansible-pull` with the roles a profile
  lists under `ansible.first_boot_roles`.

| Role | Purpose | Works in chroot |
|---|---|---|
| desktop_branding | install a brand kit package on an existing machine | yes |
| base_hardening | firewall, auditd, unattended upgrades, USBGuard, OpenSCAP baseline | yes (config only) |
| directory_join | sssd/realmd for Active Directory, FreeIPA, LDAP; Entra ID via himmelblau | run time only |
| workplace_apps | packages, Flatpak remotes and apps, default applications | yes |
| desktop_policy | dconf defaults and locks, browser policies | yes |
| fleet_enrollment | first-boot ansible-pull unit, agents, inventory registration | yes |

Install: `ansible-galaxy collection install ./ansible/collections/ansible_collections/synos/workstation`

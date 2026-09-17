# VM images from the ISO

```bash
packer init packer/
packer build -var iso=dist/SynOS-1.0.0-amd64.iso -var answers=answers/vdi.yml packer/
```

Produces `dist/vm/<name>/<name>.qcow2`, `.vmdk` and `.vhd` with SHA-256
files. The answer file under `answers/` is the same format the installer
uses for PXE and USB zero-touch installs. Deploy the images with Terraform
or OpenTofu (Proxmox, vSphere, OpenStack, Azure providers).

Requires Packer 1.9+, the QEMU plugin, `qemu-system-x86_64` (or aarch64)
with KVM, and `qemu-img`.

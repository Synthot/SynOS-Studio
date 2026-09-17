# Build VM images (qcow2, and through post-processing OVA/VHD) from a SynOS
# ISO, using the native installer's unattended answer file.
#
#   packer init packer/
#   packer build -var iso=dist/SynOS-1.0.0-amd64.iso -var answers=answers/vdi.yml packer/
#
# Terraform or OpenTofu then deploy the resulting image to VDI, Proxmox,
# OpenStack or a cloud. The answer file is the same one the installer uses
# for PXE and USB zero-touch installs, so a VM and a laptop are configured
# identically.

packer {
  required_plugins {
    qemu = {
      source  = "github.com/hashicorp/qemu"
      version = ">= 1.1.0"
    }
  }
}

variable "iso" { type = string }
variable "iso_checksum" {
  type    = string
  default = "none"   # pass file:dist/<iso>.sha256 in CI
}
variable "answers" {
  type        = string
  description = "Unattended answer file for the native installer"
  default     = "answers/vdi.yml"
}
variable "arch" {
  type    = string
  default = "amd64"
}
variable "output" {
  type    = string
  default = "dist/vm"
}
variable "disk_size" {
  type    = string
  default = "40G"
}
variable "memory" {
  type    = number
  default = 4096
}

locals {
  name       = replace(basename(var.iso), ".iso", "")
  qemu       = var.arch == "arm64" ? "qemu-system-aarch64" : "qemu-system-x86_64"
  machine    = var.arch == "arm64" ? "virt" : "q35"
  # The ISO GRUB entry boots the Live system; the answer file is served over
  # HTTP and picked up by the installer through the kernel command line.
  boot_cmd = [
    "<wait5>e<wait>",
    "<down><down><down><end>",
    " synos.autoinstall=http://{{ .HTTPIP }}:{{ .HTTPPort }}/${basename(var.answers)}",
    "<f10>",
  ]
}

source "qemu" "synos" {
  iso_url          = var.iso
  iso_checksum     = var.iso_checksum
  output_directory = "${var.output}/${local.name}"
  vm_name          = "${local.name}.qcow2"
  format           = "qcow2"
  disk_size        = var.disk_size
  memory           = var.memory
  cpus             = 4
  accelerator      = "kvm"
  qemu_binary      = local.qemu
  machine_type     = local.machine
  efi_boot         = true
  headless         = true
  http_directory   = dirname(var.answers)
  boot_wait        = "5s"
  boot_command     = local.boot_cmd
  # The answer file creates this account; Packer only needs it to know the
  # install finished and to shut the machine down.
  ssh_username     = "packer"
  ssh_password     = "packer-install-only"
  ssh_timeout      = "60m"
  shutdown_command = "sudo systemctl poweroff"
}

build {
  sources = ["source.qemu.synos"]

  provisioner "shell" {
    inline = [
      "sudo deluser --remove-home packer || true",       # the install-only account never ships
      "sudo rm -f /etc/sudoers.d/packer",
      "sudo truncate -s 0 /etc/machine-id",              # each clone gets its own identity
      "sudo rm -f /etc/ssh/ssh_host_*",                  # regenerated on first boot
      "sudo fstrim -av || true",
    ]
  }

  post-processor "checksum" {
    checksum_types = ["sha256"]
    output         = "${var.output}/${local.name}/{{.BuildName}}.{{.ChecksumType}}"
  }

  # Convert with qemu-img for other hypervisors:
  #   qemu-img convert -O vmdk  <name>.qcow2 <name>.vmdk   (VMware, then ovftool for OVA)
  #   qemu-img convert -O vpc   <name>.qcow2 <name>.vhd    (Hyper-V, Azure)
  #   qemu-img convert -O raw   <name>.qcow2 <name>.img    (OpenStack, bare metal)
  post-processor "shell-local" {
    inline = [
      "qemu-img convert -O vmdk ${var.output}/${local.name}/${local.name}.qcow2 ${var.output}/${local.name}/${local.name}.vmdk",
      "qemu-img convert -O vpc  ${var.output}/${local.name}/${local.name}.qcow2 ${var.output}/${local.name}/${local.name}.vhd",
    ]
  }
}

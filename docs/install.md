# Installing on a PC or a VM

Requirements: an x86-64 Intel/AMD machine with UEFI or legacy BIOS firmware, at least 4 GiB of RAM for the administration portal and a small test model (8 GiB recommended, on top of the RAM the models themselves need), and a target disk of 16 GiB or more. A GPU is optional: any NVIDIA, AMD or Intel GPU with Vulkan support is used automatically, including one passed through to a VM (see [GPU acceleration](gpu.md)). Secure Boot is not configured: disable it in the firmware or sign the bootloader with your own PKI. ARM and Raspberry Pi are not supported.

## From the installer ISO (recommended)

Download the `aios-installer-x86_64.iso.part-*` files and `aios-installer-x86_64.iso.sha256` from the [releases page](https://github.com/danieleghione/aios/releases), join and check them, then write the ISO to a USB stick or attach it as a virtual CD/DVD:

```bash
cat aios-installer-x86_64.iso.part-0* > aios-installer-x86_64.iso
sha256sum -c aios-installer-x86_64.iso.sha256
```

The ISO boots on both UEFI and legacy BIOS. The guided installer starts on its own: it lists only the disks it can use (the boot medium and mounted disks are never offered), asks for language, keyboard, hostname, network, time zone, clock and optional recovery SSH, shows a summary and — once you confirm it — erases the chosen disk without asking again. It creates a BIOS boot partition, an ESP, a root and a data partition, unpacks the system, installs both `EFI/BOOT/BOOTX64.EFI` and GRUB for legacy BIOS, locks the boot entries and clears every generated identity and credential. The machine reboots by itself after ten seconds; remove the installation medium first.

[Proxmox instructions](proxmox.md) cover the virtual machine settings in detail.

## From the raw image

`aios-x86_64.img` is the same appliance as a raw disk image. Write it with Raspberry Pi Imager (**Use custom**), balenaEtcher or `dd`, choosing the target device explicitly — writing overwrites it. The stick must be at least as large as the 13 GiB image.

For a virtual machine, convert it instead:

```bash
qemu-img convert -f raw -O qcow2 aios-x86_64.img aios.qcow2
qemu-img resize aios.qcow2 64G
```

Use OVMF/UEFI or legacy BIOS firmware, a virtio-blk, VirtIO SCSI or SATA controller and a virtio/E1000 network card. The ESP uses the UEFI removable path, so it does not depend on vendor NVRAM variables. The data partition grows to fill the appliance disk on every boot; root stays at 10 GiB. `qemu-img convert -O vmdk` and `-O vhdx` produce images for other hypervisors.

Do not clone an appliance that has already been initialised if you want distinct identities and secrets.

## First boot

Ethernet starts with DHCP. The first boot generates TLS material and random credentials. The console shows the address, the URLs and an administrative bootstrap secret valid for 24 hours.

Open `https://IP/admin/`, accept the self-signed certificate only after checking the fingerprint on the appliance console, and choose your own username and password. The bootstrap secret is consumed when that account is created: it is not a permanent password. Administrators created afterwards must change their initial password at first sign-in.

Open WebUI has no separate account: you sign in with the AIOS account created in the portal. Opening `https://IP/` without a valid session redirects to `https://IP/admin/`. Chat users are therefore created under **Users and roles**; the SUPERADMIN role becomes an administrator in Open WebUI too, every other role is a normal user. Self sign-up stays disabled.

The physical console offers a closed menu — network status, bootstrap code, system updates, reboot, power off and the API key — and asks for an AIOS administrator password before each action. It is not a remote login service. SSH is installed but inert unless you ask for it during installation, and the account it creates can only read or regenerate the first administrator's code and show the API key: see [security](security.md). The firewall accepts HTTP/HTTPS, DHCP, essential ICMP and, when SSH was enabled, port 22.

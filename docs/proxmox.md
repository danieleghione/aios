# Installing AIOS on Proxmox VE from a virtual CD/DVD

Use **`aios-installer-x86_64.iso`** in the Proxmox ISO storage. The raw `aios-x86_64.img` is a disk image: attaching it as a CD/DVD does not work. It stays available for USB sticks or as an imported VM disk.

> **The ISO works with stock Proxmox settings.** Its optical boot catalogue holds two El Torito entries, BIOS and UEFI, so the medium starts under both SeaBIOS and OVMF. The installer writes both bootloaders to the target disk whatever firmware it was started with, so the installed system boots either way. The default profile — SeaBIOS, i440fx, 1 core, 2 GiB — is covered by the automated tests, with a full installation and reboot.

1. On the Proxmox node pick a storage that accepts **ISO Images** (for example `local`), then **Upload** and upload `aios-installer-x86_64.iso`.
2. Create a Linux VM and select the ISO as the CD/DVD.
3. The defaults on the System tab are fine. To use UEFI instead of the default BIOS, choose **q35** and **OVMF (UEFI)**, then add an EFI Disk with **Pre-Enroll keys disabled**: this appliance does not sign its own bootloader and Secure Boot would refuse it. On an existing EFI Disk, check that Secure Boot is disabled in the OVMF firmware menu.
4. Use the **VirtIO SCSI single** controller (the default) and an empty SCSI disk of at least **16 GiB**; the default 32 GiB is fine. Installation and first boot work with the default minimum of 1 core and 2 GiB, but real use needs more: the CPU runs the inference and the model has to fit in memory. Set CPU type **host** so the guest sees the real vector instructions. To use a GPU, pass it through to the VM: see [GPU passthrough](#gpu-passthrough).
5. Add a VirtIO network card on the bridge you want, usually `vmbr0`. Static addresses, gateway and DNS must belong to your own network.
6. Under Options → Boot Order enable the CD/DVD and put it before the disk. Start the VM and open the noVNC console. The boot menu shows the AIOS logo; the first entry, **Install AIOS**, starts by itself after 5 seconds, the second one is for a serial console. The AIOS splash screen is shown while the system loads.
7. The guided installer starts on its own; there is no menu to find. It lists only the disks it can use (whole, unmounted, at least 16 GiB, and not the installation medium) and proposes the first one: press Enter or type another from the list. Press Ctrl-C to stop and open the menu (install, reboot, power off).
8. Choose the system locale and keyboard, hostname, interface, DHCP or a static IPv4 address with CIDR prefix, gateway and DNS, then the time zone and either NTP servers or a manual date and time in that zone. Every value is checked as you type it: if it is not valid the installer explains why and asks for **that value only**, without starting over. Check the summary; answering no lets you enter the values again.
9. You are asked whether to enable **recovery SSH**. It only reads or regenerates the first administrator's code and shows the API key, and the default is **yes**. Give a public key or a password of at least 12 characters, typed twice; answer no to leave SSH switched off. Details and limits in [security](security.md).
10. Once you confirm the summary the installation starts immediately on the chosen disk, with no further confirmation: the disk is erased. Progress is shown in five steps; it usually takes a few minutes. The machine reboots by itself after 10 seconds.
11. Detach the ISO from the CD/DVD drive (or put the disk first in the boot order) before that reboot, otherwise the installer starts again. The console then shows the URLs and the one-time bootstrap code; open `https://IP/admin/`.

The language you choose sets the Linux locale; the AIOS interface itself is in English. The keyboard applies to the installed system, while the live installer keeps the initial layout. The interface you select must still exist in the installed VM.

## GPU passthrough

AIOS uses a GPU passed through to the VM exactly as it would on bare metal, with nothing to configure inside the appliance.

1. Enable IOMMU on the Proxmox host (`intel_iommu=on` or `amd_iommu=on`, the default on recent kernels) and bind the card to `vfio-pci`, as described in the [PCI passthrough guide](https://pve.proxmox.com/wiki/PCI_Passthrough).
2. Give the VM machine type **q35** and, for most discrete cards, **OVMF (UEFI)**.
3. Hardware → Add → PCI Device: choose the GPU, tick **All Functions** and **PCI-Express**. Leave **Primary GPU** off: the console stays on the virtual display.
4. Give the VM enough RAM for the part of the model that does not fit in video memory. Ballooning does not work with passthrough; set a fixed amount.

Boot the VM: the **Hardware** page lists the GPU under *Accelerators* and the **Runtime** page shows how many layers run on it. An NVIDIA card installs its kernel modules on the first boot that sees it, which adds a few seconds once. Integrated Intel graphics can be passed through the same way (GVT-g and SR-IOV virtual functions also work when the host offers them); a virtual display adapter such as VirtIO-GPU is not a real GPU and is ignored.

## If it does not boot

**The firmware finds the medium but refuses to boot it** (`Access Denied`, a Secure Boot violation): the EFI Disk was created with **Pre-Enroll keys** enabled. AIOS does not sign its bootloader: recreate the EFI Disk without pre-enrol, or disable Secure Boot in the OVMF firmware menu. SeaBIOS does not have this problem.

**`No bootable device` or an iPXE network boot**: check that the file in the CD/DVD drive ends in `.iso` and that the CD/DVD comes before the disk in the boot order. The raw `.img` has no El Torito optical boot structure and does not work as a CD/DVD; the `.iso` carries BIOS and UEFI optical boot and a live environment with the installer.

If the installer menu appears again after the reboot, remove the ISO or change the boot order. If no address appears, check the bridge, the VLAN, DHCP, or the address/CIDR, gateway and DNS you chose.

## Build and test

```bash
make iso
make test-iso
```

`make iso` builds the raw image first, then a compressed live filesystem and the ISO. It needs `squashfs-tools` on the host and downloads the live-boot packages during the build. It recreates the generated `build/iso-rootfs` directory and refuses to delete it while mounts are still active. The test uses QEMU/KVM with a CD/DVD, UEFI, q35 and VirtIO-SCSI and an empty temporary disk; it never touches a Proxmox server or a physical disk.

References: [Proxmox VE manual](https://pve.proxmox.com/pve-docs/pve-admin-guide.pdf), [GRUB: making a bootable CD-ROM](https://www.gnu.org/software/grub/manual/grub/html_node/Making-a-GRUB-bootable-CD_002dROM.html), [live-boot](https://manpages.debian.org/unstable/live-boot-doc/live-boot.7.en.html).

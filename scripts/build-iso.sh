#!/usr/bin/env bash
# Build an optical UEFI installer from the release raw disk. Does not mutate it.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
# archive.ubuntu.com publishes AAAA records this build host has no route to, so
# apt stalls on half its candidates until they time out. Keep fetches on IPv4.
APT=(-o Acquire::ForceIPv4=true -o Acquire::Retries=5 -o Acquire::http::Timeout=15 -o Acquire::https::Timeout=15)
[[ $EUID == 0 ]] || exec sudo "$0" "$@"
[[ -f dist/aios-x86_64.img ]] || { echo 'Run make image first'; exit 1; }
ROOT="$PWD/build/iso-rootfs"
TREE="$ROOT/tmp/iso-tree"
if [[ -e "$ROOT" ]]; then
  python3 - "$ROOT" <<'CHECK'
import pathlib, subprocess, sys
root=pathlib.Path(sys.argv[1])
assert root == pathlib.Path.cwd()/'build/iso-rootfs'
mounts=subprocess.check_output(['findmnt','-rn','-o','TARGET'],text=True).splitlines()
assert not any(m==str(root) or m.startswith(str(root)+'/') for m in mounts), 'Unmount ISO build cache first'
CHECK
  rm -rf -- "$ROOT"
fi
mkdir -p "$ROOT"
LOOP=$(losetup --find --show --read-only --partscan dist/aios-x86_64.img)
MOUNT=$(mktemp -d /mnt/aios-iso.XXXXXX)
# Editors and indexers watching build/ keep descriptors open inside the chroot,
# which makes a plain umount fail; detach lazily rather than abort the build.
release() {
  local target=$1
  mountpoint -q "$target" || return 0
  umount "$target" 2>/dev/null && return 0
  sleep 2
  umount "$target" 2>/dev/null || umount -l "$target"
}
cleanup() {
  for part in sys proc dev; do release "$ROOT/$part" || true; done
  umount "$MOUNT" 2>/dev/null || true
  losetup -d "$LOOP" 2>/dev/null || true
  rmdir "$MOUNT" 2>/dev/null || true
}
trap cleanup EXIT
mount -o ro,noload "${LOOP}p2" "$MOUNT"
rsync -aHAXx --numeric-ids "$MOUNT/" "$ROOT/"
umount "$MOUNT"
mount --bind /dev "$ROOT/dev"
mount -t proc proc "$ROOT/proc"
mount -t sysfs sysfs "$ROOT/sys"
rm -f "$ROOT/etc/resolv.conf"
cp /etc/resolv.conf "$ROOT/etc/resolv.conf"
printf '#!/bin/sh\nexit 101\n' > "$ROOT/usr/sbin/policy-rc.d"
chmod +x "$ROOT/usr/sbin/policy-rc.d"
source "$PWD/scripts/apt-mirror-pin.sh"
pin_mirror "$ROOT" archive.ubuntu.com
pin_mirror "$ROOT" security.ubuntu.com
chroot "$ROOT" apt-get "${APT[@]}" update
chroot "$ROOT" env DEBIAN_FRONTEND=noninteractive apt-get "${APT[@]}" install -y --no-install-recommends live-boot live-boot-initramfs-tools xorriso mtools grub-pc-bin locales console-setup keyboard-configuration squashfs-tools
rm "$ROOT/usr/sbin/policy-rc.d"
unpin_mirror "$ROOT" archive.ubuntu.com
unpin_mirror "$ROOT" security.ubuntu.com
# Embed current installer sources even if the raw was built previously.
rsync -a installer/ "$ROOT/opt/aios/app/installer/"
printf '# Optical live environment; installer creates target fstab.\n' > "$ROOT/etc/fstab"
cat > "$ROOT/etc/systemd/system/aios-installer.target" <<'UNIT'
[Unit]
Description=AIOS optical installation environment
Requires=basic.target
After=basic.target
# logind is what turns the ACPI power button into a clean shutdown; without it a
# hypervisor Shutdown request (Proxmox, virsh) is silently ignored in the live ISO.
Wants=aios-live-console.service aios-live-serial.service systemd-udev-settle.service systemd-logind.service
AllowIsolate=yes
UNIT
for console in console serial; do
  TTY=tty1; CONDITION='!aios.serial'
  [[ $console != serial ]] || { TTY=ttyS0; CONDITION='aios.serial'; }
  cat > "$ROOT/etc/systemd/system/aios-live-$console.service" <<UNIT
[Unit]
Description=AIOS ISO installer $TTY
After=systemd-udev-settle.service
ConditionKernelCommandLine=boot=live
ConditionKernelCommandLine=$CONDITION
Conflicts=getty@$TTY.service serial-getty@$TTY.service
[Service]
# Close the boot splash when the installer is ready to be drawn.
ExecStartPre=-/usr/bin/plymouth quit
ExecStart=/opt/aios/app/installer/live-console.sh
StandardInput=tty
StandardOutput=tty
StandardError=tty
TTYPath=/dev/$TTY
TTYReset=yes
Restart=always
RestartSec=3
UNIT
done
KVER=$(basename "$(ls "$ROOT"/boot/vmlinuz-* | sort -V | tail -1)"); KVER=${KVER#vmlinuz-}
chroot "$ROOT" update-initramfs -u -k "$KVER"
chroot "$ROOT" apt-get clean
rm -f "$ROOT/etc/resolv.conf"
ln -s /run/systemd/resolve/stub-resolv.conf "$ROOT/etc/resolv.conf"
# grub-pc-bin above makes grub-mkrescue emit a BIOS El Torito entry and hybrid
# MBR next to the UEFI one, so the ISO boots on SeaBIOS as well as OVMF.
mkdir -p "$TREE/live" "$TREE/boot/grub"
cp "$ROOT/boot/vmlinuz-$KVER" "$TREE/live/vmlinuz"
cp "$ROOT/boot/initrd.img-$KVER" "$TREE/live/initrd.img"
cp branding/grub-background.png "$TREE/boot/grub/aios.png"
cp branding/grub-theme.txt "$TREE/boot/grub/theme.txt"
cat > "$TREE/boot/grub/grub.cfg" <<'GRUB'
set timeout=5
set default=0
serial --unit=0 --speed=115200
terminal_input console serial
terminal_output console serial
# Branded menu where the firmware offers graphics; plain text otherwise.
insmod all_video
insmod gfxterm
insmod png
if loadfont unicode; then
  set gfxmode=auto
  terminal_output gfxterm serial
  set theme=/boot/grub/theme.txt
  set color_normal=light-gray/black
  set menu_color_normal=light-gray/black
  set menu_color_highlight=cyan/black
fi
menuentry 'Install AIOS' {
 linux /live/vmlinuz boot=live components systemd.unit=aios-installer.target quiet splash plymouth.ignore-serial-consoles loglevel=3 vt.global_cursor_default=0 systemd.show_status=false console=tty0
 initrd /live/initrd.img
}
menuentry 'Install AIOS - serial console' {
 linux /live/vmlinuz boot=live components systemd.unit=aios-installer.target aios.serial quiet loglevel=3 systemd.show_status=false console=tty0 console=ttyS0,115200n8
 initrd /live/initrd.img
}
GRUB
for part in sys proc dev; do release "$ROOT/$part"; done
mksquashfs "$ROOT" "$TREE/live/filesystem.squashfs" -noappend -comp zstd -Xcompression-level 3 -processors 2 -wildcards -e 'dev/*' 'proc/*' 'sys/*' 'run/*' 'tmp/*'
mount --bind /dev "$ROOT/dev"
chroot "$ROOT" grub-mkrescue -o /tmp/aios-installer.iso /tmp/iso-tree
mv "$ROOT/tmp/aios-installer.iso" dist/aios-installer-x86_64.iso
# Bare file name, so the checksum verifies wherever the ISO is downloaded to.
(cd dist && sha256sum aios-installer-x86_64.iso > aios-installer-x86_64.iso.sha256)
python3 - <<'PY'
import json, pathlib, subprocess
root=pathlib.Path.cwd(); iso=root/'dist/aios-installer-x86_64.iso'
info={'format':'ISO9660 optical UEFI installer','size_bytes':iso.stat().st_size,'sha256':(root/'dist/aios-installer-x86_64.iso.sha256').read_text().split()[0],'raw_base':json.loads((root/'dist/aios-x86_64-build-info.json').read_text()),'source_commit':subprocess.check_output(['git','-c','safe.directory='+str(root),'rev-parse','HEAD'],text=True).strip()}
(root/'dist/aios-installer-build-info.json').write_text(json.dumps(info,indent=2)+'\n')
PY
if [[ -n ${SUDO_UID:-} ]]; then chown "$SUDO_UID:$SUDO_GID" dist/aios-installer*; fi
echo 'Built dist/aios-installer-x86_64.iso'

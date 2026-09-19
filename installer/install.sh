#!/usr/bin/env bash
set -Eeuo pipefail
[[ $EUID == 0 && $(uname -m) == x86_64 ]] || { echo 'Requires root and x86-64.'; exit 1; }
MIN_BYTES=17179869184
# Disks holding the running system or the installation medium are never offered.
busy_disks() {
  local source
  for source in $(findmnt -rn -o SOURCE / /run/live/medium 2>/dev/null); do
    [[ -b "$source" ]] && lsblk -s -n -o PATH,TYPE "$source" | awk '$2=="disk" || $2=="rom" {print $1}'
  done
  return 0
}
eligible_disks() {
  local busy path type size ro
  busy=$(busy_disks | sort -u)
  while read -r path type size ro; do
    [[ $type == disk && $ro == 0 && $size -ge $MIN_BYTES ]] || continue
    grep -qxF -- "$path" <<<"$busy" && continue
    lsblk -nr -o MOUNTPOINTS "$path" | grep -q '[^[:space:]]' && continue
    echo "$path"
  done < <(lsblk -dbn -o PATH,TYPE,SIZE,RO)
}
echo 'AIOS installer — the target disk you choose will be erased completely.'
DISKS=$(eligible_disks)
if [[ -z $DISKS ]]; then
  echo 'No usable disk: a whole, unmounted disk of at least 16 GiB is required.'
  lsblk -d -o PATH,SIZE,TYPE,MODEL,TRAN
  exit 1
fi
echo 'Usable disks:'
# shellcheck disable=SC2086
lsblk -d -o PATH,SIZE,MODEL,TRAN $DISKS
DEFAULT=$(head -n1 <<<"$DISKS")
while true; do
  read -r -p "Target disk [$DEFAULT]: " TARGET
  TARGET=${TARGET:-$DEFAULT}
  grep -qxF -- "$TARGET" <<<"$DISKS" && break
  echo "  Invalid value: '$TARGET' is not one of the usable disks ($(paste -sd' ' <<<"$DISKS")). Please enter it again."
done
SETTINGS=$(mktemp /run/aios-install-settings.XXXXXX)
trap 'rm -f "$SETTINGS"' EXIT
/opt/aios/venv/bin/python /opt/aios/app/installer/configure.py --collect "$SETTINGS"
START=$SECONDS
CURRENT='start-up'
step() { CURRENT=$2; printf '\n[%s/5] %s (%d s)\n' "$1" "$2" $((SECONDS - START)); }
trap 'echo; echo "ERROR during: $CURRENT (line $LINENO). Installation not completed; disk $TARGET is not bootable."' ERR
echo
echo "Installing on $TARGET: every piece of data on that disk is erased."
step 1 'Partitioning and formatting'
wipefs -qa "$TARGET" || true
sgdisk --zap-all "$TARGET" >/dev/null
# Partition 4 fills the 1 MiB gap before partition 1 and carries GRUB's core.img
# for legacy BIOS, leaving the data partition last so it can still grow on boot.
sgdisk -n 1:2048:+256M -t 1:ef00 -c 1:AIOS-EFI -n 2:0:+10G -t 2:8300 -c 2:AIOS-ROOT -n 3:0:0 -t 3:8300 -c 3:AIOS-DATA -n 4:34:2047 -t 4:ef02 -c 4:AIOS-BIOS "$TARGET" >/dev/null
partprobe "$TARGET"
udevadm settle
part() { lsblk -nr -o PATH,PARTN "$TARGET" | awk -v n="$1" '$2==n {print $1}'; }
EFI=$(part 1); ROOT=$(part 2); DATA=$(part 3)
mkfs.vfat -F32 "$EFI" >/dev/null
# A freshly partitioned disk has nothing to discard, and discarding a large
# thin-provisioned volume is what made formatting slow on hypervisor storage.
mkfs.ext4 -q -F -E nodiscard "$ROOT"
mkfs.ext4 -q -F -E nodiscard "$DATA"
DEST=$(mktemp -d /mnt/aios-install.XXXXXX)
cleanup() { umount -R "$DEST" 2>/dev/null || true; rmdir "$DEST" 2>/dev/null || true; rm -f "$SETTINGS"; }
trap cleanup EXIT
mount -o noatime "$ROOT" "$DEST"
mkdir -p "$DEST/boot/efi" "$DEST/var/lib/aios"
mount "$EFI" "$DEST/boot/efi"
mount -o noatime "$DATA" "$DEST/var/lib/aios"
step 2 'Copying the system'
SQUASHFS=/run/live/medium/live/filesystem.squashfs
if [[ -f $SQUASHFS ]] && command -v unsquashfs >/dev/null; then
  # Unpack the system image straight from the installation medium: it is read
  # front to back, which optical drives and virtual CD devices serve far faster
  # than the random access of copying the live tree file by file.
  unsquashfs -f -d "$DEST" -processors "$(nproc)" "$SQUASHFS"
  rm -rf "$DEST/etc/aios/secrets" "$DEST/etc/aios/webui.env" "$DEST/etc/nginx/aios.key" "$DEST/etc/nginx/aios.crt" "$DEST/root/.bash_history"
  mkdir -p "$DEST/dev" "$DEST/proc" "$DEST/sys" "$DEST/run" "$DEST/tmp" "$DEST/mnt" "$DEST/media"
  chmod 1777 "$DEST/tmp"
else
# Whole-file copies with no per-file delta search: the destination is empty.
rsync -aHAXx --numeric-ids --whole-file --info=progress2 --no-inc-recursive --exclude=/dev/* --exclude=/proc/* --exclude=/sys/* --exclude=/run/* --exclude=/tmp/* --exclude=/mnt/* --exclude=/media/* --exclude=/root/.bash_history --exclude=/var/log/journal/* --exclude=/var/lib/aios/* --exclude=/etc/aios/secrets/* --exclude=/etc/aios/webui.env --exclude=/etc/nginx/aios.key --exclude=/etc/nginx/aios.crt / "$DEST/"
fi
step 3 'System configuration'
cat > "$DEST/etc/fstab" <<FSTAB
UUID=$(blkid -s UUID -o value "$ROOT") / ext4 defaults,noatime 0 1
UUID=$(blkid -s UUID -o value "$EFI") /boot/efi vfat umask=0077 0 2
UUID=$(blkid -s UUID -o value "$DATA") /var/lib/aios ext4 defaults,noatime 0 2
FSTAB
: > "$DEST/etc/machine-id"
rm -f "$DEST/var/lib/dbus/machine-id" "$DEST/var/lib/systemd/random-seed" "$DEST/etc/ssh/ssh_host_"*
mount --bind /dev "$DEST/dev"
mount -t proc proc "$DEST/proc"
mount -t sysfs sysfs "$DEST/sys"
/opt/aios/venv/bin/python /opt/aios/app/installer/configure.py --apply "$DEST" --settings "$SETTINGS" >/dev/null
# Set the live clock/RTC only now: the next boot creates certificates using this
# time; NTP installations sync normally.
/opt/aios/venv/bin/python - "$SETTINGS" <<'PY'
import json, subprocess, sys
settings = json.load(open(sys.argv[1]))
if not settings['ntp']:
    subprocess.run(['date', '--set', settings['manual_utc']], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(['hwclock', '--systohc', '--utc'], check=False)
PY
step 4 'Protected bootloader'
# Nobody at the keyboard may edit the boot entries (init=/bin/bash) or open the
# GRUB shell: a random superuser password that is never stored or shown locks
# both, while the AIOS entry itself still boots without asking.
GRUB_SECRET=$(head -c 48 /dev/urandom | base64 | tr -d '\n')
GRUB_HASH=$(printf '%s\n%s\n' "$GRUB_SECRET" "$GRUB_SECRET" | chroot "$DEST" grub-mkpasswd-pbkdf2 | awk '/grub.pbkdf2/ {print $NF}')
unset GRUB_SECRET
[[ $GRUB_HASH == grub.pbkdf2.* ]]
printf '#!/bin/sh\nexec tail -n +3 $0\nset superusers="aios-locked"\npassword_pbkdf2 aios-locked %s\n' "$GRUB_HASH" > "$DEST/etc/grub.d/01_aios_lock"
chmod 755 "$DEST/etc/grub.d/01_aios_lock"
sed -i 's/^CLASS="--class gnu-linux --class gnu --class os"$/CLASS="--class gnu-linux --class gnu --class os --unrestricted"/' "$DEST/etc/grub.d/10_linux"
# Install both bootloaders regardless of the firmware running the installer, so
# the target boots whether the machine later uses UEFI or legacy BIOS.
chroot "$DEST" grub-install --target=x86_64-efi --efi-directory=/boot/efi --bootloader-id=AIOS --removable --no-nvram >/dev/null 2>&1
chroot "$DEST" grub-install --target=i386-pc --boot-directory=/boot "$TARGET" >/dev/null 2>&1
chroot "$DEST" update-grub >/dev/null 2>&1
grep -q '^password_pbkdf2 aios-locked' "$DEST/boot/grub/grub.cfg"
grep -q "menuentry .*--unrestricted" "$DEST/boot/grub/grub.cfg"
step 5 'Flushing to disk'
sync
printf '\nInstallation completed in %d s. Remove the installation medium.\n' $((SECONDS - START))
echo 'Rebooting automatically in 10 seconds (press Enter to reboot now).'
read -r -t 10 _ || true
systemctl reboot

#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
# archive.ubuntu.com publishes AAAA records this build host has no route to, so
# apt stalls on half its candidates until they time out. Keep fetches on IPv4.
APT=(-o Acquire::ForceIPv4=true -o Acquire::Retries=5 -o Acquire::http::Timeout=15 -o Acquire::https::Timeout=15)
source build/versions.env
ROOT="$PWD/build/rootfs"
if [[ $EUID != 0 ]]; then exec sudo "$0" "$@"; fi
if [[ ! -f "$ROOT/etc/debian_version" ]]; then
  debootstrap --variant=minbase --arch=amd64 "$UBUNTU_SUITE" "$ROOT" https://archive.ubuntu.com/ubuntu
fi
mountpoint -q "$ROOT/dev" || mount --bind /dev "$ROOT/dev"
mountpoint -q "$ROOT/proc" || mount -t proc proc "$ROOT/proc"
mountpoint -q "$ROOT/sys" || mount -t sysfs sysfs "$ROOT/sys"
cleanup() { umount "$ROOT/sys" "$ROOT/proc" "$ROOT/dev"; }
trap cleanup EXIT
rm -f "$ROOT/etc/resolv.conf"
cp /etc/resolv.conf "$ROOT/etc/resolv.conf"
cat > "$ROOT/etc/apt/sources.list" <<APT
deb https://archive.ubuntu.com/ubuntu noble main universe restricted multiverse
deb https://archive.ubuntu.com/ubuntu noble-updates main universe restricted multiverse
deb https://security.ubuntu.com/ubuntu noble-security main universe restricted multiverse
APT
printf '#!/bin/sh\nexit 101\n' > "$ROOT/usr/sbin/policy-rc.d"
chmod +x "$ROOT/usr/sbin/policy-rc.d"
chroot "$ROOT" apt-get "${APT[@]}" update
chroot "$ROOT" env DEBIAN_FRONTEND=noninteractive apt-get "${APT[@]}" install -y --no-install-recommends linux-image-generic-hwe-24.04 linux-firmware initramfs-tools zstd systemd-sysv systemd-timesyncd systemd-resolved dbus udev netplan.io iproute2 iputils-ping ca-certificates openssl nginx nftables python3-venv python3-dev build-essential cmake ninja-build git curl libcurl4-openssl-dev libssl-dev libgomp1 libnuma1 numactl util-linux rsync gdisk parted dosfstools e2fsprogs openssh-server sudo grub-efi-amd64-bin grub-pc-bin grub2-common efibootmgr cloud-guest-utils jq pciutils kmod lshw lm-sensors tzdata locales console-setup keyboard-configuration logrotate libgomp1 libsndfile1 ffmpeg
mkdir -p "$ROOT/opt/aios" "$ROOT/usr/src"
./scripts/build-runtime.sh
if [[ ! -x "$ROOT/opt/aios/webui/bin/python" ]]; then
  chroot "$ROOT" python3 -m venv /opt/aios/webui
  chroot "$ROOT" /opt/aios/webui/bin/pip install --no-cache-dir "torch==$TORCH_VERSION" --index-url https://download.pytorch.org/whl/cpu
fi
python3 scripts/patch-webui-wheel.py > build/patched-wheel-path
PATCHED_WHEEL=$(basename "$(cat build/patched-wheel-path)")
cp "$(cat build/patched-wheel-path)" "$ROOT/tmp/$PATCHED_WHEEL"
chroot "$ROOT" /opt/aios/webui/bin/pip install --no-cache-dir "/tmp/$PATCHED_WHEEL" setuptools==84.0.0 pip==26.2
chroot "$ROOT" /opt/aios/webui/bin/pip check
rm -f "$ROOT/tmp/$PATCHED_WHEEL"
chroot "$ROOT" /opt/aios/webui/bin/pip freeze > build/open-webui.lock
chroot "$ROOT" dpkg-query -W -f='${Package}=${Version}\n' > build/os-packages.lock
rm -f "$ROOT/usr/sbin/policy-rc.d"
touch build/rootfs-ready

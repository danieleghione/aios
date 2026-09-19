#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
# archive.ubuntu.com publishes AAAA records this build host has no route to, so
# apt stalls on half its candidates until they time out. Keep fetches on IPv4.
APT=(-o Acquire::ForceIPv4=true -o Acquire::Retries=5 -o Acquire::http::Timeout=15 -o Acquire::https::Timeout=15)
[[ $(uname -m) == x86_64 ]] || { echo 'x86-64 build host required'; exit 1; }
if [[ $EUID != 0 ]]; then exec sudo "$0" "$@"; fi
source build/versions.env
ROOT="$PWD/build/rootfs"
[[ -f build/rootfs-ready ]] || ./scripts/prepare-rootfs.sh
# llama.cpp with the CPU variants and the Vulkan backend; a no-op when the cache is current.
./scripts/build-runtime.sh
# stable-diffusion.cpp, the second engine: image models, same backends, own prefix.
./scripts/build-imaging.sh
rm -f "$ROOT/etc/resolv.conf"
cp /etc/resolv.conf "$ROOT/etc/resolv.conf"
if [[ ! -x "$ROOT/usr/sbin/locale-gen" || ! -x "$ROOT/bin/setupcon" && ! -x "$ROOT/usr/bin/setupcon" ]]; then
  chroot "$ROOT" apt-get "${APT[@]}" update
  chroot "$ROOT" env DEBIAN_FRONTEND=noninteractive apt-get "${APT[@]}" install -y locales console-setup keyboard-configuration
fi
# Legacy BIOS modules, so the appliance also boots on firmware without UEFI.
if [[ ! -d "$ROOT/usr/lib/grub/i386-pc" ]]; then
  chroot "$ROOT" apt-get "${APT[@]}" update
  chroot "$ROOT" env DEBIAN_FRONTEND=noninteractive apt-get "${APT[@]}" install -y --no-install-recommends grub-pc-bin
fi
# Present but switched off: the installer enables SSH only if the operator asks.
if [[ ! -x "$ROOT/usr/sbin/sshd" || ! -x "$ROOT/usr/bin/sudo" ]]; then
  chroot "$ROOT" apt-get "${APT[@]}" update
  chroot "$ROOT" env DEBIAN_FRONTEND=noninteractive apt-get "${APT[@]}" install -y --no-install-recommends openssh-server sudo
fi
# GPU support for any vendor. Vulkan reaches Intel and AMD GPUs, integrated or
# discrete, through Mesa, and NVIDIA cards through NVIDIA's own driver, whose
# user-space libraries are installed here. Its kernel modules come in two
# flavours for different card generations: both are bundled as packages and
# aios-nvidia-driver installs the matching one at boot, offline.
NVIDIA_BRANCH=580-server
# The hardware enablement kernel: the 24.04 GA kernel (6.8) predates Intel Arc
# Battlemage, Lunar and Panther Lake graphics and AMD Radeon RX 9000, and those
# GPUs would simply not appear. The GA kernel is removed so only one is carried.
if [[ ! -f "$ROOT/var/lib/dpkg/info/linux-image-generic-hwe-24.04.list" ]]; then
  chroot "$ROOT" apt-get "${APT[@]}" update
  chroot "$ROOT" env DEBIAN_FRONTEND=noninteractive apt-get "${APT[@]}" install -y --no-install-recommends linux-image-generic-hwe-24.04
  HWE_KERNEL=$(basename "$(ls "$ROOT"/boot/vmlinuz-* | sort -V | tail -1)"); HWE_KERNEL=${HWE_KERNEL#vmlinuz-}
  mapfile -t OLD_KERNEL < <(chroot "$ROOT" dpkg-query -W -f='${Package}\n' 'linux-generic' 'linux-image-generic' 'linux-headers-generic' 'linux-image-[0-9]*' 'linux-modules-[0-9]*' 'linux-modules-extra-[0-9]*' 'linux-headers-[0-9]*' 2>/dev/null | grep -v -- "$HWE_KERNEL" || true)
  if (( ${#OLD_KERNEL[@]} )); then
    chroot "$ROOT" env DEBIAN_FRONTEND=noninteractive apt-get purge -y "${OLD_KERNEL[@]}"
  fi
  rm -f "$ROOT/boot/vmlinuz.old" "$ROOT/boot/initrd.img.old"
fi
# Radeon HD 7000 to R9 200 cards are driven by the old radeon module by default,
# which has no Vulkan; amdgpu drives them with RADV.
printf 'options radeon si_support=0 cik_support=0\noptions amdgpu si_support=1 cik_support=1\n' > "$ROOT/etc/modprobe.d/aios-amdgpu.conf"
IMAGE_KERNEL=$(basename "$(ls "$ROOT"/boot/vmlinuz-* | sort -V | tail -1)"); IMAGE_KERNEL=${IMAGE_KERNEL#vmlinuz-}
if [[ ! -f "$ROOT/usr/share/vulkan/icd.d/nvidia_icd.json" || ! -x "$ROOT/usr/bin/vulkaninfo" ]]; then
  chroot "$ROOT" apt-get "${APT[@]}" update
  chroot "$ROOT" env DEBIAN_FRONTEND=noninteractive apt-get "${APT[@]}" install -y --no-install-recommends \
    libvulkan1 mesa-vulkan-drivers vulkan-tools \
    nvidia-utils-$NVIDIA_BRANCH libnvidia-gl-$NVIDIA_BRANCH libnvidia-compute-$NVIDIA_BRANCH
fi
if ! compgen -G "$ROOT/opt/aios/drivers/nvidia/closed/linux-objects-nvidia-$NVIDIA_BRANCH-${IMAGE_KERNEL}_*.deb" >/dev/null; then
  rm -rf "$ROOT/opt/aios/drivers/nvidia"
  mkdir -p "$ROOT/opt/aios/drivers/nvidia/open" "$ROOT/opt/aios/drivers/nvidia/closed"
  chroot "$ROOT" apt-get "${APT[@]}" update
  chroot "$ROOT" bash -c "cd /opt/aios/drivers/nvidia/open && apt-get download linux-modules-nvidia-$NVIDIA_BRANCH-open-$IMAGE_KERNEL linux-modules-nvidia-$NVIDIA_BRANCH-open-generic-hwe-24.04"
  chroot "$ROOT" bash -c "cd /opt/aios/drivers/nvidia/closed && apt-get download linux-modules-nvidia-$NVIDIA_BRANCH-$IMAGE_KERNEL linux-objects-nvidia-$NVIDIA_BRANCH-$IMAGE_KERNEL linux-signatures-nvidia-$IMAGE_KERNEL linux-modules-nvidia-$NVIDIA_BRANCH-generic-hwe-24.04"
fi
ls "$ROOT/opt/aios/drivers/nvidia/open/"*.deb "$ROOT/opt/aios/drivers/nvidia/closed/"*.deb >/dev/null
# Boot splash. Themed before the initramfs is generated, which is what carries it.
if [[ ! -x "$ROOT/usr/bin/plymouth" ]]; then
  chroot "$ROOT" apt-get "${APT[@]}" update
  chroot "$ROOT" env DEBIAN_FRONTEND=noninteractive apt-get "${APT[@]}" install -y --no-install-recommends plymouth
fi
mkdir -p "$ROOT/usr/share/plymouth/themes/aios" "$ROOT/etc/plymouth"
cp branding/plymouth/aios.plymouth branding/plymouth/aios.script branding/dot.png "$ROOT/usr/share/plymouth/themes/aios/"
cp branding/splash-logo.png "$ROOT/usr/share/plymouth/themes/aios/logo.png"
printf '[Daemon]\nTheme=aios\nShowDelay=0\nDeviceTimeout=8\n' > "$ROOT/etc/plymouth/plymouthd.conf"
# Without it the initramfs starts plymouth before any display driver is loaded,
# plymouth finds no screen and falls back to printing boot messages as text.
mkdir -p "$ROOT/etc/initramfs-tools/conf.d"
printf 'FRAMEBUFFER=y\n' > "$ROOT/etc/initramfs-tools/conf.d/splash"
chroot "$ROOT" update-alternatives --install /usr/share/plymouth/themes/default.plymouth default.plymouth /usr/share/plymouth/themes/aios/aios.plymouth 200
chroot "$ROOT" update-alternatives --set default.plymouth /usr/share/plymouth/themes/aios/aios.plymouth
# The splash would otherwise close as soon as basic services are up and leave a
# blank screen while AIOS starts; the console menu closes it instead.
chroot "$ROOT" systemctl mask plymouth-quit.service
# Kernel packages without recommends do not guarantee an initramfs exists.
KVER=$(basename "$(ls "$ROOT"/boot/vmlinuz-* | sort -V | tail -1)")
KVER=${KVER#vmlinuz-}
if [[ -f "$ROOT/boot/initrd.img-$KVER" ]]; then
  chroot "$ROOT" update-initramfs -u -k "$KVER"
else
  chroot "$ROOT" update-initramfs -c -k "$KVER"
fi
[[ -s "$ROOT/boot/initrd.img-$KVER" ]]
mkdir -p "$ROOT/etc/ld.so.conf.d"
printf '/opt/aios/runtime/lib\n' > "$ROOT/etc/ld.so.conf.d/aios-runtime.conf"
chroot "$ROOT" ldconfig
# ggml looks for its dynamic backends next to the executable: every one of them,
# CPU variants and Vulkan alike, must be reachable from bin/ or it is never loaded.
for backend in "$ROOT"/opt/aios/runtime/lib/libggml-cpu*.so "$ROOT"/opt/aios/runtime/lib/libggml-vulkan.so; do
  ln -sf "../lib/$(basename "$backend")" "$ROOT/opt/aios/runtime/bin/$(basename "$backend")"
done
mkdir -p "$ROOT/opt/aios/app" "$ROOT/etc/aios" dist
rsync -a --delete --exclude=.git --exclude=.venv --exclude=build --exclude=dist --exclude=node_modules --exclude=__pycache__ --exclude=.pytest_cache --exclude=.mypy_cache --exclude=.ruff_cache ./ "$ROOT/opt/aios/app/"
mkdir -p "$ROOT/opt/aios/app/frontend-admin/dist"
rsync -a --delete frontend-admin/dist/ "$ROOT/opt/aios/app/frontend-admin/dist/"
chroot "$ROOT" python3 -m venv /opt/aios/venv
chroot "$ROOT" /opt/aios/venv/bin/pip install --no-cache-dir --upgrade pip==26.2
chroot "$ROOT" /opt/aios/venv/bin/pip install --no-cache-dir /opt/aios/app
# Verify the installed wheel, not only imports from the source checkout.
chroot "$ROOT" env AIOS_DATA=/tmp/aios-package-check/data AIOS_ETC=/tmp/aios-package-check/etc /opt/aios/venv/bin/python -m aios init
rm -rf "$ROOT/tmp/aios-package-check"
mkdir -p "$ROOT/opt/aios/bin"
chroot "$ROOT" cc -O3 -fopenmp /opt/aios/app/runtime/benchmark.c -o /opt/aios/bin/aios-benchmark
chroot "$ROOT" /opt/aios/venv/bin/pip freeze > build/backend.lock
chroot "$ROOT" /opt/aios/webui/bin/python -m nltk.downloader -d /opt/aios/nltk_data punkt punkt_tab averaged_perceptron_tagger_eng stopwords
chroot "$ROOT" id aios >/dev/null 2>&1 || chroot "$ROOT" useradd --system --home-dir /var/lib/aios --shell /usr/sbin/nologin aios
chroot "$ROOT" id aios-webui >/dev/null 2>&1 || chroot "$ROOT" useradd --system --home-dir /var/lib/aios/webui --shell /usr/sbin/nologin aios-webui
cp config/aios.yaml config/repositories.yaml "$ROOT/etc/aios/"
# Record what this exact llama.cpp build can load, straight from the sources it
# was compiled from, so the catalogue never offers an architecture that would
# fail at load time. Regenerated on every build: it cannot drift.
python3 - "$PWD/build/llama.tar.gz" "$ROOT/etc/aios/supported-architectures.json" <<'ARCH'
import json, re, subprocess, sys
source = subprocess.run(['tar', '-xzOf', sys.argv[1], '--wildcards', '*/src/llama-arch.cpp'],
                        capture_output=True, text=True, check=True).stdout
block = re.search(r'LLM_ARCH_NAMES\s*=\s*\{(.*?)\n\};', source, re.S)
names = sorted({m for m in re.findall(r'"([a-z][a-z0-9_.-]*)"', block.group(1))} - {'clip'})
assert len(names) > 50, names
open(sys.argv[2], 'w').write(json.dumps(names, indent=1) + '\n')
print('architetture supportate registrate:', len(names))
ARCH
# Chat colours only: Open WebUI's own name and logo stay as its licence requires.
cp config/webui-custom.css "$ROOT"/opt/aios/webui/lib/python3*/site-packages/open_webui/static/custom.css
# A first visit opens in the dark theme, like the portal; users can still change it.
sed -i "s/localStorage.theme = 'system';/localStorage.theme = 'dark';/" "$ROOT"/opt/aios/webui/lib/python3*/site-packages/open_webui/frontend/index.html
cp config/nginx.conf "$ROOT/etc/nginx/sites-available/aios"
rm -f "$ROOT/etc/nginx/sites-enabled/default"
ln -sf /etc/nginx/sites-available/aios "$ROOT/etc/nginx/sites-enabled/aios"
mkdir -p "$ROOT/etc/systemd/system/nginx.service.d"
printf '[Unit]\nAfter=aios-firstboot.service\nRequires=aios-firstboot.service\n' > "$ROOT/etc/systemd/system/nginx.service.d/aios.conf"
cp config/nftables.conf "$ROOT/etc/nftables.conf"
# The appliance ships with no SSH rule; the installer replaces this fragment.
printf '# SSH disabled: no rule.\n' > "$ROOT/etc/aios/nftables-ssh.nft"
cp systemd/* "$ROOT/etc/systemd/system/"
mkdir -p "$ROOT/etc/netplan"
cp config/network-dhcp.yaml "$ROOT/etc/netplan/10-aios.yaml"
chmod 600 "$ROOT/etc/netplan/10-aios.yaml"
cat > "$ROOT/etc/sysctl.d/90-aios.conf" <<'SYS'
vm.swappiness=1
kernel.dmesg_restrict=1
kernel.kptr_restrict=2
net.ipv4.conf.all.rp_filter=2
kernel.sysrq=0
SYS
cat > "$ROOT/etc/logrotate.d/aios" <<'LOG'
/var/lib/aios/runtime/*.log /var/log/aios/*.log {
  size 10M
  rotate 5
  compress
  missingok
  notifempty
  copytruncate
  su aios aios
}
LOG
mkdir -p "$ROOT/etc/systemd/journald.conf.d"
printf '[Journal]\nSystemMaxUse=128M\nRuntimeMaxUse=32M\n' > "$ROOT/etc/systemd/journald.conf.d/aios.conf"
echo aios > "$ROOT/etc/hostname"
printf '127.0.0.1 localhost\n127.0.1.1 aios\n::1 localhost ip6-localhost\n' > "$ROOT/etc/hosts"
ln -sf /opt/aios/app/installer/console.sh "$ROOT/usr/local/bin/aios-console"
mkdir -p "$ROOT/usr/local/sbin"
ln -sf /opt/aios/app/installer/recovery-shell.sh "$ROOT/usr/local/bin/aios-recovery-shell"
ln -sf /opt/aios/app/installer/bootstrap-recovery.sh "$ROOT/usr/local/sbin/aios-bootstrap-recovery"
chroot "$ROOT" systemctl enable aios-nvidia-driver
chroot "$ROOT" systemctl enable aios-firstboot aios-control-plane aios-runtime-manager aios-download-worker aios-platform aios-open-webui aios-console aios-local-console aios-serial-console aios-hardware-profiler.timer aios-repository-sync.timer aios-update-check.timer nginx nftables systemd-networkd systemd-resolved systemd-timesyncd
# Closed appliance: no login prompt on any other virtual terminal, no magic
# SysRq keys; the physical console only offers the authenticated AIOS menu.
mkdir -p "$ROOT/etc/systemd/logind.conf.d"
printf '[Login]\nNAutoVTs=0\nReserveVT=0\n' > "$ROOT/etc/systemd/logind.conf.d/aios.conf"
chroot "$ROOT" systemctl mask getty@tty1.service serial-getty@ttyS0.service apt-daily.timer apt-daily-upgrade.timer
# Shipped inert: socket activation means disabling the service alone would still
# leave port 22 listening, so switch off both units and set OpenSSH's own kill
# switch. No host keys and no account able to log in either.
chroot "$ROOT" systemctl disable ssh.service ssh.socket
touch "$ROOT/etc/ssh/sshd_not_to_be_run"
chroot "$ROOT" passwd -l root
# Build identities and secrets must never reach a distributable image.
: > "$ROOT/etc/machine-id"
rm -f "$ROOT/var/lib/dbus/machine-id" "$ROOT/var/lib/systemd/random-seed"
rm -f "$ROOT/etc/ssh/ssh_host_"* "$ROOT/etc/nginx/aios.key" "$ROOT/etc/nginx/aios.crt" "$ROOT/etc/aios/webui.env"
rm -rf "$ROOT/etc/aios/secrets" "$ROOT/var/lib/aios"
mkdir -p "$ROOT/var/lib/aios"
chroot "$ROOT" apt-get clean
rm -rf "$ROOT/var/lib/apt/lists/"* "$ROOT/usr/src/llama-build" "$ROOT/usr/src/llama.cpp-$LLAMA_COMMIT"
rm -rf "$ROOT/tmp/"* "$ROOT/var/log/"* "$ROOT/root/.cache"
mkdir -p "$ROOT/var/log/nginx" "$ROOT/var/log/aios" "$ROOT/var/log/journal"
chroot "$ROOT" systemctl set-default multi-user.target
ln -sf /run/systemd/resolve/stub-resolv.conf "$ROOT/etc/resolv.conf"
IMAGE="$PWD/dist/aios-x86_64.img"
rm -f "$IMAGE"
truncate -s 13G "$IMAGE"
# Partition 4 occupies the 1 MiB gap that precedes partition 1, so the data
# partition stays last on the disk and keeps growing on first boot.
sgdisk -n 1:2048:+256M -t 1:ef00 -c 1:AIOS-EFI -n 2:0:+10G -t 2:8300 -c 2:AIOS-ROOT -n 3:0:0 -t 3:8300 -c 3:AIOS-DATA -n 4:34:2047 -t 4:ef02 -c 4:AIOS-BIOS "$IMAGE"
LOOP=$(losetup --find --show --partscan "$IMAGE")
MOUNT=$(mktemp -d /mnt/aios-build.XXXXXX)
cleanup() { umount -R "$MOUNT" 2>/dev/null || true; losetup -d "$LOOP"; rmdir "$MOUNT"; }
trap cleanup EXIT
udevadm settle
mkfs.vfat -F32 -n AIOS-EFI "${LOOP}p1"
mkfs.ext4 -F -L AIOS-ROOT -E lazy_itable_init=0,lazy_journal_init=0 "${LOOP}p2"
mkfs.ext4 -F -L AIOS-DATA -E lazy_itable_init=0,lazy_journal_init=0 "${LOOP}p3"
mount "${LOOP}p2" "$MOUNT"
rsync -aHAXx --numeric-ids "$ROOT/" "$MOUNT/"
mkdir -p "$MOUNT/boot/efi" "$MOUNT/var/lib/aios"
mount "${LOOP}p1" "$MOUNT/boot/efi"
mount "${LOOP}p3" "$MOUNT/var/lib/aios"
cat > "$MOUNT/etc/fstab" <<FSTAB
UUID=$(blkid -s UUID -o value "${LOOP}p2") / ext4 defaults,noatime 0 1
UUID=$(blkid -s UUID -o value "${LOOP}p1") /boot/efi vfat umask=0077 0 2
UUID=$(blkid -s UUID -o value "${LOOP}p3") /var/lib/aios ext4 defaults,noatime 0 2
FSTAB
mount --bind /dev "$MOUNT/dev"
mount -t proc proc "$MOUNT/proc"
mount -t sysfs sysfs "$MOUNT/sys"
# The build cache keeps compilers; the appliance itself does not need them.
chroot "$MOUNT" apt-get purge -y build-essential cmake cmake-data ninja-build gcc g++ cpp git python3-dev libcurl4-openssl-dev libssl-dev glslc libvulkan-dev spirv-headers
# The proprietary NVIDIA modules are linked against the kernel at install time.
chroot "$MOUNT" apt-mark manual binutils >/dev/null
chroot "$MOUNT" apt-get autoremove -y
# The finished system must load the Vulkan backend. Mesa's software renderer,
# made visible on purpose, stands in for a GPU the build host does not have.
DEVICES=$(chroot "$MOUNT" env LD_LIBRARY_PATH=/opt/aios/runtime/lib GGML_VK_VISIBLE_DEVICES=0 /opt/aios/runtime/bin/llama-server --list-devices 2>&1 || true)
grep -q 'Vulkan0:' <<<"$DEVICES" || { echo "the image cannot load the Vulkan backend: $DEVICES"; exit 1; }
IMAGE_DEVICES=$(chroot "$MOUNT" env LD_LIBRARY_PATH=/opt/aios/imaging/lib GGML_VK_VISIBLE_DEVICES=0 /opt/aios/imaging/bin/sd-server --list-devices 2>&1 || true)
grep -q '^Vulkan0' <<<"$IMAGE_DEVICES" || { echo "the image cannot load the Vulkan backend of the image engine: $IMAGE_DEVICES"; exit 1; }
chroot "$MOUNT" apt-get clean
chroot "$MOUNT" dpkg-query -W -f='${Package}=${Version}\n' > build/os-packages.lock
chroot "$MOUNT" grub-install --target=x86_64-efi --efi-directory=/boot/efi --bootloader-id=AIOS --removable --no-nvram
# Legacy BIOS path: MBR boot code plus core.img in the BIOS boot partition.
chroot "$MOUNT" grub-install --target=i386-pc --boot-directory=/boot "$LOOP"
ROOTUUID=$(blkid -s UUID -o value "${LOOP}p2")
KERNEL=$(basename "$(ls "$MOUNT"/boot/vmlinuz-* | sort -V | tail -1)")
INITRD=${KERNEL/vmlinuz/initrd.img}
cat > "$MOUNT/boot/grub/grub.cfg" <<GRUB
set timeout=3
set default=0
serial --unit=0 --speed=115200
terminal_input console serial
terminal_output console serial
menuentry 'AIOS appliance' {
 search --no-floppy --fs-uuid --set=root $ROOTUUID
 linux /boot/$KERNEL root=UUID=$ROOTUUID ro quiet splash plymouth.ignore-serial-consoles loglevel=3 vt.global_cursor_default=0 systemd.show_status=false console=ttyS0,115200n8 console=tty0
 initrd /boot/$INITRD
}
GRUB
# Preserve serial console for subsequent installed-disk GRUB regeneration.
# The menu stays hidden: the machine boots straight into the splash.
printf 'GRUB_TIMEOUT=0\nGRUB_TIMEOUT_STYLE=hidden\nGRUB_RECORDFAIL_TIMEOUT=0\nGRUB_DISABLE_RECOVERY=true\nGRUB_DISABLE_OS_PROBER=true\nGRUB_CMDLINE_LINUX_DEFAULT="quiet splash"\nGRUB_CMDLINE_LINUX="plymouth.ignore-serial-consoles loglevel=3 vt.global_cursor_default=0 systemd.show_status=false console=ttyS0,115200n8 console=tty0"\n' > "$MOUNT/etc/default/grub"
sync
fstrim "$MOUNT" || true
cleanup
trap - EXIT
(cd dist && sha256sum aios-x86_64.img > aios-x86_64.img.sha256)
python3 scripts/build-info.py
if [[ -n "${SUDO_UID:-}" ]]; then chown "$SUDO_UID:${SUDO_GID}" dist/* build/*.lock; fi
echo "Built $IMAGE"

#!/usr/bin/env bash
# Development only: update code in an existing, stopped image. A release still
# requires make image and fresh checksum/boot verification.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
[[ $EUID == 0 ]] || exec sudo "$0"
IMAGE="$PWD/dist/aios-x86_64.img"
[[ -f "$IMAGE" ]]
if pgrep -f '^qemu-system-x86_64.*(qemu-test|installer-test|aios-x86_64.img)' >/dev/null; then echo 'Stop test VM first'; exit 1; fi
# Release images intentionally contain no compiler. Build the native helper in
# the Ubuntu build cache, keeping its ABI compatible with the appliance.
BUILD_ROOT="$PWD/build/rootfs"
[[ -x "$BUILD_ROOT/usr/bin/cc" ]] || { echo 'Build cache with compiler required; run make image first.'; exit 1; }
cp runtime/benchmark.c "$BUILD_ROOT/tmp/aios-refresh-benchmark.c"
chroot "$BUILD_ROOT" cc -O3 -fopenmp /tmp/aios-refresh-benchmark.c -o /tmp/aios-refresh-benchmark
rm "$BUILD_ROOT/tmp/aios-refresh-benchmark.c"
LOOP=$(losetup --find --show --partscan "$IMAGE")
DEST=$(mktemp -d /mnt/aios-refresh.XXXXXX)
cleanup() { umount "$DEST"; losetup -d "$LOOP"; rmdir "$DEST"; }
trap cleanup EXIT
mount "${LOOP}p2" "$DEST"
rsync -a --delete backend/aios/ "$DEST/opt/aios/app/backend/aios/"
rsync -a --delete backend/aios/ "$DEST/opt/aios/venv/lib/python3.12/site-packages/aios/"
rsync -a --delete frontend-admin/dist/ "$DEST/opt/aios/app/frontend-admin/dist/"
rsync -a scripts/ "$DEST/opt/aios/app/scripts/"
rsync -a installer/ "$DEST/opt/aios/app/installer/"
cp systemd/* "$DEST/etc/systemd/system/"
cp config/nginx.conf "$DEST/etc/nginx/sites-available/aios"
mkdir -p "$DEST/var/log/journal" "$DEST/opt/aios/bin"
install -m 755 "$BUILD_ROOT/tmp/aios-refresh-benchmark" "$DEST/opt/aios/bin/aios-benchmark"
rm "$BUILD_ROOT/tmp/aios-refresh-benchmark"
chroot "$DEST" systemctl set-default multi-user.target
rm -f dist/aios-x86_64.img.sha256 dist/aios-x86_64-build-info.json
sync

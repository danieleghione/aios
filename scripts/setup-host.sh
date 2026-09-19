#!/usr/bin/env bash
set -Eeuo pipefail
sudo apt-get update
sudo apt-get install -y --no-install-recommends debootstrap qemu-system-x86 ovmf gdisk dosfstools e2fsprogs parted rsync zstd build-essential cmake ninja-build libcurl4-openssl-dev python3-venv python3-dev mtools uuid-runtime curl git xz-utils squashfs-tools xorriso
NODE_VERSION=v22.22.1
if ! command -v node >/dev/null || [[ $(node -p 'Number(process.versions.node.split(".")[0])') -lt 22 ]]; then
  WORK=$(mktemp -d)
  trap 'rm -rf "$WORK"' EXIT
  curl --fail --location "https://nodejs.org/dist/$NODE_VERSION/node-$NODE_VERSION-linux-x64.tar.xz" -o "$WORK/node-$NODE_VERSION-linux-x64.tar.xz"
  curl --fail --location "https://nodejs.org/dist/$NODE_VERSION/SHASUMS256.txt" -o "$WORK/SHASUMS256.txt"
  (cd "$WORK" && awk -v name="node-$NODE_VERSION-linux-x64.tar.xz" '$2==name' SHASUMS256.txt | sha256sum -c -)
  sudo tar -xJf "$WORK/node-$NODE_VERSION-linux-x64.tar.xz" -C /usr/local --strip-components=1
fi
node --version
npm --version
